"""C# call sites with a light local type environment, for symbol-level call resolution.

Only facts that the syntax tree states are recorded here (receiver kind and name, declared types of
parameters and locals, ``var x = new T()`` / casts / ``as``). Matching a call to a method definition happens
later in ``callgraph.py``, once every file is indexed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SCOPES = {
    "method_declaration", "constructor_declaration", "destructor_declaration", "operator_declaration",
    "indexer_declaration", "property_declaration",
}
_TASK = re.compile(r"^(?:[\w.]+\.)?(?:Task|ValueTask)\s*<(.+)>$")
_OUTER = re.compile(r"^([\w.]+)\s*(?:<.*>)?$", re.S)


@dataclass
class CallSite:
    caller_id: str | None
    line: int
    name: str
    argc: int
    recv_kind: str      # none | this | base | name | member | new | cast | expr | type
    recv_name: str = ""
    recv_type: str = ""   # type known from the syntax (declared type, new T(), cast); "" when unknown
    recv_expr: str = ""   # "kind|name|method|argc|rtype" of the call that produced the receiver
    is_ctor: bool = False


def norm_type(text: str) -> str:
    """Outer type name without namespace, generic arguments, nullability or array brackets.
    ``Task<T>`` / ``ValueTask<T>`` become ``T`` (the type a caller works with after ``await``)."""
    t = (text or "").strip()
    t = re.sub(r"^(?:ref|out|in|readonly|const|params)\s+", "", t)
    t = t.replace("global::", "")
    for _ in range(3):
        m = _TASK.match(t)
        if not m:
            break
        t = m.group(1).strip()
    t = re.sub(r"(?:\[[,\s]*\]|\?)+$", "", t).strip()
    if t.startswith("(") or t in ("var", "dynamic", "object", ""):
        return ""
    m = _OUTER.match(t)
    return m.group(1).split(".")[-1] if m else ""


def _text(n, src: bytes) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")


def member_info(node, src: bytes) -> tuple[str, int, int]:
    """(type name, min args, max args) for a C# member node. Type is the return / declared type; the argument
    counts are -1 for members without a parameter list."""
    t = node.type
    type_name = ""
    if t == "method_declaration":
        r = node.child_by_field_name("returns")
        type_name = norm_type(_text(r, src)) if r is not None else ""
    elif t in ("property_declaration", "indexer_declaration"):
        r = node.child_by_field_name("type")
        type_name = norm_type(_text(r, src)) if r is not None else ""
    elif t in ("field_declaration", "event_field_declaration"):
        for c in node.children:
            if c.type == "variable_declaration":
                r = c.child_by_field_name("type")
                type_name = norm_type(_text(r, src)) if r is not None else ""
    plist = node.child_by_field_name("parameters")
    if plist is None or t not in ("method_declaration", "constructor_declaration", "operator_declaration"):
        return type_name, -1, -1
    fixed = optional = 0
    variadic = any(c.type == "params" or _text(c, src) == "params" for c in plist.children)
    for p in plist.named_children:
        if p.type != "parameter":
            continue
        tn, nm = p.child_by_field_name("type"), p.child_by_field_name("name")
        declared = {(x.start_byte, x.end_byte) for x in (tn, nm) if x is not None}
        has_default = any(c.type == "=" for c in p.children) or any(
            c.is_named and c.type not in ("modifier", "attribute_list") and (c.start_byte, c.end_byte) not in declared
            for c in p.children)
        fixed += 1
        optional += 1 if has_default else 0
    return type_name, fixed - optional, (99 if variadic else fixed)


class _Calls:
    def __init__(self, src: bytes, symbols: list):
        self.src = src
        spans = [(s.end - s.start, s) for s in symbols if s.type != "namespace"]
        spans.sort(key=lambda t: (t[0], t[1].start))
        self.spans = [s for _, s in spans]
        self.out: list[CallSite] = []

    def text(self, n) -> str:
        return _text(n, self.src)

    def locate(self, line: int):
        for s in self.spans:
            if s.start <= line <= s.end:
                return s
        return None

    # ---------------------------------------------------------------- local environment
    def _set(self, env: dict, name: str, typ: str) -> None:
        if name in env and env[name] != typ:
            env[name] = ""  # declared twice with different types: unknown
        else:
            env[name] = typ

    def _unwrap(self, n):
        while n is not None and n.type in ("parenthesized_expression", "await_expression"):
            kids = list(n.named_children)
            n = kids[-1] if kids else None
        return n

    def _infer_initializer(self, init, env) -> str:
        n = self._unwrap(init)
        if n is None:
            return ""
        if n.type == "object_creation_expression":
            t = n.child_by_field_name("type")
            return norm_type(self.text(t)) if t is not None else ""
        if n.type == "cast_expression":
            kids = list(n.named_children)
            return norm_type(self.text(kids[0])) if kids else ""
        if n.type == "as_expression":
            kids = list(n.named_children)
            return norm_type(self.text(kids[-1])) if len(kids) > 1 else ""
        if n.type == "invocation_expression":
            info = self._call_info(n, env, depth=1)
            if info is not None:
                kind, name, recv_type, _expr, method, argc = info
                if kind in ("none", "this", "name", "member"):
                    return f"@call:{kind}|{name}|{method}|{argc}|{recv_type}"
        return ""

    def collect_locals(self, scope) -> dict:
        env: dict[str, str] = {}
        plist = scope.child_by_field_name("parameters")
        if plist is not None:
            for p in plist.named_children:
                if p.type == "parameter":
                    t, nm = p.child_by_field_name("type"), p.child_by_field_name("name")
                    if t is not None and nm is not None:
                        self._set(env, self.text(nm), norm_type(self.text(t)))
        stack = list(scope.children)
        while stack:
            n = stack.pop()
            ty = n.type
            if ty == "variable_declaration":
                tnode = n.child_by_field_name("type")
                implicit = tnode is None or tnode.type == "implicit_type"
                declared = "" if implicit else norm_type(self.text(tnode))
                for d in n.named_children:
                    if d.type != "variable_declarator":
                        continue
                    ident = next((c for c in d.named_children if c.type == "identifier"), None)
                    if ident is None:
                        continue
                    init = None
                    for c in d.named_children:
                        if c.start_byte != ident.start_byte:
                            init = c
                    typ = declared if not implicit else (self._infer_initializer(init, env) if init is not None else "")
                    self._set(env, self.text(ident), typ)
            elif ty == "foreach_statement":
                tnode, left = n.child_by_field_name("type"), n.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    typ = "" if (tnode is None or tnode.type == "implicit_type") else norm_type(self.text(tnode))
                    self._set(env, self.text(left), typ)
            elif ty == "catch_declaration":
                kids = list(n.named_children)
                if len(kids) >= 2 and kids[-1].type == "identifier":
                    self._set(env, self.text(kids[-1]), norm_type(self.text(kids[0])))
            elif ty == "declaration_pattern":
                kids = list(n.named_children)
                if len(kids) >= 2:
                    self._set(env, self.text(kids[-1]), norm_type(self.text(kids[0])))
            stack.extend(n.children)
        return env

    # ---------------------------------------------------------------- call classification
    @staticmethod
    def _method_name(func, text) -> str | None:
        if func is None:
            return None
        t = func.type
        if t == "identifier":
            return text(func)
        if t == "generic_name":
            for c in func.children:
                if c.type == "identifier":
                    return text(c)
        if t == "member_access_expression":
            return _Calls._method_name(func.child_by_field_name("name"), text)
        return None

    def _classify_receiver(self, expr, env, depth: int) -> tuple[str, str, str, str]:
        """(kind, name, type, expr) of a receiver expression."""
        n = self._unwrap(expr)
        if n is None:
            return "expr", "", "", ""
        t = n.type
        if t == "identifier":
            nm = self.text(n)
            typ = env.get(nm, "")
            if typ.startswith("@call:"):
                return "name", nm, "", typ[len("@call:"):]
            return "name", nm, typ, ""
        if t == "this_expression":
            return "this", "", "", ""
        if t == "base_expression":
            return "base", "", "", ""
        if t == "member_access_expression":
            inner = n.child_by_field_name("expression")
            nm = n.child_by_field_name("name")
            if inner is not None and inner.type == "this_expression" and nm is not None:
                return "member", self.text(nm), "", ""
            return "expr", "", "", ""
        if t == "object_creation_expression":
            tn = n.child_by_field_name("type")
            return "new", "", norm_type(self.text(tn)) if tn is not None else "", ""
        if t == "cast_expression":
            kids = list(n.named_children)
            return "cast", "", norm_type(self.text(kids[0])) if kids else "", ""
        if t == "as_expression":
            kids = list(n.named_children)
            return "cast", "", norm_type(self.text(kids[-1])) if len(kids) > 1 else "", ""
        if t == "invocation_expression" and depth < 2:
            info = self._call_info(n, env, depth + 1)
            if info is not None:
                kind, name, recv_type, _expr, method, argc = info
                if kind in ("none", "this", "name", "member"):
                    return "expr", "", "", f"{kind}|{name}|{method}|{argc}|{recv_type}"
        return "expr", "", "", ""

    def _call_info(self, n, env, depth: int):
        """(recv_kind, recv_name, recv_type, recv_expr, method name, argc) of an invocation node."""
        func = n.child_by_field_name("function")
        if func is None:
            return None
        args = n.child_by_field_name("arguments")
        argc = sum(1 for a in args.named_children if a.type == "argument") if args is not None else 0
        name = self._method_name(func, self.text)
        if func.type in ("identifier", "generic_name"):
            return ("none", "", "", "", name, argc) if name else None
        if func.type == "member_access_expression":
            if not name:
                return None
            kind, rname, rtype, rexpr = self._classify_receiver(func.child_by_field_name("expression"), env, depth)
            return kind, rname, rtype, rexpr, name, argc
        if func.type == "conditional_access_expression":
            binding = next((c for c in func.children if c.type == "member_binding_expression"), None)
            if binding is None:
                return None
            ident = next((c for c in binding.children if c.type in ("identifier", "generic_name")), None)
            name = self._method_name(ident, self.text)
            cond = func.child_by_field_name("condition") or (func.named_children[0] if func.named_children else None)
            if not name or cond is None:
                return None
            kind, rname, rtype, rexpr = self._classify_receiver(cond, env, depth)
            return kind, rname, rtype, rexpr, name, argc
        return None

    # ---------------------------------------------------------------- traversal
    def run(self, root) -> list[CallSite]:
        stack: list = [("node", root, {})]
        while stack:
            _kind, n, env = stack.pop()
            if n.type in _SCOPES:
                env = self.collect_locals(n)
            elif n.type == "field_declaration":
                env = {}
            if n.type == "invocation_expression":
                info = self._call_info(n, env, 0)
                if info is not None:
                    kind, rname, rtype, rexpr, method, argc = info
                    self._add(n, method, argc, kind, rname, rtype, rexpr, False)
            elif n.type == "object_creation_expression":
                tn = n.child_by_field_name("type")
                tname = norm_type(self.text(tn)) if tn is not None else ""
                if tname:
                    args = n.child_by_field_name("arguments")
                    argc = sum(1 for a in args.named_children if a.type == "argument") if args is not None else 0
                    self._add(n, tname, argc, "type", tname, "", "", True)
            for c in reversed(n.children):
                stack.append(("node", c, env))
        return self.out

    def _add(self, n, name, argc, kind, rname, rtype, rexpr, is_ctor) -> None:
        line = n.start_point[0] + 1
        sym = self.locate(line)
        self.out.append(CallSite(sym.id if sym is not None else None, line, name, argc, kind, rname, rtype, rexpr, is_ctor))


def extract_call_sites(root, src: bytes, symbols: list) -> list[CallSite]:
    try:
        return _Calls(src, symbols).run(root)
    except RecursionError:
        return []
