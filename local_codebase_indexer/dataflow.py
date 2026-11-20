"""Data access queries: who reads / writes / executes a table or stored procedure, and what a procedure touches.

Direct accesses come from the ``data_access`` table. Indirect ones follow SQL objects: if procedure P writes
table T, then everything that executes P (C# code or other procedures) is an indirect writer of T, found up to
``MAX_DEPTH`` hops. Accesses with ``uncertain`` confidence (dynamic SQL) are never mixed into the confirmed lists;
they are reported as ``possible``. ``programs`` follows confirmed / probable C# calls upward from the accessing
methods to controller actions (up to ``ENTRY_DEPTH`` hops), groups them per controller file and lists the Razor
pages whose ``Url.Action`` targets those actions.
"""

from __future__ import annotations

import re
import sqlite3
from collections import deque

from . import sqlidentity
from .sqlaccess import _split_name, _unquote

MAX_DEPTH = 4
ENTRY_DEPTH = 8
CALLEE_DEPTH = 4
_METHOD_KINDS = ("method", "constructor", "function", "vb6_method")
SQL_OBJECT_KINDS = ("table", "procedure", "view", "function")
_SQL_DEFINITION = "file_path IN (SELECT file_path FROM files WHERE language='sql')"
_CONF = {"confirmed": 0, "probable": 1, "uncertain": 2}
_ANCESTOR_DEPTH = 12
_TYPE_KINDS = ("class", "interface", "struct", "record")

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
    parts = _split_name(name.strip())
    return (_unquote(parts[-1]) if parts else "").lower()


def detect_intents(query: str) -> set[str]:
    words = set(re.findall(r"[A-Za-z]+", query.lower()))
    out = set()
    for intent, vocab in (("write", WRITE_WORDS), ("read", READ_WORDS), ("touch", TOUCH_WORDS), ("call", CALL_WORDS)):
        if words & vocab or any((not v.isascii()) and v in query for v in vocab):
            out.add(intent)
    return out


def known_object_keys(conn: sqlite3.Connection) -> set[str]:
    keys = {r[0] for r in conn.execute(
        "SELECT DISTINCT target_key FROM data_access WHERE target_key != '?' AND via != 'ef_raw'")}
    keys |= {r[0].lower() for r in conn.execute(
        f"SELECT symbol_name FROM symbols WHERE {_SQL_DEFINITION} AND symbol_type IN ({','.join('?' * len(SQL_OBJECT_KINDS))})",
        SQL_OBJECT_KINDS)}
    return keys


class _Catalog:
    """Resolve names only within explicit, compatible indexed scopes."""
    def __init__(self, conn):
        self.conn = conn
        self.cache = {}

    def objects(self, name):
        key = name[-1]
        if key not in self.cache:
            definitions = self.conn.execute(
                f"SELECT * FROM symbols WHERE {_SQL_DEFINITION} AND lower(symbol_name)=? "
                f"AND symbol_type IN ({','.join('?' * len(SQL_OBJECT_KINDS))}) ORDER BY file_path,start_line",
                (key, *SQL_OBJECT_KINDS)).fetchall()
            if not key.isascii():
                # SQLite's built-in lower() is ASCII-only; keep Unicode SQL names
                # consistent with the Python-normalized lookup key.
                definitions = [d for d in self.conn.execute(
                    f"SELECT * FROM symbols WHERE {_SQL_DEFINITION} AND symbol_type IN ({','.join('?' * len(SQL_OBJECT_KINDS))}) "
                    "ORDER BY file_path,start_line", SQL_OBJECT_KINDS)
                    if d['symbol_name'].lower() == key]
            rows = self.conn.execute(
                "SELECT * FROM data_access WHERE target_key=? AND via!='ef_raw' ORDER BY file_path,line", (key,)).fetchall()
            identities = {self.definition_name(d) for d in definitions} | {sqlidentity.from_row(r) for r in rows}
            maximal = {a for a in identities if not any(sqlidentity.refines(b, a) for b in identities)}
            self.cache[key] = (definitions, rows, maximal)
        return self.cache[key]

    @staticmethod
    def definition_name(d):
        return sqlidentity.decode(d['sql_identity']) if d['sql_identity'] else sqlidentity.parts(d['qualified_name'])

    def select(self, name):
        definitions, rows, maximal = self.objects(name)
        candidates = sorted(a for a in maximal if sqlidentity.compatible(a, name))
        if candidates and all(name == c or sqlidentity.refines(name, c) for c in candidates):
            candidates = [name]
        if len(candidates) != 1:
            return None, candidates, [], []
        selected = sqlidentity.combine(name, candidates[0])
        defs = [d for d in definitions if self.definition_name(d) == selected]
        matched = []
        for row in rows:
            identity = sqlidentity.from_row(row)
            if not sqlidentity.compatible(identity, selected):
                continue
            choices = [a for a in maximal if sqlidentity.compatible(identity, a)]
            confidence = row['confidence']
            if identity != selected:
                confidence = ('probable' if choices == [selected] and all(identity) else 'uncertain')
                if row['confidence'] == 'uncertain':
                    confidence = 'uncertain'
            matched.append((row, confidence))
        return selected, candidates, defs, matched


