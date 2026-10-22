"""Data access found in C# code: SQL strings, stored procedure calls (Dapper / ADO.NET) and EF table mappings.

Strings are evaluated statically: literals, ``+`` concatenation, interpolation, ``string.Format``, constants and
local variables (including ``sql += ...``). Parts that cannot be evaluated become holes. A SQL string handed to a
known database API is ``confirmed`` when it is fully static and ``probable`` otherwise; a SQL-looking string that
is not provably executed is ``probable``; a table name that is itself a hole, or an execution site whose SQL could
not be determined at all, is ``uncertain``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .sqlaccess import HOLE, Access, extract_sql_accesses

_STRINGISH = {"string_literal", "verbatim_string_literal", "raw_string_literal", "interpolated_string_expression"}
_SCOPES = {
    "method_declaration", "constructor_declaration", "destructor_declaration", "operator_declaration",
    "indexer_declaration", "property_declaration",
}
_SINK_METHOD = re.compile(r"^(Execute\w*|Query\w*|FromSql\w*|SqlQuery\w*|Fill)$")
_DAPPER_STRICT = re.compile(r"^(Execute|Query)\w*$")
_COMMAND_TYPE = re.compile(r"(Command|DataAdapter)$")
_PROC_NAME = re.compile(r"^[A-Za-z_][\w$]*(?:\.[\w$]+){0,2}$")
_NOT_PROC = {"select", "update", "delete", "insert", "exec", "execute", "merge", "truncate"}
_STATEMENT_SHAPES = [
    re.compile(r"^\s*select\b[\s\S]+\bfrom\b\s+\S", re.I),
    re.compile(r"^\s*insert\s+(?:into\s+)?\S+[\s\S]*\b(?:values|select|exec(?:ute)?)\b", re.I),
    re.compile(r"^\s*update\s+\S+\s+set\b", re.I),
    re.compile(r"^\s*delete\s+(?:from\s+)?\S+[\s\S]*(?:\bwhere\b|@\w)", re.I),
    re.compile(r"^\s*merge\b[\s\S]+\busing\b", re.I),
    re.compile(r"^\s*truncate\s+table\s+\S+", re.I),
    re.compile(r"^\s*exec(?:ute)?\s+[\w.\[\]]*[_.@][\w.\[\]]*", re.I),
    re.compile(r"^\s*with\s+\w+\s+as\s*\([\s\S]+\bselect\b", re.I),
]


@dataclass
class _Cand:
    text: str
    complete: bool
    line: int
    sink: bool = False
    proc_ok: bool = False  # may be read as a stored procedure name


def looks_like_statement(text: str) -> bool:
    return any(rx.search(text) for rx in _STATEMENT_SHAPES)


def _walk(node, type_name):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == type_name:
            yield n
        stack.extend(reversed(n.children))


class _Extractor:
    def __init__(self, src: bytes, symbols: list):
        self.src = src
        self.out: list[Access] = []
        spans = [(s.end - s.start, s) for s in symbols if s.type != "namespace"]
        spans.sort(key=lambda t: (t[0], t[1].start))
        self.spans = [s for _, s in spans]
        self.file_env: dict[str, _Cand] = {}
        self.cands: list[_Cand] = []

    # ---------------------------------------------------------------- helpers
    def text(self, n) -> str:
        return self.src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

    def locate(self, line: int):
        for s in self.spans:
            if s.start <= line <= s.end:
                return s
        return None

    @staticmethod
    def line(n) -> int:
        return n.start_point[0] + 1

    def collapsed(self, n) -> str:
        return " ".join(self.text(n).split())[:160]

    def method_name(self, func) -> str | None:
        if func is None:
            return None
        t = func.type
        if t == "identifier":
            return self.text(func)
        if t == "generic_name":
            for c in func.children:
                if c.type == "identifier":
                    return self.text(c)
        if t == "member_access_expression":
            return self.method_name(func.child_by_field_name("name"))
        if t == "conditional_access_expression":
            for c in func.children:
                if c.type == "member_binding_expression":
                    for cc in c.children:
                        if cc.type in ("identifier", "generic_name"):
                            return self.method_name(cc)
        return None

    @staticmethod
    def arg_exprs(arglist) -> list:
        """[(expression node, is_named_argument)] for an argument_list node."""
        out = []
        if arglist is None:
            return out
        for a in arglist.named_children:
            if a.type != "argument":
                continue
            named = list(a.named_children)
            out.append((named[-1] if named else None, len(named) > 1))
        return out

    # ---------------------------------------------------------------- string evaluation
    def evaluate(self, n, env) -> tuple[str, bool] | None:
        t = n.type
        if t == "string_literal":
            raw = self.text(n)
            if raw.startswith('"""'):
                return raw.strip('"'), True
            body = raw[1:-1] if len(raw) >= 2 else raw
            body = (body.replace('\\"', '"').replace("\\n", "\n").replace("\\r", " ")
                    .replace("\\t", " ").replace("\\\\", "\\"))
            return body, True
        if t == "verbatim_string_literal":
            raw = self.text(n)
            return (raw[2:-1].replace('""', '"') if len(raw) >= 3 else raw), True
        if t == "raw_string_literal":
            return self.text(n).strip('"'), True
        if t == "interpolated_string_expression":
            parts, complete = [], True
            for c in n.children:
                if c.type == "interpolation":
                    parts.append(HOLE)
                    complete = False
                elif c.type in ("string_content", "string_literal_content", "interpolated_string_text",
                                "interpolated_verbatim_string_text"):
                    parts.append(self.text(c).replace("{{", "{").replace("}}", "}"))
            return "".join(parts), complete
        if t == "parenthesized_expression":
            inner = list(n.named_children)
            return self.evaluate(inner[0], env) if inner else None
        if t == "binary_expression" and any(c.type == "+" for c in n.children):
            kids = n.named_children
            if len(kids) != 2:
                return None
            left, right = self.evaluate(kids[0], env), self.evaluate(kids[1], env)
            if left is None and right is None:
                return None
            lt, lc = left if left else (HOLE, False)
            rt, rc = right if right else (HOLE, False)
            return lt + rt, lc and rc
        if t == "identifier":
            c = env.get(self.text(n))
            return (c.text, c.complete) if c else None
        if t == "member_access_expression":
            name = n.child_by_field_name("name")
            c = self.file_env.get(self.text(name)) if name is not None else None
            return (c.text, c.complete) if c else None
        if t == "invocation_expression":
            func = n.child_by_field_name("function")
            if self.method_name(func) in ("Format", "Concat") and func is not None and "string" in self.text(func).lower():
                args = self.arg_exprs(n.child_by_field_name("arguments"))
                if args and args[0][0] is not None:
                    first = self.evaluate(args[0][0], env)
                    if first is not None:
                        return re.sub(r"\{\d+[^}]*\}", HOLE, first[0]), False
        return None

    def _is_stringy(self, n) -> bool:
        if n.type in _STRINGISH:
            return True
        if n.type == "binary_expression" and any(c.type == "+" for c in n.children):
            return any(self._is_stringy(c) for c in n.named_children)
        if n.type == "parenthesized_expression":
            return any(self._is_stringy(c) for c in n.named_children)
        return False

    # ---------------------------------------------------------------- traversal
    def run(self, root) -> list[Access]:
        self._collect_file_consts(root)
        env: dict[str, _Cand] = dict(self.file_env)
        stack: list = [("node", root)]
        while stack:
            kind, n = stack.pop()
            if kind == "exit":
                mark, scope_node, saved_env = n
                self._flush(mark, scope_node)
                env = saved_env
                continue
            if n.type in _SCOPES:
                stack.append(("exit", (len(self.cands), n, env)))
                env = dict(self.file_env)
                for c in reversed(n.children):
                    stack.append(("node", c))
                continue
            if self._visit(n, env, stack):
                continue
            for c in reversed(n.children):
                stack.append(("node", c))
        self._flush(0, root)
        return self.out

    def _collect_file_consts(self, root) -> None:
        stack = [root]
        while stack:
            n = stack.pop()
            if n.type == "field_declaration":
                for d in _walk(n, "variable_declarator"):
                    ident, init = self._declarator(d)
                    if ident and init is not None and self._is_stringy(init):
                        r = self.evaluate(init, self.file_env)
                        if r:
                            c = _Cand(r[0], r[1], self.line(init))
                            self.file_env[ident] = c
                            self.cands.append(c)
                continue
            if n.type in _SCOPES:
                continue
            stack.extend(n.children)

    def _declarator(self, d):
        ident, init = None, None
        for c in d.named_children:
            if c.type == "identifier" and ident is None:
                ident = self.text(c)
            else:
                init = c
        return ident, init

    def _visit(self, n, env, stack) -> bool:
        """Handle nodes of interest. Returns True when the node's children must not be traversed."""
        t = n.type
        if t == "variable_declarator":
            ident, init = self._declarator(n)
            if ident and init is not None and self._is_stringy(init):
                r = self.evaluate(init, env)
                if r:
                    c = _Cand(r[0], r[1], self.line(init))
                    env[ident] = c
                    self.cands.append(c)
                    return True
            return False
        if t == "assignment_expression":
            return self._assignment(n, env, stack)
        if t == "invocation_expression":
            return self._invocation(n, env, stack)
        if t == "object_creation_expression":
            return self._creation(n, env, stack)
        if t == "attribute":
            self._attribute(n)
            return False
        if self._is_stringy(n):
            r = self.evaluate(n, env)
            if r:
                self.cands.append(_Cand(r[0], r[1], self.line(n)))
            return True
        return False

    def _assignment(self, n, env, stack) -> bool:
        kids = n.named_children
        if len(kids) != 2:
            return False
        left, right = kids
        op = next((c.type for c in n.children if c.type in ("=", "+=")), "=")
        if left.type == "member_access_expression" and self.text(left.child_by_field_name("name")) == "CommandText":
            if not self._sink_arg(right, env, stack, first=True):
                if not self._is_stringy(right):
                    self._dynamic_site(n)
            stack.append(("node", left))
            return True
        if left.type == "identifier" and self._is_stringy(right):
            name = self.text(left)
            r = self.evaluate(right, env)
            if r is None:
                return False
            cur = env.get(name)
            if op == "+=" and cur is not None:
                cur.text += r[0]
                cur.complete = cur.complete and r[1]
            elif cur is not None and re.search(rf"\b{re.escape(name)}\b", self.text(right)):
                cur.text, cur.complete = r  # sql = sql + "..."
            else:
                c = _Cand(r[0], r[1], self.line(right))
                env[name] = c
                self.cands.append(c)
            return True
        return False

    def _sink_arg(self, expr, env, stack, first: bool) -> bool:
        """An argument handed to a database API. True when it was a statically known string."""
        if expr is None:
            return False
        if expr.type == "identifier":
            c = env.get(self.text(expr))
            if c is not None:
                c.sink = True
                c.proc_ok = c.proc_ok or first
                return True
        elif self._is_stringy(expr) or expr.type in ("member_access_expression", "invocation_expression"):
            r = self.evaluate(expr, env)
            if r is not None:
                self.cands.append(_Cand(r[0], r[1], self.line(expr), sink=True, proc_ok=first))
                return True
        stack.append(("node", expr))
        return False

    def _invocation(self, n, env, stack) -> bool:
        func = n.child_by_field_name("function")
        name = self.method_name(func)
        args = self.arg_exprs(n.child_by_field_name("arguments"))
        if name in ("ToTable", "ToView") and args and args[0][0] is not None and self._is_stringy(args[0][0]):
            r = self.evaluate(args[0][0], env)
            if r and r[1]:
                self._mapping(r[0], n, func)
        if name and _SINK_METHOD.match(name):
            handled_any = False
            for i, (expr, _named) in enumerate(args):
                handled = self._sink_arg(expr, env, stack, first=not handled_any)
                handled_any = handled_any or handled
            if not handled_any and _DAPPER_STRICT.match(name) and args and args[0][0] is not None:
                second = args[1][0] if len(args) > 1 else None
                has_params = second is not None and second.type in (
                    "anonymous_object_creation_expression", "object_creation_expression", "identifier")
                if has_params or any(named for _e, named in args):
                    self._dynamic_site(n)
            if func is not None:
                stack.append(("node", func))
            return True
        return False

    def _creation(self, n, env, stack) -> bool:
        tnode = n.child_by_field_name("type")
        tname = self.text(tnode) if tnode is not None else ""
        if not _COMMAND_TYPE.search(tname.split(".")[-1].split("<")[0]):
            return False
        args = self.arg_exprs(n.child_by_field_name("arguments"))
        if args and args[0][0] is not None:
            if not self._sink_arg(args[0][0], env, stack, first=True):
                self._dynamic_site(n)
        for expr, _named in args[1:]:
            if expr is not None:
                stack.append(("node", expr))
        for c in n.children:
            if c.type == "initializer_expression":
                stack.append(("node", c))
        return True

    def _dynamic_site(self, n) -> None:
        self.out.append(Access(
            key="?", name="(dynamic)", qualifier="", kind="unknown", access="exec", op="dynamic",
            line=self.line(n), via="csharp_sql", confidence="uncertain", evidence=self.collapsed(n),
        ))

    # ---------------------------------------------------------------- mappings
    def _map_access(self, table: str, line: int, evidence: str, entity: str) -> None:
        parts = [p.strip("[] ") for p in table.split(".")]
        self.out.append(Access(
            key=parts[-1].lower(), name=parts[-1], qualifier=".".join(parts[:-1]), kind="table", access="map",
            op="map", line=line, via="ef_mapping", confidence="confirmed", evidence=evidence, detail=entity,
        ))

    def _mapping(self, table: str, n, func) -> None:
        entity = ""
        node = func.child_by_field_name("expression") if func is not None and func.type == "member_access_expression" else None
        while node is not None and node.type == "invocation_expression":
            f = node.child_by_field_name("function")
            if f is None or f.type != "member_access_expression":
                break
            nm = f.child_by_field_name("name")
            if nm is not None and nm.type == "generic_name" and self.method_name(nm) == "Entity":
                targs = [c for c in nm.children if c.type == "type_argument_list"]
                if targs and targs[0].named_children:
                    entity = self.text(targs[0].named_children[0])
                break
            node = f.child_by_field_name("expression")
        self._map_access(table, self.line(n), self.collapsed(n), entity)

    def _attribute(self, n) -> None:
        name = next((self.text(c) for c in n.named_children if c.type in ("identifier", "qualified_name")), "")
        if name.split(".")[-1] not in ("Table", "TableAttribute"):
            return
        arglist = next((c for c in n.named_children if c.type == "attribute_argument_list"), None)
        if arglist is None or not arglist.named_children:
            return
        first = list(arglist.named_children[0].named_children)
        if not first or first[0].type not in _STRINGISH:
            return
        r = self.evaluate(first[0], {})
        if not r or not r[0]:
            return
        cls = n.parent.parent if n.parent is not None else None
        entity = ""
        if cls is not None:
            entity = next((self.text(c) for c in cls.named_children if c.type == "identifier"), "")
        self._map_access(r[0], self.line(n), self.collapsed(n), entity)

    # ---------------------------------------------------------------- emission
    def _flush(self, mark: int, scope_node) -> None:
        cands, self.cands = self.cands[mark:], self.cands[:mark]
        has_sp = (scope_node.type in _SCOPES
                  and b"StoredProcedure" in self.src[scope_node.start_byte:scope_node.end_byte])
        for c in cands:
            text = c.text.strip()
            if not text:
                continue
            collapsed = " ".join(text.split())[:160]
            if (c.sink and c.proc_ok and c.complete and "\n" not in text and _PROC_NAME.match(text)
                    and text.lower() not in _NOT_PROC):
                parts = text.split(".")
                self.out.append(Access(
                    key=parts[-1].lower(), name=parts[-1], qualifier=".".join(parts[:-1]), kind="procedure",
                    access="exec", op="exec", line=c.line, via="csharp_sp",
                    confidence="confirmed" if has_sp else "probable", evidence=collapsed,
                ))
                continue
            if not (c.sink or looks_like_statement(text)):
                continue
            for a in extract_sql_accesses(text, c.line):
                a.line = c.line
                a.via = "csharp_sql"
                a.evidence = collapsed
                if a.confidence != "uncertain":
                    a.confidence = "confirmed" if (c.sink and c.complete) else "probable"
                self.out.append(a)


def extract_cs_accesses(root, src: bytes, symbols: list) -> list[Access]:
    ex = _Extractor(src, symbols)
    try:
        accesses = ex.run(root)
    except RecursionError:
        return []
    seen, out = set(), []
    for a in accesses:
        sym = ex.locate(a.line)
        a.source_id = sym.id if sym is not None else None
        sig = (a.source_id, a.key, a.access, a.op, a.line, a.via)
        if sig not in seen:
            seen.add(sig)
            out.append(a)
    out.sort(key=lambda a: (a.line, a.access, a.key))
    return out
