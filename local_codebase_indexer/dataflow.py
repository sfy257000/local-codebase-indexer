"""Data access queries: who reads / writes / executes a table or stored procedure, and what a procedure touches.

Direct accesses come from the ``data_access`` table. Indirect ones follow SQL objects: if procedure P writes
table T, then everything that executes P (C# code or other procedures) is an indirect writer of T, found up to
``MAX_DEPTH`` hops. Accesses with ``uncertain`` confidence (dynamic SQL) are never mixed into the confirmed lists;
they are reported as ``possible``. Call chains between C# methods are not followed here, use ``relations``.
"""

from __future__ import annotations

import re
import sqlite3
from collections import deque

MAX_DEPTH = 4
LIST_CAP = 200
SQL_OBJECT_KINDS = ("table", "procedure", "view", "function")
_CONF = {"confirmed": 0, "probable": 1, "uncertain": 2}

WRITE_WORDS = {
    "write", "writes", "written", "writing", "update", "updates", "modify", "modifies", "modifying", "insert",
    "inserts", "delete", "deletes", "change", "changes", "save", "saves", "persist", "persists", "populate",
    "populates", "寫入", "寫", "更新", "修改", "新增", "刪除", "異動", "變更", "儲存",
    "写入", "写", "删除", "变更", "储存",
}
READ_WORDS = {
    "read", "reads", "select", "selects", "query", "queries", "fetch", "fetches", "load", "loads", "retrieve",
    "retrieves", "讀取", "讀", "查詢", "取得", "撈", "读取", "读", "查询", "取得",
}
TOUCH_WORDS = {
    "touch", "touches", "access", "accesses", "reference", "references", "use", "uses", "used", "using",
    "depend", "depends", "involving", "存取", "使用", "用到", "相關", "涉及", "相关",
}
CALL_WORDS = {
    "call", "calls", "called", "execute", "executes", "executed", "invoke", "invokes", "invoked", "exec", "run",
    "runs", "呼叫", "執行", "被誰", "誰用", "调用", "执行", "被谁",
}


def object_key(name: str) -> str:
    """Lower-case last segment of a possibly schema / database qualified, bracketed name."""
    parts = [p.strip("[]\"` ") for p in name.strip().split(".")]
    return (parts[-1] if parts else "").lower()


def detect_intents(query: str) -> set[str]:
    words = set(re.findall(r"[A-Za-z]+", query.lower()))
    out = set()
    for intent, vocab in (("write", WRITE_WORDS), ("read", READ_WORDS), ("touch", TOUCH_WORDS), ("call", CALL_WORDS)):
        if words & vocab or any((not v.isascii()) and v in query for v in vocab):
            out.add(intent)
    return out


def known_object_keys(conn: sqlite3.Connection) -> set[str]:
    keys = {r[0] for r in conn.execute("SELECT DISTINCT target_key FROM data_access WHERE target_key != '?'")}
    keys |= {r[0].lower() for r in conn.execute(
        f"SELECT symbol_name FROM symbols WHERE symbol_type IN ({','.join('?' * len(SQL_OBJECT_KINDS))})",
        SQL_OBJECT_KINDS)}
    return keys


class _Lookup:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._sym: dict[str, sqlite3.Row | None] = {}

    def symbol(self, sid: str | None):
        if not sid:
            return None
        if sid not in self._sym:
            self._sym[sid] = self.conn.execute(
                "SELECT id, symbol_name, symbol_type, qualified_name, file_path, start_line FROM symbols WHERE id=?",
                (sid,)).fetchone()
        return self._sym[sid]

    def entry(self, row: sqlite3.Row, depth: int = 0, chain: list[str] | None = None) -> dict:
        s = self.symbol(row["source_id"])
        return {
            "source": (s["qualified_name"] or s["symbol_name"]) if s else row["file_path"],
            "source_type": s["symbol_type"] if s else "file",
            "source_id": row["source_id"],
            "file_path": row["file_path"],
            "line": row["line"],
            "target": row["target_name"],
            "op": row["op"],
            "access": row["access"],
            "via": row["via"],
            "confidence": row["confidence"],
            "evidence": row["evidence"],
            "detail": row["detail"],
            "depth": depth,
            "via_chain": chain or [],
        }


def _sort(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: (e["depth"], _CONF[e["confidence"]], e["file_path"], e["line"]))


def _definitions(conn: sqlite3.Connection, key: str) -> list[dict]:
    return [
        {"file_path": r["file_path"], "line": r["start_line"], "type": r["symbol_type"],
         "qualified_name": r["qualified_name"], "id": r["id"]}
        for r in conn.execute(
            f"SELECT id, file_path, start_line, symbol_type, qualified_name FROM symbols "
            f"WHERE lower(symbol_name)=? AND symbol_type IN ({','.join('?' * len(SQL_OBJECT_KINDS))}) "
            f"ORDER BY file_path, start_line", (key, *SQL_OBJECT_KINDS))
    ]


def _expand_indirect(conn: sqlite3.Connection, lk: _Lookup, direct: list[dict], max_depth: int) -> list[dict]:
    """Accessors of the SQL objects that themselves access the target (procedures executing procedures, views
    read by code, ...). Each result keeps the effective access of the direct entry it descends from."""
    out: list[dict] = []
    seen = {(e["source_id"], e["line"]) for e in direct}
    queue = deque()
    for e in direct:
        if e["source_type"] in ("procedure", "view", "function"):
            queue.append((e, e["via_chain"] + [lk.symbol(e["source_id"])["symbol_name"]]))
    while queue:
        entry, chain = queue.popleft()
        if len(chain) > max_depth:
            continue
        key = chain[-1].lower()
        for r in conn.execute(
            "SELECT * FROM data_access WHERE target_key=? AND access IN ('exec','read') AND confidence != 'uncertain'",
            (key,),
        ):
            sig = (r["source_id"], r["line"])
            if sig in seen:
                continue
            seen.add(sig)
            e = lk.entry(r, depth=len(chain), chain=list(chain))
            e["access"], e["op"] = entry["access"], entry["op"]  # effective access on the original target
            out.append(e)
            if e["source_type"] in ("procedure", "view", "function"):
                queue.append((e, chain + [lk.symbol(e["source_id"])["symbol_name"]]))
    return out