class _Lookup:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._sym: dict[str, sqlite3.Row | None] = {}
        self.catalog = _Catalog(conn)
        self.warnings: list[dict] = []
        self._bases: dict[str, list[str]] | None = None
        self._type_rows: dict[str, list[sqlite3.Row]] = {}
        self._children: dict[str, list[str]] | None = None
        self._impl_count: dict[tuple[str, str], int] = {}

    def symbol(self, sid: str | None):
        if not sid:
            return None
        if sid not in self._sym:
            self._sym[sid] = self.conn.execute(
                "SELECT id, symbol_name, symbol_type, qualified_name, file_path, start_line, sql_identity, parent_id "
                "FROM symbols WHERE id=?",
                (sid,)).fetchone()
        return self._sym[sid]

    def ancestor_methods(self, sid: str) -> list[sqlite3.Row]:
        """Same-named methods on every base class / interface above the class of method ``sid``."""
        if self._bases is None:
            self._bases = {}
            for r in self.conn.execute("SELECT owner, name FROM raw_refs WHERE kind = 'base'"):
                self._bases.setdefault(r[0], []).append(r[1])
        sym = self.symbol(sid)
        if sym is None or not sym["parent_id"]:
            return []
        out: list[sqlite3.Row] = []
        seen = {sym["parent_id"]}
        level = [sym["parent_id"]]
        for _ in range(_ANCESTOR_DEPTH):
            nxt = []
            for owner in level:
                for name in self._bases.get(owner, ()):
                    for t in self._types(name):
                        if t["id"] not in seen:
                            seen.add(t["id"])
                            nxt.append(t["id"])
                            out.extend(self.conn.execute(
                                "SELECT id, file_path, start_line, ? AS symbol_type, parent_id FROM symbols "
                                "WHERE symbol_name = ? AND parent_id = ?", (t["symbol_type"], sym["symbol_name"], t["id"])))
            if not nxt:
                break
            level = nxt
        return out

    def implementations(self, type_id: str, method: str) -> int:
        """Classes below ``type_id`` (any depth) that declare ``method``: how many targets a call can dispatch to."""
        key = (type_id, method)
        if key not in self._impl_count:
            if self._children is None:
                self._children = {}
                for owner, names in (self._bases or {}).items():
                    for name in names:
                        self._children.setdefault(name, []).append(owner)
            root = self.symbol(type_id)
            seen: set[str] = set()
            level = [root["symbol_name"]] if root else []
            count = 0
            for _ in range(_ANCESTOR_DEPTH):
                nxt = []
                for name in level:
                    for child in self._children.get(name, ()):
                        if child in seen:
                            continue
                        seen.add(child)
                        row = self.symbol(child)
                        if row is None:
                            continue
                        nxt.append(row["symbol_name"])
                        if row["symbol_type"] != "interface" and any(
                                not re.search(r"\babstract\b", sig or "") for (sig,) in self.conn.execute(
                                    "SELECT signature FROM symbols WHERE symbol_name = ? AND parent_id = ?", (method, child))):
                            count += 1  # abstract declarations have no body to dispatch to
                if not nxt:
                    break
                level = nxt
            self._impl_count[key] = count
        return self._impl_count[key]

    def _types(self, name: str) -> list[sqlite3.Row]:
        if name not in self._type_rows:
            self._type_rows[name] = self.conn.execute(
                f"SELECT id, symbol_type FROM symbols WHERE symbol_name = ? AND symbol_type IN ({','.join('?' * len(_TYPE_KINDS))})",
                (name, *_TYPE_KINDS)).fetchall()
        return self._type_rows[name]

    def entry(self, row: sqlite3.Row, depth: int = 0, chain: list[str] | None = None) -> dict:
        s = self.symbol(row["source_id"])
        source_name = (s['qualified_name'] or s['symbol_name']) if s else row['file_path']
        path_name = sqlidentity.display(self.catalog.definition_name(s)) if s and s['sql_identity'] else source_name
        identity = sqlidentity.from_row(row)
        target = {'symbol': sqlidentity.display(identity), 'relation': 'sql_object'}
        definitions = [d for d in self.catalog.objects(identity)[0] if self.catalog.definition_name(d) == identity]
        if len(definitions) == 1:
            target.update(file_path=definitions[0]['file_path'], line=definitions[0]['start_line'])
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
            "object_identity": sqlidentity.display(sqlidentity.from_row(row)),
            "path": [{"symbol": path_name,
                      "file_path": row["file_path"], "line": row["line"], "relation": row["access"],
                      "confidence": row["confidence"], "evidence": row["evidence"]},
                     target],
        }


