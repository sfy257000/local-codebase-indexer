"""Symbol-level call resolution for C#: match every recorded call site to method definitions.

Confidence levels
  confirmed  the receiver's type is known and exactly one method matches name and argument count
  probable   the receiver's type is known but several overloads match, or the call goes through an interface
             that has a single implementation, or the receiver is unknown but the method name is unique
  candidate  the receiver's type is unknown and several methods share the name (or an interface has several
             implementations)

A receiver whose type is known but is not defined in the index (``List<T>``, ``IDbConnection``, ...) produces no
edge at all. That is what removes the false matches a name-only search produces. What this cannot do: overload
resolution by argument type, calls through delegates or lambdas, extension methods, and member access chains
longer than one call.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

CONF_ORDER = {"confirmed": 0, "probable": 1, "candidate": 2}
_CLASS_KINDS = {"class", "interface", "struct", "record", "enum"}
_CALLABLE = {"method", "operator"}
_MAX_UNKNOWN_CANDIDATES = 8


class _Graph:
    def __init__(self, conn: sqlite3.Connection):
        self.sym: dict[str, sqlite3.Row] = {}
        self.types_by_name: dict[str, list] = defaultdict(list)
        self.members: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.methods_by_name: dict[str, list] = defaultdict(list)
        self.file_ns: dict[str, set[str]] = defaultdict(set)
        for r in conn.execute(
            "SELECT s.id, s.file_path, s.symbol_name, s.symbol_type, s.parent_id, s.qualified_name, s.type_name, "
            "s.min_args, s.max_args FROM symbols s JOIN files f ON f.file_path = s.file_path "
            "WHERE f.language='csharp'"
        ):
            self.sym[r["id"]] = r
            t = r["symbol_type"]
            if t in _CLASS_KINDS:
                self.types_by_name[r["symbol_name"]].append(r)
            elif t == "namespace":
                self.file_ns[r["file_path"]].add(r["qualified_name"] or r["symbol_name"])
            if r["parent_id"]:
                self.members[r["parent_id"]][r["symbol_name"]].append(r)
            if t in _CALLABLE:
                self.methods_by_name[r["symbol_name"]].append(r)
        for r in conn.execute(
            "SELECT r.file_path, r.name FROM raw_refs r JOIN files f ON f.file_path = r.file_path "
            "WHERE r.kind='import' AND f.language='csharp'"
        ):
            self.file_ns[r["file_path"]].add(r["name"])
        self.bases: dict[str, list[str]] = defaultdict(list)
        for r in conn.execute(
            "SELECT r.owner, r.name FROM raw_refs r WHERE r.kind='base' AND r.owner != ''"
        ):
            if r["owner"] in self.sym:
                self.bases[r["owner"]].append(r["name"])
        self._anc: dict[str, list[str]] = {}
        self._desc: dict[str, list[str]] | None = None

    # ------------------------------------------------------------------ type helpers
    def ns_of(self, row) -> str:
        while row["parent_id"] and row["parent_id"] in self.sym:
            row = self.sym[row["parent_id"]]
        q = row["qualified_name"] or ""
        return q.rsplit(".", 1)[0] if "." in q else ""

    def pick_types(self, name: str, file_path: str) -> list:
        cands = self.types_by_name.get(name, [])
        if len(cands) > 1:
            vis = self.file_ns.get(file_path, set())
            visible = [c for c in cands if self.ns_of(c) in vis]
            if visible:
                return visible
        return cands

    def ancestors(self, cid: str) -> list[str]:
        if cid in self._anc:
            return self._anc[cid]
        self._anc[cid] = []  # cycle guard
        out, queue, seen = [], list(self.bases.get(cid, [])), {cid}
        file_path = self.sym[cid]["file_path"]
        depth = 0
        while queue and depth < 12:
            nxt = []
            for name in queue:
                for t in self.pick_types(name, file_path):
                    if t["id"] not in seen:
                        seen.add(t["id"])
                        out.append(t["id"])
                        nxt.extend(self.bases.get(t["id"], []))
            queue, depth = nxt, depth + 1
        self._anc[cid] = out
        return out

    def implementers(self, iface_id: str) -> list[str]:
        if self._desc is None:
            self._desc = defaultdict(list)
            for cid, row in self.sym.items():
                if row["symbol_type"] in _CLASS_KINDS:
                    for a in self.ancestors(cid):
                        self._desc[a].append(cid)
        return [c for c in self._desc.get(iface_id, []) if self.sym[c]["symbol_type"] != "interface"]

    def enclosing_class(self, row):
        while row is not None:
            if row["symbol_type"] in _CLASS_KINDS:
                return row
            row = self.sym.get(row["parent_id"]) if row["parent_id"] else None
        return None

    def member_type(self, cls_id: str, name: str) -> str:
        for c in [cls_id] + self.ancestors(cls_id):
            types = {m["type_name"] for m in self.members[c].get(name, ())
                     if m["symbol_type"] in ("field", "property") and m["type_name"]}
            if len(types) == 1:
                return next(iter(types))
        return ""

    # ------------------------------------------------------------------ method lookup
    @staticmethod
    def _fits(m, argc: int) -> bool:
        return m["min_args"] is None or m["min_args"] < 0 or m["min_args"] <= argc <= m["max_args"]

    def find_methods(self, class_ids: list[str], name: str, argc: int, ctor: bool = False):
        """(methods, arity_ok). Searches each class then its ancestors, stopping at the first class that has the name."""
        found: dict[str, object] = {}
        for cid in class_ids:
            chain = [cid] if ctor else [cid] + self.ancestors(cid)
            kinds = ("constructor",) if ctor else tuple(_CALLABLE)
            for c in chain:
                ms = [m for m in self.members[c].get(name, ()) if m["symbol_type"] in kinds]
                if ms:
                    for m in ms:
                        found[m["id"]] = m
                    break
        allm = list(found.values())
        fitting = [m for m in allm if self._fits(m, argc)]
        return (fitting, True) if fitting else (allm, False)


def _decode_expr(expr: str):
    kind, name, method, argc, rtype = (expr.split("|") + [""] * 5)[:5]
    return kind, name, method, int(argc or 0), rtype


def resolve_calls(conn: sqlite3.Connection) -> None:
    """Rebuild the ``calls`` table from ``call_sites`` and ``symbols``."""
    conn.execute("DELETE FROM calls")
    g = _Graph(conn)
    edges: dict[tuple, tuple] = {}

    def add(caller, callee, file_path, line, conf, reason):
        key = (caller, callee["id"], line)
        old = edges.get(key)
        if old is None or CONF_ORDER[conf] < CONF_ORDER[old[0]]:
            edges[key] = (conf, reason, file_path)

    def receiver_classes(site, caller_row, ctx_class, depth: int = 0):
        """(class rows, how, known). ``known`` is False when the receiver's type could not be determined;
        an empty class list with known=True means the type is external to the index."""
        kind = site["recv_kind"]
        file_path = site["file_path"]
        if kind in ("none", "this"):
            return ([ctx_class], "this" if kind == "this" else "same class", True) if ctx_class is not None else ([], "", False)
        if kind == "base":
            if ctx_class is None:
                return [], "", False
            anc = [g.sym[a] for a in g.ancestors(ctx_class["id"])]
            return anc, "base class", True
        typ = site["recv_type"]
        how = ""
        if kind in ("new", "cast"):
            how = "new/cast type"
        elif kind == "type":
            how = "constructor"
        elif kind in ("name", "member"):
            if typ:
                how = f"local '{site['recv_name']}' is {typ}"
            else:
                if site["recv_expr"] and depth == 0:
                    inner = _decode_expr(site["recv_expr"])
                    rt = resolve_expr_type(inner, site, ctx_class)
                    if rt:
                        typ, how = rt, "return type of earlier call"
                if not typ and ctx_class is not None and site["recv_name"]:
                    typ = g.member_type(ctx_class["id"], site["recv_name"])
                    how = f"'{site['recv_name']}' is declared as {typ}" if typ else ""
                if not typ and kind == "name" and site["recv_name"] in g.types_by_name:
                    return g.pick_types(site["recv_name"], file_path), "static call", True
        elif kind == "expr" and site["recv_expr"] and depth == 0:
            rt = resolve_expr_type(_decode_expr(site["recv_expr"]), site, ctx_class)
            if rt:
                typ, how = rt, "return type of chained call"
        if not typ:
            return [], "", False
        classes = g.pick_types(typ, file_path)
        return classes, how, True

    def resolve_expr_type(inner, site, ctx_class) -> str:
        kind, name, method, argc, rtype = inner
        fake = {"recv_kind": kind, "recv_name": name, "recv_type": rtype, "recv_expr": "", "file_path": site["file_path"]}
        classes, _how, known = receiver_classes(fake, None, ctx_class, depth=1)
        if not known or not classes:
            return ""
        methods, _ok = g.find_methods([c["id"] for c in classes], method, argc)
        types = {m["type_name"] for m in methods if m["type_name"]}
        return next(iter(types)) if len(types) == 1 and len(methods) == 1 else ""

    sites = conn.execute(
        "SELECT c.* FROM call_sites c JOIN files f ON f.file_path = c.file_path WHERE f.language='csharp'"
    ).fetchall()
    for site in sites:
        caller_id = site["caller_id"]
        if not caller_id or caller_id not in g.sym:
            continue
        caller = g.sym[caller_id]
        ctx = g.enclosing_class(caller)
        file_path, line, name, argc = site["file_path"], site["line"], site["name"], site["argc"]

        if site["is_ctor"]:
            for cls in g.pick_types(name, file_path):
                ctors, ok = g.find_methods([cls["id"]], cls["symbol_name"], argc, ctor=True)
                if len(ctors) == 1:
                    add(caller_id, ctors[0], file_path, line, "confirmed" if ok else "probable", "constructor call")
                elif ctors:
                    for m in ctors:
                        add(caller_id, m, file_path, line, "probable", "constructor overload")
                else:
                    add(caller_id, cls, file_path, line, "confirmed", "instantiates class")
            continue

        classes, how, known = receiver_classes(site, caller, ctx)
        if known:
            if not classes:
                continue  # type outside the index
            methods, arity_ok = g.find_methods([c["id"] for c in classes], name, argc)
            if not methods:
                continue
            if len(methods) == 1 and arity_ok and len(classes) == 1:
                add(caller_id, methods[0], file_path, line, "confirmed", how)
            else:
                for m in methods:
                    add(caller_id, m, file_path, line, "probable",
                        f"{how}; {len(methods)} overloads or types match" if how else "several matches")
            for m in methods:
                owner = g.sym.get(m["parent_id"]) if m["parent_id"] else None
                if owner is None or owner["symbol_type"] != "interface":
                    continue
                impls = []
                for cid in g.implementers(owner["id"]):
                    ims, _ = g.find_methods([cid], name, argc)
                    impls.extend(ims)
                if len(impls) == 1:
                    add(caller_id, impls[0], file_path, line, "probable",
                        f"only implementation of {owner['symbol_name']}.{name}")
                else:
                    for im in impls:
                        add(caller_id, im, file_path, line, "candidate",
                            f"one of {len(impls)} implementations of {owner['symbol_name']}.{name}")
            continue

        # receiver type unknown: fall back to the method name, labelled as such
        named = [m for m in g.methods_by_name.get(name, ()) if g._fits(m, argc)]
        if not named or len(named) > _MAX_UNKNOWN_CANDIDATES:
            continue
        if len(named) == 1:
            add(caller_id, named[0], file_path, line, "probable", "receiver type unknown; method name is unique")
        else:
            for m in named:
                add(caller_id, m, file_path, line, "candidate",
                    f"receiver type unknown; {len(named)} methods share the name")

    conn.executemany(
        "INSERT INTO calls(caller_id, callee_id, file_path, line, confidence, reason) VALUES(?,?,?,?,?,?)",
        [(k[0], k[1], v[2], k[2], v[0], v[1]) for k, v in sorted(edges.items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][1]))],
    )