def _touches(conn: sqlite3.Connection, lk: _Lookup, definitions: list[dict], max_depth: int) -> dict:
    """What a procedure / view / function reads, writes and executes, following nested procedure calls."""
    reads, writes, execs, dynamic = [], [], [], []
    seen_obj = set()
    queue = deque((d["id"], [], 0) for d in definitions)
    while queue:
        sid, chain, depth = queue.popleft()
        if sid in seen_obj:
            continue
        seen_obj.add(sid)
        for r in conn.execute("SELECT * FROM data_access WHERE source_id=? ORDER BY line", (sid,)):
            e = lk.entry(r, depth=depth, chain=chain)
            if r["op"] == "dynamic" or r["confidence"] == "uncertain":
                dynamic.append(e)
            elif r["access"] == "write":
                writes.append(e)
            elif r["access"] == "read":
                reads.append(e)
            elif r["access"] == "exec":
                execs.append(e)
                if depth < max_depth:
                    for d in _definitions(conn, r["target_key"]):
                        if d["type"] in ("procedure", "function", "view"):
                            queue.append((d["id"], chain + [r["target_name"]], depth + 1))
    return {"reads": _sort(reads)[:LIST_CAP], "writes": _sort(writes)[:LIST_CAP],
            "executes": _sort(execs)[:LIST_CAP], "dynamic_sql": _sort(dynamic)[:LIST_CAP]}


def data_access(conn: sqlite3.Connection, object_name: str, max_depth: int = MAX_DEPTH) -> dict:
    key = object_key(object_name)
    lk = _Lookup(conn)
    defs = _definitions(conn, key)
    rows = conn.execute("SELECT * FROM data_access WHERE target_key=? ORDER BY file_path, line", (key,)).fetchall()
    if not defs and not rows:
        like = [r[0] for r in conn.execute(
            "SELECT DISTINCT target_name FROM data_access WHERE target_key LIKE ? AND target_key != '?' LIMIT 10",
            (f"%{key}%",))]
        like += [r[0] for r in conn.execute(
            f"SELECT DISTINCT symbol_name FROM symbols WHERE symbol_type IN ({','.join('?' * len(SQL_OBJECT_KINDS))}) "
            f"AND lower(symbol_name) LIKE ? LIMIT 10", (*SQL_OBJECT_KINDS, f"%{key}%"))]
        return {"object": object_name, "key": key, "found": False, "suggestions": sorted(set(like))[:10]}

    if defs:
        kind = defs[0]["type"]
    else:
        kinds = [r["target_kind"] for r in rows]
        kind = max(set(kinds), key=kinds.count) if kinds else "unknown"

    direct = [lk.entry(r) for r in rows]
    confirmed = [e for e in direct if e["confidence"] != "uncertain"]
    possible = [e for e in direct if e["confidence"] == "uncertain"]
    indirect = _expand_indirect(conn, lk, [e for e in confirmed if e["access"] in ("write", "read", "exec")], max_depth)

    def pick(access: str) -> list[dict]:
        return _sort([e for e in confirmed + indirect if e["access"] == access])

    result = {
        "object": object_name, "key": key, "found": True, "kind": kind, "definitions": defs,
        "readers": pick("read")[:LIST_CAP],
        "writers": pick("write")[:LIST_CAP],
        "executed_by": pick("exec")[:LIST_CAP],
        "mapped_by": _sort([e for e in confirmed if e["access"] == "map"]),
        "possible": _sort(possible)[:LIST_CAP],
        "notes": [],
    }
    if kind in ("procedure", "function", "view") and defs:
        result["touches"] = _touches(conn, lk, [d for d in defs if d["type"] == kind], max_depth)
    # dynamic SQL elsewhere whose text names this object
    word = re.compile(rf"(?<![\w$]){re.escape(key)}(?![\w$])", re.I)
    for r in conn.execute("SELECT * FROM data_access WHERE target_key='?' AND op='dynamic'"):
        if word.search(r["evidence"] or ""):
            result["possible"].append(lk.entry(r))
    result["notes"].append(
        "Static analysis: dynamic SQL is listed under 'possible' and cannot be resolved. C# call chains are not "
        "followed; use the relations tool for who calls a C# method.")
    return result


def accessors_for_search(conn: sqlite3.Connection, key: str, intents: set[str]) -> list[dict]:
    """Ordered accessors of ``key`` that match the intents of a natural language query (empty when the intent
    does not apply to the object)."""
    res = data_access(conn, key, max_depth=2)
    if not res["found"]:
        return []
    kind = res["kind"]
    picked: list[dict] = []
    if kind in ("procedure", "function"):
        if intents & {"call", "touch"}:
            picked = res["executed_by"]
    else:
        want = set()
        if "write" in intents:
            want.add("write")
        if "read" in intents:
            want.add("read")
        if "touch" in intents or "call" in intents:
            want |= {"read", "write"}
        for e in res["writers"] + res["readers"]:
            if e["access"] in want:
                picked.append(e)
        if "touch" in intents or "call" in intents:
            picked += res["mapped_by"]
    seen, out = set(), []
    for e in _sort(picked):
        sig = (e["source_id"], e["file_path"], e["line"])
        if sig not in seen:
            seen.add(sig)
            out.append(e)
    return out
