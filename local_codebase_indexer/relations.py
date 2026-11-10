"""Symbol and type navigation, including call confidence and SQL access."""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from . import dataflow
from .query_common import _consistent_read

_TYPE_KINDS = ("class", "interface", "struct", "record", "enum", "type")

class RelationTools:
    conn: sqlite3.Connection

    # ------------------------------------------------------------------ Tool 2
    @_consistent_read(refresh=True)
    def symbol_relations(self, symbol_name: str, relation_direction: str = "both") -> dict:
        if relation_direction not in ("upstream", "downstream", "both"):
            raise ValueError("relation_direction must be 'upstream', 'downstream' or 'both'")
        conn = self.conn
        # "Type.Method" or a full qualified name narrows overloaded names such as InsertAsync to one class
        if "." in symbol_name:
            defs = conn.execute(
                "SELECT * FROM symbols WHERE symbol_name=? AND symbol_type != 'namespace' "
                "AND (qualified_name=? OR qualified_name LIKE ?) ORDER BY file_path, start_line LIMIT 8",
                (symbol_name.rsplit(".", 1)[1], symbol_name, "%." + symbol_name),
            ).fetchall()
        else:
            defs = conn.execute(
                "SELECT * FROM symbols WHERE symbol_name=? AND symbol_type != 'namespace' ORDER BY file_path, start_line LIMIT 8",
                (symbol_name,),
            ).fetchall()
        if not defs:
            like = conn.execute(
                "SELECT DISTINCT symbol_name FROM symbols WHERE symbol_name LIKE ? LIMIT 10", (f"%{symbol_name}%",)
            ).fetchall()
            return {"symbol": symbol_name, "definitions": [], "suggestions": [r[0] for r in like]}

        type_defs: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in conn.execute(
            f"SELECT * FROM symbols WHERE symbol_type IN ({','.join('?' * len(_TYPE_KINDS))})", _TYPE_KINDS
        ):
            type_defs[r["symbol_name"]].append(r)

        out_defs = []
        for d in defs:
            entry = {
                "id": d["id"], "file_path": d["file_path"], "type": d["symbol_type"],
                "qualified_name": d["qualified_name"], "start_line": d["start_line"],
                "end_line": d["end_line"], "signature": d["signature"],
            }
            if relation_direction in ("upstream", "both"):
                entry["upstream"] = self._upstream(d)
            if relation_direction in ("downstream", "both"):
                entry["downstream"] = self._downstream(d, type_defs)
            out_defs.append(entry)
        return {
            "symbol": symbol_name, "definitions": out_defs,
            "note": ("C# method calls are resolved by receiver type (confirmed / probable / candidate); type references and "
                 "fields remain identifier-name matches; inheritance edges come from the AST"),
        }

    def _type_upstream(self, d: sqlite3.Row) -> dict:
        name = d["symbol_name"]
        conn = self.conn
        inheritors = [
            {"symbol": r["qualified_name"], "type": r["symbol_type"], "file_path": r["file_path"], "line": r["start_line"]}
            for r in conn.execute(
                "SELECT s.* FROM raw_refs r JOIN symbols s ON s.id = r.owner WHERE r.kind='base' AND r.name=? "
                "ORDER BY s.file_path, s.start_line LIMIT 40", (name,))
        ]
        refs = []
        for r in conn.execute(
            "SELECT r.file_path, r.line, r.owner, s.qualified_name AS user_symbol FROM raw_refs r "
            "LEFT JOIN symbols s ON s.id = r.owner WHERE r.kind='ident' AND r.name=? "
            "ORDER BY r.file_path, r.line LIMIT 200", (name,)):
            if r["file_path"] == d["file_path"] and d["start_line"] <= r["line"] <= d["end_line"]:
                continue  # inside the definition itself
            refs.append({"file_path": r["file_path"], "line": r["line"], "in_symbol": r["user_symbol"]})
        up = {"inheritors_or_implementers": inheritors, "referenced_by": refs[:60], "referenced_by_total": len(refs)}
        if d["symbol_type"] in _TYPE_KINDS:
            up["instantiated_by"] = self._callers(
                "c.callee_id IN (SELECT id FROM symbols WHERE id=? OR (parent_id=? AND symbol_type='constructor'))",
                (d["id"], d["id"]))[0]
        return up

    _METHOD_KINDS = ("method", "constructor", "operator")
    _SQL_KINDS = ("procedure", "view", "function", "table")

    def _callers(self, where: str, params: tuple, limit: int = 200) -> tuple[list[dict], int]:
        order = "CASE c.confidence WHEN 'confirmed' THEN 0 WHEN 'probable' THEN 1 ELSE 2 END, c.file_path, c.line"
        base = f"FROM calls c JOIN symbols s ON s.id = c.caller_id WHERE {where}"
        total = self.conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT s.qualified_name, s.symbol_type, c.file_path, c.line, c.confidence, c.reason {base} "
            f"ORDER BY {order} LIMIT ?", (*params, limit)).fetchall()
        return ([{"symbol": r["qualified_name"], "type": r["symbol_type"], "file_path": r["file_path"],
                  "line": r["line"], "confidence": r["confidence"], "reason": r["reason"]} for r in rows], total)

    def _upstream(self, d: sqlite3.Row) -> dict:
        kind = d["symbol_type"]
        if kind in self._METHOD_KINDS:
            callers, total = self._callers("c.callee_id=?", (d["id"],))
            impls = []
            parent = self.conn.execute("SELECT * FROM symbols WHERE id=?", (d["parent_id"],)).fetchone() if d["parent_id"] else None
            if parent is not None and parent["symbol_type"] == "interface":
                impls = [{"symbol": r["qualified_name"], "file_path": r["file_path"], "line": r["start_line"]}
                         for r in self.conn.execute(
                    "SELECT m.* FROM raw_refs r JOIN symbols m ON m.parent_id = r.owner "
                    "WHERE r.kind='base' AND r.name=? AND m.symbol_name=? AND m.symbol_type='method' "
                    "ORDER BY m.file_path, m.start_line", (parent["symbol_name"], d["symbol_name"]))]
            return {"callers": callers, "callers_total": total, "implementations": impls,
                    "inheritors_or_implementers": [], "referenced_by": [], "referenced_by_total": 0}
        if kind in self._SQL_KINDS:
            res = dataflow.data_access(self.conn, d["symbol_name"])
            callers = []
            for label, key in (("writes", "writers"), ("reads", "readers"), ("executes", "executed_by"), ("maps", "mapped_by")):
                for e in res.get(key, []):
                    callers.append({"symbol": e["source"], "type": e["source_type"], "file_path": e["file_path"],
                                    "line": e["line"], "confidence": e["confidence"],
                                    "reason": f'{label} ({e["op"]}, {e["via"]})' + (
                                        f' via {" > ".join(e["via_chain"])}' if e["via_chain"] else "")})
            return {"callers": callers[:200], "callers_total": len(callers), "implementations": [],
                    "inheritors_or_implementers": [], "referenced_by": [], "referenced_by_total": 0}
        return self._type_upstream(d)

    def _downstream(self, d: sqlite3.Row, type_defs: dict) -> dict:
        kind = d["symbol_type"]
        conn = self.conn
        if kind in self._METHOD_KINDS:
            order = "CASE c.confidence WHEN 'confirmed' THEN 0 WHEN 'probable' THEN 1 ELSE 2 END"
            calls = [{"symbol": r["qualified_name"], "type": r["symbol_type"], "file_path": r["file_path"],
                      "line": r["line"], "callee_line": r["start_line"], "confidence": r["confidence"],
                      "reason": r["reason"]}
                     for r in conn.execute(
                "SELECT s.qualified_name, s.symbol_type, s.file_path, s.start_line, c.line, c.confidence, c.reason "
                f"FROM calls c JOIN symbols s ON s.id = c.callee_id WHERE c.caller_id=? ORDER BY c.line, {order}",
                (d["id"],))]
            down = self._type_downstream(d, type_defs)
            down["calls"] = calls
            down["data_access"] = self._own_data_access(d["id"])
            return down
        if kind in ("procedure", "view", "function"):
            res = dataflow.data_access(conn, d["symbol_name"])
            t = res.get("touches", {})
            down = {"base_types": [], "uses_types": [], "calls": [], "data_access": []}
            for key in ("writes", "reads", "executes", "dynamic_sql"):
                for e in t.get(key, []):
                    down["data_access"].append({
                        "target": e["target"], "access": e["access"], "op": e["op"], "via": e["via"],
                        "confidence": e["confidence"], "line": e["line"], "file_path": e["file_path"],
                        "via_chain": e["via_chain"]})
            return down
        return self._type_downstream(d, type_defs)

    def _own_data_access(self, symbol_id: str) -> list[dict]:
        return [{"target": r["target_name"], "access": r["access"], "op": r["op"], "via": r["via"],
                 "confidence": r["confidence"], "line": r["line"], "file_path": r["file_path"], "via_chain": []}
                for r in self.conn.execute(
            "SELECT * FROM data_access WHERE source_id=? ORDER BY line", (symbol_id,))]

    def _type_downstream(self, d: sqlite3.Row, type_defs: dict) -> dict:
        conn = self.conn
        bases = []
        for r in conn.execute("SELECT name FROM raw_refs WHERE kind='base' AND owner=?", (d["id"],)):
            targets = [{"file_path": t["file_path"], "line": t["start_line"], "type": t["symbol_type"]}
                       for t in type_defs.get(r["name"], [])]
            bases.append({"name": r["name"], "defined_in": targets})
        used: dict[str, dict] = {}
        for r in conn.execute(
            "SELECT name, MIN(line) AS line FROM raw_refs WHERE kind='ident' AND file_path=? "
            "AND line BETWEEN ? AND ? GROUP BY name", (d["file_path"], d["start_line"], d["end_line"])):
            nm = r["name"]
            if nm == d["symbol_name"] or nm not in type_defs:
                continue
            used[nm] = {
                "name": nm, "first_line": r["line"],
                "defined_in": [{"file_path": t["file_path"], "line": t["start_line"], "type": t["symbol_type"]}
                               for t in type_defs[nm][:5]],
            }
        return {"base_types": bases, "uses_types": sorted(used.values(), key=lambda u: u["first_line"])[:60]}

