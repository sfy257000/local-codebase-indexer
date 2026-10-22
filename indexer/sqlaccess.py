"""T-SQL data access extraction: which tables a piece of SQL reads / writes and which procedures it executes.

This is a lexical analysis (comments and string literals are masked, then statements are matched with
regular expressions), not a full T-SQL parser. What it does not understand is reported, not guessed:

* dynamic SQL (``EXEC(@sql)``, ``sp_executesql``) becomes an ``uncertain`` access to the unknown object ``?``;
  string literals inside it are still analysed but also marked ``uncertain``;
* temp tables (``#t``) and table variables (``@t``) are ignored, as are CTE names and system objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HOLE = "__hole__"  # stands in for interpolated / unknown parts of a C# string

_NAME = r"[#@]?[\w$]+(?:\s*\.\s*[\w$]*){0,3}"

_KEYWORDS = {
    "select", "from", "where", "set", "values", "as", "on", "default", "exec", "execute", "with", "top",
    "output", "cascade", "no", "null", "table", "index", "view", "procedure", "function", "trigger", "into",
    "using", "when", "then", "case", "end", "begin", "inserted", "deleted", "openquery", "openrowset",
    "openjson", "openxml", "string_split", "generate_series", "and", "or", "not", "is", "in", "if", "else",
    "all", "any", "distinct", "union", "go", "declare", "return", "the", "a", "an",
}
_ALIAS_STOP = _KEYWORDS | {
    "inner", "left", "right", "full", "cross", "outer", "join", "group", "order", "having", "except",
    "intersect", "option", "for", "natural", "pivot", "unpivot", "tablesample", "update", "delete", "insert",
    "merge", "truncate", "while", "print", "raiserror", "commit", "rollback", "throw", "try", "catch",
}
_SYSTEM_QUALIFIERS = {"sys", "information_schema"}

_RX_INSERT = re.compile(rf"(?<![@\w$#.])INSERT\s+(?:INTO\s+)?(?P<n>{_NAME})", re.I)
_RX_UPDATE = re.compile(rf"(?<![@\w$#.])UPDATE\s+(?:TOP\s*\(\s*\d+\s*\)\s+)?(?P<n>{_NAME})\s+SET\b", re.I)
_RX_DELETE = re.compile(rf"(?<![@\w$#.])DELETE\s+(?:TOP\s*\(\s*\d+\s*\)\s+)?(?:FROM\s+)?(?P<n>{_NAME})", re.I)
_RX_MERGE = re.compile(rf"(?<![@\w$#.])MERGE\s+(?:TOP\s*\(\s*\d+\s*\)\s+)?(?:INTO\s+)?(?P<n>{_NAME})", re.I)
_RX_TRUNC = re.compile(rf"(?<![@\w$#.])TRUNCATE\s+TABLE\s+(?P<n>{_NAME})", re.I)
_RX_INTO = re.compile(rf"(?<![@\w$#.])INTO\s+(?P<n>{_NAME})", re.I)
_RX_USING = re.compile(rf"(?<![@\w$#.])USING\s+(?P<n>{_NAME})", re.I)
_RX_FROM = re.compile(r"(?<![@\w$#.])FROM\b", re.I)
_RX_JOIN = re.compile(r"(?<![@\w$#.])JOIN\b", re.I)
_RX_EXEC = re.compile(rf"(?<![@\w$#.])EXEC(?:UTE)?\s+(?:@\w+\s*=\s*)?(?P<n>{_NAME})", re.I)
_RX_EXEC_PAREN = re.compile(r"(?<![@\w$#.])EXEC(?:UTE)?\s*\(", re.I)
_RX_SPEXEC = re.compile(r"(?<![@\w$#.])sp_executesql\b", re.I)
_RX_CTE = re.compile(r"(?:\bWITH|,)\s+(?P<n>[\w$]+)\s*(?:\([^)]*\))?\s+AS\s*\(", re.I)

_RX_ITEM_NAME = re.compile(rf"\s*(?P<n>{_NAME})")
_RX_ALIAS = re.compile(r"\s+(?:AS\s+)?(?P<a>[\w$]+)", re.I)
_RX_HINT = re.compile(r"\s+WITH\s*\([^)]*\)", re.I)
_RX_COMMA = re.compile(r"\s*,")


@dataclass
class Access:
    key: str          # lower-case last name segment; "?" for dynamic SQL
    name: str         # as written (last segment)
    qualifier: str    # database / schema / linked server prefix as written
    kind: str         # table | procedure | function | unknown
    access: str       # read | write | exec | map
    op: str           # select | insert | update | delete | merge | truncate | into | exec | dynamic | map
    line: int
    via: str          # sql | dynamic_sql | csharp_sql | csharp_sp | ef_mapping
    confidence: str   # confirmed | probable | uncertain
    evidence: str = ""
    detail: str = ""
    source_id: str | None = None  # symbol that contains the access (set by the caller)


def _blank(chars: list[str], start: int, end: int) -> None:
    for i in range(start, min(end, len(chars))):
        if chars[i] != "\n":
            chars[i] = " "


def mask_sql(text: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Blank out comments and string contents (offsets are preserved). Returns the masked text and the
    string literals as (start, end, content)."""
    out = list(text)
    n = len(text)
    i = 0
    literals: list[tuple[int, int, str]] = []
    while i < n:
        c = text[i]
        if c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank(out, i, j)
            i = j
        elif c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            _blank(out, i, j)
            i = j
        elif c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            literals.append((i, j, text[i + 1:j].replace("''", "'")))
            _blank(out, i + 1, j)
            i = j + 1
        elif c in '[]"':
            out[i] = " "
            i += 1
        else:
            i += 1
    return "".join(out), literals