def _sort(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: (e["depth"], _CONF[e["confidence"]], e["file_path"], e["line"]))


def _definitions(conn: sqlite3.Connection, key: str) -> list[dict]:
    _selected, _candidates, definitions, _rows = _Catalog(conn).select(sqlidentity.parts(key))
    return [_definition(d) for d in definitions]


def _definition(r):
    return {"file_path": r["file_path"], "line": r["start_line"], "type": r["symbol_type"],
            "qualified_name": r["qualified_name"], "id": r["id"],
            "object_identity": sqlidentity.display(_Catalog.definition_name(r))}


def _expand_indirect(conn: sqlite3.Connection, lk: _Lookup, direct: list[dict], max_depth: int) -> list[dict]:
    out: dict[tuple, dict] = {}
    seen = {(e["source_id"], e["line"], e["access"]): _CONF[e['confidence']] for e in direct}
    queue: deque = deque((e, []) for e in direct if e['source_type'] in ('procedure', 'view', 'function'))
    while queue:
        entry, chain = queue.popleft()
        symbol = lk.symbol(entry['source_id'])
        name = _Catalog.definition_name(symbol)
        _selected, _candidates, _definitions_found, bindings = lk.catalog.select(name)
        viable = [(r, conf) for r, conf in bindings if r['access'] in ('exec', 'read')]
        if len(chain) >= max_depth:
            if any((r['source_id'], r['line'], entry['access']) not in seen for r, _ in viable):
                lk.warnings.append({'code': 'sql_depth_limit', 'object': sqlidentity.display(name), 'limit': max_depth})
            continue
        next_chain = chain + [symbol['symbol_name']]
        for r, confidence in viable:
            sig = (r['source_id'], r['line'], entry['access'])
            effective = max(entry['confidence'], confidence, key=_CONF.__getitem__)
            if seen.get(sig, 99) <= _CONF[effective]:
                continue
            seen[sig] = _CONF[effective]
            e = lk.entry(r, depth=len(next_chain), chain=next_chain)
            e['access'], e['op'] = entry['access'], entry['op']
            e['confidence'] = max(entry['confidence'], confidence, key=_CONF.__getitem__)
            e['path'][0]['confidence'] = confidence
            e['path'] = e['path'][:1] + entry['path']
            out[sig] = e
            if e['source_type'] in ('procedure', 'view', 'function'):
                queue.append((e, next_chain))
    return list(out.values())


