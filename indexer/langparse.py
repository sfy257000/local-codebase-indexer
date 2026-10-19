"""Syntactic layer: tree-sitter parsing into symbols, references and chunks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Language, Parser

MAX_CHUNK_LINES = 80
SPLIT_LINES = 60
SPLIT_OVERLAP = 8
WINDOW_LINES = 50
WINDOW_OVERLAP = 5

CLASS_LIKE = {"class", "interface", "struct", "record", "enum", "type"}
LEAF_TYPES = {"method", "constructor", "destructor", "property", "function", "indexer", "operator", "delegate", "event"}


@dataclass
class Symbol:
    id: str
    name: str
    qname: str
    type: str
    parent_id: str | None
    start: int
    end: int
    signature: str


@dataclass
class Ref:
    kind: str  # import | base | ident
    name: str
    line: int
    owner: str = ""


@dataclass
class Chunk:
    start: int
    end: int
    text: str
    symbol_id: str | None
    header: str  # "<type> <qualified name>\n<signature>" used for FTS/embedding


@dataclass
class ParseResult:
    symbols: list[Symbol] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    parsed_with: str = "none"


# --------------------------------------------------------------------------- grammars

_LANG_CACHE: dict[str, Parser | None] = {}


def _load_parser(language: str) -> Parser | None:
    if language in _LANG_CACHE:
        return _LANG_CACHE[language]
    parser = None
    try:
        if language == "csharp":
            import tree_sitter_c_sharp as m
            lang = Language(m.language())
        elif language == "python":
            import tree_sitter_python as m
            lang = Language(m.language())
        elif language == "javascript":
            import tree_sitter_javascript as m
            lang = Language(m.language())
        elif language == "typescript":
            import tree_sitter_typescript as m
            lang = Language(m.language_typescript())
        elif language == "tsx":
            import tree_sitter_typescript as m
            lang = Language(m.language_tsx())
        else:
            lang = None
        if lang is not None:
            parser = Parser(lang)
    except Exception:  # grammar package missing -> regex fallback
        parser = None
    _LANG_CACHE[language] = parser
    return parser


# --------------------------------------------------------------------------- helpers

def normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _node_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _end_line(node) -> int:
    row, col = node.end_point
    return row if col == 0 and row > node.start_point[0] else row + 1


_ATTR_PREFIX = re.compile(r"^(?:\s*\[[^\]]*\]\s*)+")


def _signature(node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else None
    text = src[node.start_byte:end if end is not None else node.end_byte].decode("utf-8", errors="replace")
    if body is None:
        # properties / fields: cut at first '{' or '=>' or end of first line
        text = re.split(r"\{|=>|\n", text, maxsplit=1)[0]
    text = _ATTR_PREFIX.sub("", text)
    text = " ".join(text.split())
    return text[:300]


def _first_identifier(node, src: bytes) -> str | None:
    name = node.child_by_field_name("name")
    if name is not None:
        return _node_text(name, src)
    for ch in node.children:
        if ch.type in ("identifier", "type_identifier", "property_identifier"):
            return _node_text(ch, src)
    return None


def _field_name_csharp(node, src: bytes) -> str | None:
    for ch in node.children:
        if ch.type == "variable_declaration":
            for d in ch.children:
                if d.type == "variable_declarator":
                    return _first_identifier(d, src)
    return _first_identifier(node, src)


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HERITAGE_SKIP = {"extends", "implements", "where", "new", "class", "struct", "notnull", "unmanaged"}


def _heritage_names(text: str) -> list[str]:
    # strip generic arguments, keep the last dotted segment of each base type
    text = re.sub(r"<[^<>]*>", "", text)
    text = re.sub(r"<[^<>]*>", "", text)
    out = []
    for part in text.split(","):
        part = part.split(" where ")[0]
        names = [n for n in _IDENT_RE.findall(part) if n not in _HERITAGE_SKIP]
        if names:
            out.append(names[-1])
    return out


# --------------------------------------------------------------------------- tree-sitter walkers

class _Walker:
    def __init__(self, language: str, src: bytes, lines: list[str], path: str):
        self.language = language
        self.src = src
        self.lines = lines
        self.path = path
        self.symbols: list[Symbol] = []
        self.refs: list[Ref] = []
        self.node_symbol: dict[tuple, str] = {}
        self.depth_cap = 80

    # -- symbol construction
    def add(self, node, name: str, typ: str, parent: Symbol | None, ns: str) -> Symbol:
        prefix = parent.qname if parent else ns
        qname = f"{prefix}.{name}" if prefix else name
        start = node.start_point[0] + 1
        sym = Symbol(
            id=f"{self.path}#{typ}:{qname}@{start}",
            name=name, qname=qname, type=typ,
            parent_id=parent.id if parent else None,
            start=start, end=_end_line(node),
            signature=_signature(node, self.src),
        )
        self.symbols.append(sym)
        self.node_symbol[(node.type, node.start_byte, node.end_byte)] = sym.id
        return sym

    def add_heritage(self, node, sym: Symbol, text: str) -> None:
        for n in _heritage_names(text):
            self.refs.append(Ref("base", n, node.start_point[0] + 1, sym.id))

    # -- dispatch
    def walk(self, node, parent: Symbol | None, ns: str, depth: int = 0) -> None:
        if depth > self.depth_cap:
            return
        handler = getattr(self, f"_walk_{self.language}", None)
        children = list(node.children)
        cur_ns = ns
        for ch in children:
            if self.language == "csharp" and ch.type == "file_scoped_namespace_declaration":
                name = ch.child_by_field_name("name")
                cur_ns = _node_text(name, self.src) if name is not None else ns
                self.add(ch, cur_ns, "namespace", None, "")
                continue
            handler(ch, parent, cur_ns, depth)

    # -- C#
    _CS_CLASS = {
        "class_declaration": "class", "interface_declaration": "interface",
        "struct_declaration": "struct", "record_declaration": "record",
        "record_struct_declaration": "record", "enum_declaration": "enum",
    }
    _CS_MEMBER = {
        "method_declaration": "method", "constructor_declaration": "constructor",
        "destructor_declaration": "destructor", "property_declaration": "property",
        "indexer_declaration": "indexer", "operator_declaration": "operator",
        "delegate_declaration": "delegate", "event_declaration": "event",
    }

    def _walk_csharp(self, ch, parent, ns, depth):
        t = ch.type
        if t in ("namespace_declaration",):
            name = ch.child_by_field_name("name")
            full = _node_text(name, self.src) if name is not None else ""
            qn = f"{ns}.{full}" if ns and full else (full or ns)
            self.add(ch, qn, "namespace", None, "")
            body = ch.child_by_field_name("body")
            if body is not None:
                self.walk(body, parent, qn, depth + 1)
        elif t in self._CS_CLASS:
            name = _first_identifier(ch, self.src)
            if not name:
                return
            sym = self.add(ch, name, self._CS_CLASS[t], parent, ns)
            for c in ch.children:
                if c.type == "base_list":
                    self.add_heritage(c, sym, _node_text(c, self.src).lstrip(": ").strip())
            body = ch.child_by_field_name("body")
            if body is not None:
                self.walk(body, sym, ns, depth + 1)
            else:
                for c in ch.children:
                    if c.type in ("declaration_list", "enum_member_declaration_list"):
                        self.walk(c, sym, ns, depth + 1)
        elif t in self._CS_MEMBER and parent is not None:
            name = _first_identifier(ch, self.src)
            if t == "indexer_declaration":
                name = "this[]"
            elif t == "operator_declaration":
                name = "operator"
            if name:
                self.add(ch, name, self._CS_MEMBER[t], parent, ns)
        elif t in ("field_declaration", "event_field_declaration") and parent is not None:
            name = _field_name_csharp(ch, self.src)
            if name:
                self.add(ch, name, "field", parent, ns)
        elif t in ("declaration_list", "global_statement", "compilation_unit"):
            self.walk(ch, parent, ns, depth + 1)

    # -- Python
    def _walk_python(self, ch, parent, ns, depth):
        t = ch.type
        if t == "decorated_definition":
            inner = ch.child_by_field_name("definition")
            if inner is not None:
                self._walk_python_def(inner, ch, parent, ns, depth)
        elif t in ("class_definition", "function_definition"):
            self._walk_python_def(ch, ch, parent, ns, depth)

    def _walk_python_def(self, node, outer, parent, ns, depth):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(name_node, self.src)
        if node.type == "class_definition":
            sym = self.add(outer, name, "class", parent, ns)
            sup = node.child_by_field_name("superclasses")
            if sup is not None:
                self.add_heritage(sup, sym, _node_text(sup, self.src).strip("()"))
            body = node.child_by_field_name("body")
            if body is not None:
                self.walk(body, sym, ns, depth + 1)
        elif parent is not None and parent.type == "class":
            self.add(outer, name, "method", parent, ns)
        elif parent is None:
            self.add(outer, name, "function", parent, ns)

    # -- JavaScript / TypeScript
    _JS_CLASS = {
        "class_declaration": "class", "abstract_class_declaration": "class",
        "interface_declaration": "interface", "enum_declaration": "enum",
        "type_alias_declaration": "type",
    }

    def _walk_javascript(self, ch, parent, ns, depth):
        t = ch.type
        if t == "export_statement":
            decl = ch.child_by_field_name("declaration")
            if decl is not None:
                self._walk_javascript(decl, parent, ns, depth + 1)
        elif t in self._JS_CLASS:
            name = _first_identifier(ch, self.src)
            if not name:
                return
            sym = self.add(ch, name, self._JS_CLASS[t], parent, ns)
            for c in ch.children:
                if c.type in ("class_heritage", "extends_type_clause", "extends_clause", "implements_clause"):
                    self.add_heritage(c, sym, _node_text(c, self.src))
            body = ch.child_by_field_name("body")
            if body is not None:
                self.walk(body, sym, ns, depth + 1)
        elif t in ("function_declaration", "generator_function_declaration") and parent is None:
            name = _first_identifier(ch, self.src)
            if name:
                self.add(ch, name, "function", parent, ns)
        elif t == "method_definition" and parent is not None:
            name = _first_identifier(ch, self.src)
            if name:
                self.add(ch, name, "constructor" if name == "constructor" else "method", parent, ns)
        elif t in ("lexical_declaration", "variable_declaration") and parent is None:
            for d in ch.children:
                if d.type == "variable_declarator":
                    val = d.child_by_field_name("value")
                    if val is not None and val.type in ("arrow_function", "function_expression", "function"):
                        name = _first_identifier(d, self.src)
                        if name:
                            self.add(ch, name, "function", parent, ns)
        elif t in ("class_body", "statement_block"):
            self.walk(ch, parent, ns, depth + 1)

    _walk_typescript = _walk_javascript
    _walk_tsx = _walk_javascript


_CS_USING = re.compile(r"using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;")
_PY_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))", re.M)
_IDENT_TYPES = {"identifier", "type_identifier", "property_identifier"}


def _collect_refs(root, w: _Walker, language: str) -> None:
    """Iterative walk collecting imports and identifier references per owning symbol."""
    src = w.src
    seen: set[tuple[str, str]] = set()
    stack = [(root, "")]
    while stack:
        node, owner = stack.pop()
        key = (node.type, node.start_byte, node.end_byte)
        sid = w.node_symbol.get(key)
        if sid is not None:
            owner = sid
        t = node.type
        if language == "csharp" and t == "using_directive":
            m = _CS_USING.search(_node_text(node, src))
            if m:
                w.refs.append(Ref("import", m.group(1), node.start_point[0] + 1))
            continue
        if language == "python" and t in ("import_statement", "import_from_statement"):
            for m in _PY_IMPORT.finditer(_node_text(node, src)):
                if m.group(1):
                    w.refs.append(Ref("import", m.group(1), node.start_point[0] + 1))
                else:
                    for part in m.group(2).split(","):
                        part = part.strip().split(" as ")[0].strip()
                        if part:
                            w.refs.append(Ref("import", part, node.start_point[0] + 1))
            continue
        if language in ("javascript", "typescript", "tsx") and t == "import_statement":
            s = node.child_by_field_name("source")
            if s is not None:
                w.refs.append(Ref("import", _node_text(s, src).strip("'\"` "), node.start_point[0] + 1))
            continue
        if t in _IDENT_TYPES:
            name = _node_text(node, src)
            if len(name) >= 3 and (name, owner) not in seen:
                seen.add((name, owner))
                w.refs.append(Ref("ident", name, node.start_point[0] + 1, owner))
            continue
        for c in node.children:
            stack.append((c, owner))


# --------------------------------------------------------------------------- SQL (regex)

_SQL_CREATE = re.compile(
    r"^\s*create\s+(?:or\s+alter\s+)?(proc(?:edure)?|view|function|table)\s+([\w\[\]\.\"`]+)",
    re.I | re.M,
)


def _parse_sql(lines: list[str], text: str, path: str) -> list[Symbol]:
    found = []
    for m in _SQL_CREATE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1 + (1 if text[m.start()] == "\n" else 0)
        kind = m.group(1).lower()
        kind = "procedure" if kind.startswith("proc") else kind
        name = m.group(2).replace("[", "").replace("]", "").replace('"', "").replace("`", "")
        found.append((line, kind, name))
    syms = []
    for i, (line, kind, name) in enumerate(found):
        end = (found[i + 1][0] - 1) if i + 1 < len(found) else len(lines)
        syms.append(Symbol(
            id=f"{path}#{kind}:{name}@{line}", name=name.split(".")[-1], qname=name, type=kind,
            parent_id=None, start=line, end=max(line, end),
            signature=" ".join(lines[line - 1].split())[:300],
        ))
    return syms


# --------------------------------------------------------------------------- chunking

_COMMENT_PREFIXES = ("//", "///", "#", "*", "/*", "--", "'", "@", "[")


def _with_leading_comments(lines: list[str], start: int, floor: int = 1) -> int:
    s = start
    while s - 1 >= floor and start - s < 15:
        prev = lines[s - 2].strip()
        if prev and prev.startswith(_COMMENT_PREFIXES):
            s -= 1
        else:
            break
    return s


def _split_range(start: int, end: int) -> list[tuple[int, int]]:
    if end - start + 1 <= MAX_CHUNK_LINES:
        return [(start, end)]
    out, s = [], start
    while s <= end:
        e = min(end, s + SPLIT_LINES - 1)
        out.append((s, e))
        if e >= end:
            break
        s = e - SPLIT_OVERLAP + 1
    return out


def _make_chunk(lines: list[str], start: int, end: int, sym: Symbol | None, label: str = "") -> Chunk | None:
    text = "\n".join(lines[start - 1:end])
    if not text.strip():
        return None
    if sym is not None:
        header = f"{sym.type} {sym.qname}\n{sym.signature}"
    else:
        header = label
    return Chunk(start, end, text, sym.id if sym else None, header)


def build_chunks(lines: list[str], symbols: list[Symbol], path: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    covered = [False] * (len(lines) + 2)

    def emit(start, end, sym):
        for s, e in _split_range(start, end):
            c = _make_chunk(lines, s, e, sym)
            if c:
                chunks.append(c)
        for i in range(start, min(end, len(lines)) + 1):
            covered[i] = True

    children: dict[str, list[Symbol]] = {}
    for s in symbols:
        if s.parent_id:
            children.setdefault(s.parent_id, []).append(s)

    for s in symbols:
        if s.type in LEAF_TYPES or s.type in ("procedure", "view", "function", "table"):
            emit(_with_leading_comments(lines, s.start), s.end, s)
        elif s.type in CLASS_LIKE:
            members = [c for c in children.get(s.id, []) if c.type in LEAF_TYPES]
            if s.type == "enum" or not members:
                emit(_with_leading_comments(lines, s.start), min(s.end, s.start + MAX_CHUNK_LINES - 1), s)
            else:
                # stop the header before the first member's own leading comments
                first = _with_leading_comments(lines, min(m.start for m in members), s.start + 1)
                emit(_with_leading_comments(lines, s.start), min(max(s.start, first - 1), s.start + 40), s)

    # uncovered regions (top level scripts, plain text, files without symbols)
    i = 1
    n = len(lines)
    while i <= n:
        if covered[i]:
            i += 1
            continue
        j = i
        while j <= n and not covered[j]:
            j += 1
        seg_start, seg_end = i, j - 1
        if sum(1 for k in range(seg_start, seg_end + 1) if lines[k - 1].strip()) >= (5 if symbols else 1):
            s = seg_start
            while s <= seg_end:
                e = min(seg_end, s + WINDOW_LINES - 1)
                c = _make_chunk(lines, s, e, None, f"text {path}")
                if c:
                    chunks.append(c)
                if e >= seg_end:
                    break
                s = e - WINDOW_OVERLAP + 1
        i = j
    chunks.sort(key=lambda c: (c.start, c.end))
    return chunks


# --------------------------------------------------------------------------- public API

def parse_file(language: str, text: str, path: str) -> ParseResult:
    text = normalize_text(text)
    lines = text.split("\n")
    result = ParseResult()

    if language == "sql":
        result.symbols = _parse_sql(lines, text, path)
        result.parsed_with = "regex"
    elif language in ("csharp", "python", "javascript", "typescript", "tsx"):
        parser = _load_parser(language)
        if parser is not None:
            src = text.encode("utf-8")
            tree = parser.parse(src)
            w = _Walker(language, src, lines, path)
            w.walk(tree.root_node, None, "")
            _collect_refs(tree.root_node, w, language)
            result.symbols, result.refs, result.parsed_with = w.symbols, w.refs, "tree-sitter"
    result.chunks = build_chunks(lines, result.symbols, path)
    return result