def _split_name(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(".")]


def _acceptable(parts: list[str]) -> bool:
    last = parts[-1]
    if not last or last[0] in "#@":
        return False
    if last.lower() in _KEYWORDS:
        return False
    if len(parts) > 1 and parts[-2].lower() in _SYSTEM_QUALIFIERS:
        return False
    return True


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _evidence(original: str, pos: int, limit: int = 160) -> str:
    start = original.rfind("\n", 0, pos) + 1
    end = original.find("\n", pos)
    end = len(original) if end < 0 else end
    return " ".join(original[start:end].split())[:limit]


def _parse_items(masked: str, pos: int, allow_list: bool) -> list[tuple[list[str], str | None, bool, int]]:
    """Table references after FROM / JOIN: [(name parts, alias, is_function, offset)]. With ``allow_list`` a
    comma separated list (FROM a, b c) is followed."""
    items = []
    while True:
        j = pos
        while j < len(masked) and masked[j].isspace():
            j += 1
        if j >= len(masked) or masked[j] == "(":
            break  # derived table: its own SELECT is scanned separately
        m = _RX_ITEM_NAME.match(masked, pos)
        if not m:
            break
        parts = _split_name(m.group("n"))
        end = m.end()
        is_func = False
        k = end
        while k < len(masked) and masked[k] in " \t":
            k += 1
        if k < len(masked) and masked[k] == "(":
            is_func = True
            # skip the argument list so a function's arguments are not mistaken for an alias
            depth = 0
            while k < len(masked):
                if masked[k] == "(":
                    depth += 1
                elif masked[k] == ")":
                    depth -= 1
                    if depth == 0:
                        k += 1
                        break
                k += 1
            end = k
        alias = None
        a = _RX_ALIAS.match(masked, end)
        if a and a.group("a").lower() not in _ALIAS_STOP:
            alias = a.group("a")
            end = a.end()
        h = _RX_HINT.match(masked, end)
        if h:
            end = h.end()
        items.append((parts, alias, is_func, m.start("n")))
        pos = end
        if not allow_list:
            break
        c = _RX_COMMA.match(masked, pos)
        if not c:
            break
        pos = c.end()
    return items


def _resolve_alias(target: list[str], window_items: list) -> list[str]:
    if len(target) != 1:
        return target
    low = target[0].lower()
    for parts, alias, is_func, _ in window_items:
        if alias and alias.lower() == low and not is_func:
            return parts
    return target