def _touches(conn: sqlite3.Connection, lk: _Lookup, definitions: list[dict], max_depth: int) -> dict:
    """What a procedure / view / function reads, writes and executes, following nested procedure calls."""
    reads, writes, execs, dynamic, schema = [], [], [], [], []
    seen_obj = set()
    queue: deque = deque((d["id"], [], 0) for d in definitions)
    while queue:
        sid, chain, depth = queue.popleft()
        if sid in seen_obj:
            continue
        seen_obj.add(sid)
        for r in conn.execute("SELECT * FROM data_access WHERE source_id=? ORDER BY line", (sid,)):
            e = lk.entry(r, depth=depth, chain=chain)
            if r["op"] == "dynamic" or r["confidence"] == "uncertain":
                dynamic.append(e)
            elif r["access"] == "schema":
                schema.append(e)
            elif r["access"] == "write":
                writes.append(e)
            elif r["access"] == "read":
                reads.append(e)
            elif r["access"] == "exec":
                execs.append(e)
                if depth < max_depth:
                    _selected, candidates, definitions, _bindings = lk.catalog.select(sqlidentity.from_row(r))
                    if len(candidates) > 1 or not definitions:
                        lk.warnings.append({'code': 'unresolved_sql_definition', 'object': e['object_identity'],
                                            'candidates': [sqlidentity.display(c) for c in candidates]})
                    for definition in definitions:
                        if definition['symbol_type'] in ('procedure', 'function', 'view'):
                            queue.append((definition['id'], chain + [r['target_name']], depth + 1))
                elif _definitions(conn, sqlidentity.display(sqlidentity.from_row(r))):
                    lk.warnings.append({'code': 'sql_depth_limit', 'object': e['object_identity'], 'limit': max_depth})
    return {"reads": _sort(reads), "writes": _sort(writes),
            "executes": _sort(execs), "dynamic_sql": _sort(dynamic), "schema_changes": _sort(schema)}


def callee_accesses(conn: sqlite3.Connection, lk: _Lookup, source_id: str,
                    max_depth: int = CALLEE_DEPTH) -> list[tuple[sqlite3.Row, int, list[str], str]]:
    """Data accesses of a method and of the methods it reaches through confirmed / probable calls:
    (data_access row, call depth, callee chain, effective confidence)."""
    out = []
    seen = {source_id}
    queue: deque = deque([(source_id, 0, [], "confirmed")])
    while queue:
        sid, depth, chain, conf = queue.popleft()
        for r in conn.execute("SELECT * FROM data_access WHERE source_id=? AND via != 'ef_raw' ORDER BY line", (sid,)):
            out.append((r, depth, chain, max(conf, r["confidence"], key=_CONF.__getitem__)))
        callees = conn.execute(
            "SELECT DISTINCT callee_id, confidence FROM calls WHERE caller_id=? AND confidence IN ('confirmed','probable')",
            (sid,)).fetchall()
        if depth >= max_depth:
            if any(c["callee_id"] not in seen for c in callees):
                lk.warnings.append({'code': 'callee_depth_limit', 'symbol': chain[-1] if chain else source_id,
                                    'limit': max_depth})
            continue
        for c in callees:
            if c["callee_id"] in seen:
                continue
            seen.add(c["callee_id"])
            sym = lk.symbol(c["callee_id"])
            if sym is None or sym["symbol_type"] not in _METHOD_KINDS:
                continue
            queue.append((c["callee_id"], depth + 1, chain + [sym["qualified_name"] or sym["symbol_name"]],
                          max(conf, c["confidence"], key=_CONF.__getitem__)))
    return out


def _program(sym: sqlite3.Row) -> str | None:
    """Controller file label (``WD0096Controller.cs`` / ``WK0019.cs`` -> ``WD0096`` / ``WK0019``) for a method of a
    ``*Controller`` class, else None."""
    if not sym['file_path'].lower().endswith('.cs'):
        return None
    parts = (sym["qualified_name"] or "").split(".")
    if len(parts) < 2 or not parts[-2].endswith("Controller"):
        return None
    stem = sym["file_path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return stem[:-len("Controller")] if stem.endswith("Controller") and stem != "Controller" else stem


def _programs(conn: sqlite3.Connection, lk: _Lookup, entries: list[dict], call_depth: int = ENTRY_DEPTH) -> tuple[list[dict], list[dict]]:
    """Controller actions that reach the accessing methods through the C# call graph (one entry per program), and
    the methods on those paths that have no confirmed / probable caller at all (API, jobs, Main, or calls the graph
    could not resolve)."""
    queue: deque = deque()
    for e in entries:
        if e["source_type"] in _METHOD_KINDS and e["access"] != "map":
            queue.append((e["source_id"], e["access"], 0, e["confidence"], e.get("path", [])))
    seen: dict[tuple[str, str], int] = {}
    progs: dict[str, dict] = {}
    roots: dict[str, dict] = {}
    while queue:
        sid, access, depth, conf, path = queue.popleft()
        if seen.get((sid, access), 99) <= _CONF[conf]:
            continue
        seen[(sid, access)] = _CONF[conf]
        sym = lk.symbol(sid)
        if sym is None:
            continue
        if not path:
            path = [{'symbol': sym['qualified_name'], 'file_path': sym['file_path'],
                     'line': sym['start_line'], 'relation': access, 'confidence': conf}]
        label = _program(sym)
        if label is not None:
            p = progs.setdefault(label, {"program": label, "file_path": sym["file_path"], "access": set(),
                                         "actions": set(), "confidence": conf, "ids": set(), "paths": []})
            p["access"].add(access)
            p["actions"].add(sym["symbol_name"])
            p["ids"].add(sid)
            p["paths"].append({"action": sym["symbol_name"], "action_id": sid, "access": access, "confidence": conf, "steps": path})
            if _CONF[conf] < _CONF[p["confidence"]]:
                p["confidence"] = conf
            continue
        parents = [(r["caller_id"], max(conf, r["confidence"], key=_CONF.__getitem__), r["file_path"], r["line"], r["reason"]) for r in conn.execute(
            "SELECT caller_id, confidence, file_path, line, reason FROM calls WHERE callee_id=? AND confidence IN ('confirmed','probable')",
            (sid,))]
        # callers of the interface / base method this one implements or overrides may dispatch here
        # (pipelines: foreach step.Run()), also when the interface is implemented by an abstract base class;
        # with several implementations the runtime choice is unknown, so the path is only uncertain
        for r in lk.ancestor_methods(sid):
            impls = lk.implementations(r["parent_id"], sym["symbol_name"])
            kind = "interface dispatch" if r["symbol_type"] == "interface" else "override dispatch"
            edge = "probable" if impls <= 1 else "uncertain"
            parents.append((r["id"], max(conf, edge, key=_CONF.__getitem__), r["file_path"], r["start_line"],
                            kind if impls <= 1 else f"{kind}, one of {impls} implementations"))
        if depth >= call_depth and parents:
            lk.warnings.append({'code': 'call_depth_limit', 'symbol': sym['qualified_name'], 'limit': call_depth})
            continue
        for parent, pconf, file_path, line, reason in parents:
            psym = lk.symbol(parent)
            step = {'symbol': psym['qualified_name'] if psym else parent, 'file_path': file_path,
                    'line': line, 'confidence': pconf, 'relation': reason}
            queue.append((parent, access, depth + 1, pconf, [step] + path))
        if not parents and sym["symbol_type"] in _METHOD_KINDS:
            root = roots.setdefault(sid, {"symbol": sym["qualified_name"] or sym["symbol_name"],
                                          "file_path": sym["file_path"], "line": sym["start_line"], "access": set(), "confidence": conf})
            root["access"].add(access)
            root['confidence'] = min(root['confidence'], conf, key=_CONF.__getitem__)
    out = []
    for p in sorted(progs.values(), key=lambda p: p["program"]):
        ids = sorted(p.pop("ids"))
        pages = sorted({r[0] for r in conn.execute(
            f"SELECT s.file_path FROM calls c JOIN symbols s ON s.id = c.caller_id WHERE s.symbol_type = 'page' "
            f"AND c.callee_id IN ({','.join('?' * len(ids))})", ids)})
        for trace in p['paths']:
            action_id = trace.pop('action_id')
            trace['pages'] = [dict(r) for r in conn.execute(
                "SELECT DISTINCT c.file_path,c.line,c.confidence FROM calls c JOIN symbols s ON s.id=c.caller_id "
                "WHERE s.symbol_type='page' AND c.callee_id=? ORDER BY c.file_path,c.line", (action_id,))]
        out.append({**p, "access": sorted(p["access"]), "actions": sorted(p["actions"]), "pages": pages})
    no_caller = [{**r, "access": sorted(r["access"])} for r in sorted(roots.values(), key=lambda r: r["file_path"])]
    return out, no_caller


def data_access(conn: sqlite3.Connection, object_name: str, max_depth: int = MAX_DEPTH, call_depth: int = ENTRY_DEPTH) -> dict:
    key = object_key(object_name)
    lk = _Lookup(conn)
    if not 1 <= max_depth <= 32 or not 1 <= call_depth <= 64:
        raise ValueError('SQL depth must be 1..32 and call depth 1..64')
    requested = sqlidentity.parts(object_name)
    selected, candidates, definition_rows, bindings = lk.catalog.select(requested)
    if len(candidates) > 1:
        return {'object': object_name, 'key': key, 'found': False, 'ambiguous': True,
                'candidates': [sqlidentity.display(c) for c in candidates],
                'suggestions': [sqlidentity.display(c) for c in candidates],
                'notes': ['Use a qualified candidate. No accessors from different scopes were combined.']}
    defs = [_definition(d) for d in definition_rows]
    rows = [r for r, confidence in bindings]
    if not defs and not rows:
        like = [r[0] for r in conn.execute(
            "SELECT DISTINCT target_name FROM data_access WHERE target_key LIKE ? AND target_key != '?' "
            "AND via != 'ef_raw' LIMIT 10",
            (f"%{key}%",))]
        like += [r[0] for r in conn.execute(
            f"SELECT DISTINCT symbol_name FROM symbols WHERE {_SQL_DEFINITION} AND symbol_type IN ({','.join('?' * len(SQL_OBJECT_KINDS))}) "
            f"AND lower(symbol_name) LIKE ? LIMIT 10", (*SQL_OBJECT_KINDS, f"%{key}%"))]
        return {"object": object_name, "key": key, "found": False, "suggestions": sorted(set(like))[:10]}

    if defs:
        kind = defs[0]["type"]
    else:
        kinds = [r["target_kind"] for r in rows]
        kind = max(set(kinds), key=kinds.count) if kinds else "unknown"

    direct = []
    for row, confidence in bindings:
        entry = lk.entry(row)
        entry['confidence'] = max(entry['confidence'], confidence, key=_CONF.__getitem__)
        if entry['confidence'] != row['confidence']:
            entry['detail'] += ' Scope inferred from indexed candidates; default database/schema is not runtime-verified.'
            entry['path'][0]['confidence'] = entry['confidence']
        direct.append(entry)
    confirmed = [e for e in direct if e["confidence"] != "uncertain"]
    possible = [e for e in direct if e["confidence"] == "uncertain"]
    indirect = _expand_indirect(conn, lk, [e for e in direct if e["access"] in ("write", "read", "exec", "schema")], max_depth)

    possible.extend(e for e in indirect if e['confidence'] == 'uncertain')

    def pick(access: str) -> list[dict]:
        return _sort([e for e in confirmed + indirect if e["access"] == access and e["confidence"] != "uncertain"])

    result = {
        "object": object_name, "key": key, "found": True, "kind": kind, "definitions": defs,
        "object_identity": sqlidentity.display(selected), "ambiguous": False,
        "readers": pick("read"),
        "writers": pick("write"),
        "executed_by": pick("exec"),
        "mapped_by": _sort([e for e in confirmed if e["access"] == "map"]),
        "schema_changes": pick("schema"),
        "possible": _sort(possible),
        "programs": [],
        "no_known_caller": [],
        "notes": [],
    }
    result["programs"], result["no_known_caller"] = _programs(conn, lk, [e for e in confirmed + indirect if e['confidence'] != 'uncertain'], call_depth)
    if kind in ("procedure", "function", "view") and defs:
        result["touches"] = _touches(conn, lk, [d for d in defs if d["type"] == kind], max_depth)
    # dynamic SQL elsewhere whose text names this object
    word = re.compile(rf"(?<![\w$]){re.escape(key)}(?![\w$])", re.I)
    for r in conn.execute("SELECT * FROM data_access WHERE target_key='?' AND op='dynamic'"):
        if word.search(r["evidence"] or ""):
            result["possible"].append(lk.entry(r))
    result['coverage'] = {'complete_within_limits': not lk.warnings,
        'sql_depth': max_depth, 'call_depth': call_depth, 'warnings': lk.warnings,
        'unresolved_bindings': len(possible), 'scope': 'indexed supported static patterns; not runtime completeness'}
    result['notes'].extend(f"{w['code']}: {w.get('object', w.get('symbol', ''))}" + (f" (limit {w['limit']})" if 'limit' in w else '') for w in lk.warnings)
    result["notes"].append(
        "Static analysis: dynamic SQL is listed under 'possible' and cannot be resolved. PROGRAMS follows confirmed / "
        f"probable C# calls up to {ENTRY_DEPTH} hops to *Controller methods; use relations for a single method's callers.")
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