def extract_sql_accesses(text: str, first_line: int = 1, _dynamic: bool = False) -> list[Access]:
    """All table / procedure accesses in ``text``; line numbers start at ``first_line``."""
    masked, literals = mask_sql(text)
    out: list[Access] = []
    base_conf = "uncertain" if _dynamic else "confirmed"
    base_via = "dynamic_sql" if _dynamic else "sql"
    ctes = {m.group("n").lower() for m in _RX_CTE.finditer(masked)}
    seen: set[tuple] = set()

    def add(parts: list[str], pos: int, kind: str, access: str, op: str) -> None:
        if not parts or not _acceptable(parts):
            return
        key = parts[-1].lower()
        if kind == "table" and key in ctes and len(parts) == 1:
            return
        conf = base_conf
        if HOLE in key:
            parts, key, kind, conf = ["(dynamic)"], "?", "unknown", "uncertain"
        line = first_line + _line_of(masked, pos) - 1
        sig = (key, access, op, line)
        if sig in seen:
            return
        seen.add(sig)
        out.append(Access(
            key=key, name=parts[-1], qualifier=".".join(parts[:-1]), kind=kind, access=access, op=op, line=line,
            via=base_via, confidence=conf, evidence=_evidence(text, pos),
        ))

    def window_items(end: int) -> list:
        stop = masked.find(";", end)
        window = masked[end: (stop if 0 <= stop - end < 3000 else end + 3000)]
        items = []
        for rx in (_RX_FROM, _RX_JOIN):
            for m in rx.finditer(window):
                items.extend(_parse_items(window, m.end(), allow_list=rx is _RX_FROM))
        return items

    for m in _RX_INSERT.finditer(masked):
        add(_split_name(m.group("n")), m.start("n"), "table", "write", "insert")
    for m in _RX_UPDATE.finditer(masked):
        parts = _resolve_alias(_split_name(m.group("n")), window_items(m.end()))
        add(parts, m.start("n"), "table", "write", "update")
    for m in _RX_DELETE.finditer(masked):
        parts = _resolve_alias(_split_name(m.group("n")), window_items(m.end()))
        add(parts, m.start("n"), "table", "write", "delete")
    for m in _RX_MERGE.finditer(masked):
        add(_split_name(m.group("n")), m.start("n"), "table", "write", "merge")
        u = _RX_USING.search(masked, m.end(), min(len(masked), m.end() + 3000))
        if u:
            add(_split_name(u.group("n")), u.start("n"), "table", "read", "select")
    for m in _RX_TRUNC.finditer(masked):
        add(_split_name(m.group("n")), m.start("n"), "table", "write", "truncate")
    for m in _RX_INTO.finditer(masked):
        if re.search(r"(?:INSERT|MERGE)\s*(?:TOP\s*\(\s*\d+\s*\)\s*)?$", masked[max(0, m.start() - 40): m.start()], re.I):
            continue
        add(_split_name(m.group("n")), m.start("n"), "table", "write", "into")

    for m in _RX_FROM.finditer(masked):
        if re.search(r"(?<![@\w$#.])DELETE\s+(?:TOP\s*\(\s*\d+\s*\)\s*)?$", masked[max(0, m.start() - 40): m.start()], re.I):
            continue  # the FROM of DELETE FROM t is the write target, handled above
        for parts, _alias, is_func, off in _parse_items(masked, m.end(), allow_list=True):
            add(parts, off, "function" if is_func else "table", "read", "select")
    for m in _RX_JOIN.finditer(masked):
        for parts, _alias, is_func, off in _parse_items(masked, m.end(), allow_list=False):
            add(parts, off, "function" if is_func else "table", "read", "select")

    for m in _RX_EXEC.finditer(masked):
        parts = _split_name(m.group("n"))
        if parts[-1].lower() == "sp_executesql":
            continue
        if parts[0].startswith("@"):
            add([HOLE], m.start("n"), "unknown", "exec", "dynamic")
        else:
            add(parts, m.start("n"), "procedure", "exec", "exec")

    # dynamic SQL: record the site, and analyse any literal inside the call as uncertain
    if not _dynamic:
        dyn_ranges: list[tuple[int, int]] = []
        for m in list(_RX_EXEC_PAREN.finditer(masked)):
            depth, k = 1, m.end()
            while k < len(masked) and depth:
                depth += (masked[k] == "(") - (masked[k] == ")")
                k += 1
            dyn_ranges.append((m.end(), k))
            add([HOLE], m.start(), "unknown", "exec", "dynamic")
        for m in _RX_SPEXEC.finditer(masked):
            dyn_ranges.append((m.end(), min(len(masked), m.end() + 4000)))
            add([HOLE], m.start(), "unknown", "exec", "dynamic")
        for start, end in dyn_ranges:
            for ls, le, content in literals:
                if start <= ls < end and content.strip():
                    line = first_line + _line_of(masked, ls) - 1
                    for a in extract_sql_accesses(content, line, _dynamic=True):
                        sig = (a.key, a.access, a.op, a.line)
                        if sig not in seen:
                            seen.add(sig)
                            out.append(a)
    out.sort(key=lambda a: (a.line, a.access, a.key))
    return out
