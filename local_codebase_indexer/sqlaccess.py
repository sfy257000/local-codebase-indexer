"""T-SQL data access extraction: which tables a piece of SQL reads / writes and which procedures it executes.

This is a lexical analysis (comments and string literals are masked, then statements are matched with
regular expressions), not a full T-SQL parser. What it does not understand is reported, not guessed:

* dynamic SQL (``EXEC(@sql)``, ``sp_executesql``) becomes an ``uncertain`` access to the unknown object ``?``;
  string literals inside it are still analysed but also marked ``uncertain``;
* temp tables (``#t``) and table variables (``@t``) are ignored, as are CTE names and system objects.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from itertools import islice

HOLE = "__hole__"  # stands in for interpolated / unknown parts of a C# string

_PART = r'(?:\[(?:[^\]]|\]\])+\]|"(?:[^"]|"")+"|[#@]?[\w$]+)'
_NAME = rf"{_PART}(?:\s*\.\s*(?:{_PART})?){{0,3}}"
_TOP = r"(?:TOP\s*(?:\((?:[^()]|\([^()]*\))*\)|\d+)(?:\s+PERCENT)?\s+)?"
_HINT = r"\s+WITH\s*\((?:[^()]|\([^()]*\))*\)"
_QUOTED = re.compile(r'\[(?:[^\]]|\]\])*\]|"(?:[^"]|"")*"')

_KEYWORDS = {
    "select", "from", "where", "set", "values", "as", "on", "default", "exec", "execute", "with", "top",
    "output", "cascade", "no", "null", "table", "index", "view", "procedure", "function", "trigger", "into",
    "using", "when", "then", "case", "end", "begin", "inserted", "deleted", "openquery", "openrowset",
    "openjson", "openxml", "string_split", "generate_series", "and", "or", "not", "is", "in", "if", "else",
    "all", "any", "distinct", "union", "go", "declare", "return",
}
_ALIAS_STOP = _KEYWORDS | {
    "inner", "left", "right", "full", "cross", "outer", "join", "group", "order", "having", "except",
    "intersect", "option", "for", "natural", "pivot", "unpivot", "tablesample", "update", "delete", "insert",
    "merge", "truncate", "while", "print", "raiserror", "commit", "rollback", "throw", "try", "catch",
}
_SYSTEM_QUALIFIERS = {"sys", "information_schema"}

_RX_INSERT = re.compile(rf"(?<![@\w$#.])INSERT\s+{_TOP}(?:INTO\s+)?(?P<n>{_NAME})", re.I)
_RX_UPDATE = re.compile(rf"(?<![@\w$#.])UPDATE\s+{_TOP}(?P<n>{_NAME})(?:{_HINT})?\s+SET\b", re.I)
_RX_DELETE = re.compile(rf"(?<![@\w$#.])DELETE\s+{_TOP}(?:FROM\s+)?(?P<n>{_NAME})", re.I)
_RX_MERGE = re.compile(rf"(?<![@\w$#.])MERGE\s+{_TOP}(?:INTO\s+)?(?P<n>{_NAME})", re.I)
_RX_TRUNC = re.compile(rf"(?<![@\w$#.])TRUNCATE\s+TABLE\s+(?P<n>{_NAME})", re.I)
_RX_INTO = re.compile(rf"(?<![@\w$#.])INTO\s+(?P<n>{_NAME})", re.I)
_RX_USING = re.compile(rf"(?<![@\w$#.])USING\s+(?P<n>{_NAME})", re.I)
_RX_FROM = re.compile(r"(?<![@\w$#.])FROM\b", re.I)
_RX_JOIN = re.compile(r"(?<![@\w$#.])JOIN\b", re.I)
_RX_EXEC = re.compile(rf"(?<![@\w$#.])EXEC(?:UTE)?\s+(?:@\w+\s*=\s*)?(?P<n>{_NAME})", re.I)
_RX_EXEC_PAREN = re.compile(r"(?<![@\w$#.])EXEC(?:UTE)?\s*\(", re.I)
_RX_SPEXEC = re.compile(r"(?<![@\w$#.])sp_executesql\b", re.I)
_RX_CTE = re.compile(rf"(?:\bWITH|,)\s+(?P<n>{_PART})\s*(?:\([^)]*\))?\s+AS\s*\(", re.I)

_RX_ITEM_NAME = re.compile(rf"\s*(?P<n>{_NAME})")
_RX_ALIAS = re.compile(rf"\s+(?:AS\s+)?(?P<a>{_PART})", re.I)
_RX_HINT = re.compile(_HINT, re.I)
_RX_COMMA = re.compile(r"\s*,")


@dataclass
class Access:
    key: str          # lower-case last name segment; "?" for dynamic SQL
    name: str         # as written (last segment)
    qualifier: str    # database / schema / linked server prefix as written
    kind: str         # table | procedure | function | unknown
    access: str       # read | write | exec | map | schema
    op: str           # select | insert | update | delete | merge | truncate | into | exec | dynamic | map
    line: int
    via: str          # sql | dynamic_sql | csharp_sql | csharp_sp | ef_mapping | ef_raw (unresolved) | ef_linq
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
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
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
        elif c in '["':
            closing = ']' if c == '[' else '"'
            i += 1
            while i < n:
                if text[i] == closing:
                    if i + 1 < n and text[i + 1] == closing:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        else:
            i += 1
    return "".join(out), literals


def _split_name(raw: str) -> list[str]:
    """Split only unquoted dots, preserving delimiters until acceptance checks."""
    parts, start = [], 0
    for token in re.finditer(rf'{_QUOTED.pattern}|[.]', raw):
        if token.group() == '.':
            parts.append(raw[start:token.start()].strip())
            start = token.end()
    return parts + [raw[start:].strip()]


def _unquote(part: str) -> str:
    part = part.strip()
    if part.startswith('[') and part.endswith(']'):
        return part[1:-1].replace(']]', ']')
    if part.startswith('"') and part.endswith('"'):
        return part[1:-1].replace('""', '"')
    return part


def _acceptable(parts: list[str]) -> bool:
    last = _unquote(parts[-1])
    if not last or last[0] in '#@':
        return False
    if parts[-1] == last and last.lower() in _KEYWORDS:
        return False
    if len(parts) > 1 and _unquote(parts[-2]).lower() in _SYSTEM_QUALIFIERS:
        return False
    return True


def _matches(rx, text: str):
    """SQL keywords inside delimited identifiers are data, not syntax."""
    quoted = list(_QUOTED.finditer(text))
    qi = 0
    for match in rx.finditer(text):
        while qi < len(quoted) and quoted[qi].end() <= match.start():
            qi += 1
        if qi == len(quoted) or match.start() < quoted[qi].start():
            yield match


def _close_paren(masked: str, start: int) -> int:
    depth = 0
    tokens = re.compile(rf'{_QUOTED.pattern}|[()]')
    for m in tokens.finditer(masked, start):
        if m.group() not in {'(', ')'}:
            continue
        depth += 1 if m.group() == '(' else -1
        if depth == 0:
            return m.end()
    return len(masked)


def _table_from(masked: str, pos: int, start: int = 0) -> bool:
    prefix = masked[start:pos]
    if re.search(r'\bFETCH\s+(?:(?:NEXT|PRIOR|FIRST|LAST|ABSOLUTE|RELATIVE)(?:\s+[+-]?(?:\d+|@\w+))?\s*)?$', prefix, re.I):
        return False
    stack = []
    for m in _matches(re.compile(r'[()]|\bSELECT\b', re.I), prefix):
        if m.group() == '(':
            before = re.search(r'([\w$]+)\s*$', prefix[:m.start()])
            stack.append([before.group(1).lower() if before else '', False])
        elif m.group() == ')':
            if stack:
                stack.pop()
        elif stack:
            stack[-1][1] = True
    if stack:
        func, query = stack[-1]
        if func and not query and func not in {'in', 'exists', 'as', 'with', 'from', 'join', 'where', 'on', 'and', 'or'}:
            return False
    return True


def _source_items(masked: str) -> list:
    items = []
    punctuation = list(_matches(re.compile(r'[(),;]|\b(?:WHERE|GROUP|ORDER|HAVING|UNION|EXCEPT|INTERSECT|OPTION|FOR|SET|GO)\b', re.I), masked))
    positions = [token.start() for token in punctuation]
    boundaries = [token.end() for token in punctuation if token.group() == ';']
    for rx in (_RX_FROM, _RX_JOIN):
        for m in _matches(rx, masked):
            boundary = bisect_right(boundaries, m.start())
            statement_start = boundaries[boundary - 1] if boundary else 0
            if rx is _RX_FROM and not _table_from(masked, m.start(), statement_start):
                continue
            items.extend(_parse_items(masked, m.end(), allow_list=rx is _RX_FROM))
            if rx is _RX_FROM:
                # Follow commas after JOIN ... ON, at the FROM clause's own depth.
                depth = 0
                for t in islice(punctuation, bisect_right(positions, m.end() - 1), None):
                    value = t.group().upper()
                    if value == '(':
                        depth += 1
                    elif value == ')':
                        if not depth:
                            break
                        depth -= 1
                    elif not depth:
                        if value != ',':
                            break
                        items.extend(_parse_items(masked, t.end(), False))
    return items


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
            hint_end = _close_paren(masked, k)
            legacy = masked[k + 1:hint_end - 1].strip().lower()
            is_func = not bool(re.fullmatch(r'(?:nolock|readuncommitted|readcommitted|rowlock|paglock|tablock|tablockx|updlock|holdlock|xlock)(?:\s*,\s*\w+)*', legacy))
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
        h = _RX_HINT.match(masked, end)
        if h:
            end = h.end()
        alias = None
        a = _RX_ALIAS.match(masked, end)
        if a and _unquote(a.group("a")).lower() not in _ALIAS_STOP:
            alias = _unquote(a.group("a"))
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
    low = _unquote(target[0]).lower()
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
    ctes = {_unquote(m.group("n")).lower() for m in _matches(_RX_CTE, masked)}
    seen: set[tuple] = set()
    line_breaks = [m.start() for m in re.finditer("\n", masked)]

    def add(parts: list[str], pos: int, kind: str, access: str, op: str) -> None:
        if not parts or not _acceptable(parts):
            return
        parts = [_unquote(p) for p in parts]
        key = parts[-1].lower()
        if kind == "table" and key in ctes and len(parts) == 1:
            return
        conf = base_conf
        if HOLE in key:
            parts, key, kind, conf = ["(dynamic)"], "?", "unknown", "uncertain"
        line = first_line + bisect_right(line_breaks, pos - 1)
        sig = (key, tuple(parts[:-1]), access, op, line)
        if sig in seen:
            return
        seen.add(sig)
        out.append(Access(
            key=key, name=parts[-1], qualifier=".".join(parts[:-1]), kind=kind, access=access, op=op, line=line,
            via=base_via, confidence=conf, evidence=_evidence(text, pos),
        ))

    def window_items(end: int) -> list:
        window = masked[end:end + 3000]
        stop = next(_matches(re.compile(';'), window), None)
        if stop:
            window = window[:stop.start()]
        return _source_items(window)

    for m in _matches(_RX_INSERT, masked):
        add(_split_name(m.group("n")), m.start("n"), "table", "write", "insert")
    for m in _matches(_RX_UPDATE, masked):
        parts = _resolve_alias(_split_name(m.group("n")), window_items(m.end()))
        add(parts, m.start("n"), "table", "write", "update")
    for m in _matches(_RX_DELETE, masked):
        parts = _resolve_alias(_split_name(m.group("n")), window_items(m.end()))
        add(parts, m.start("n"), "table", "write", "delete")
    for m in _matches(_RX_MERGE, masked):
        add(_split_name(m.group("n")), m.start("n"), "table", "write", "merge")
        u = _RX_USING.search(masked, m.end(), min(len(masked), m.end() + 3000))
        if u:
            add(_split_name(u.group("n")), u.start("n"), "table", "read", "select")
    for m in _matches(_RX_TRUNC, masked):
        add(_split_name(m.group("n")), m.start("n"), "table", "write", "truncate")
    for m in _matches(_RX_INTO, masked):
        if re.search(rf"(?:INSERT|MERGE)\s+{_TOP}$", masked[max(0, m.start() - 500): m.start()], re.I):
            continue
        add(_split_name(m.group("n")), m.start("n"), "table", "write", "into")

    for parts, _alias, is_func, off in _source_items(masked):
        # DELETE FROM's target is already a write, not an additional read.
        if re.search(rf'\bDELETE\s+{_TOP}FROM\s*$', masked[max(0, off - 500):off], re.I):
            continue
        add(parts, off, "function" if is_func else "table", "read", "select")

    ddl = re.compile(rf'\b(?P<op>ALTER|DROP|CREATE)\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<n>{_NAME})', re.I)
    index_ddl = re.compile(rf'\b(?P<op>CREATE\s+(?:UNIQUE\s+)?(?:(?:CLUSTERED|NONCLUSTERED)\s+)?|DROP\s+)INDEX\s+{_NAME}\s+ON\s+(?P<n>{_NAME})', re.I)
    for m in _matches(ddl, masked):
        add(_split_name(m.group('n')), m.start('n'), 'table', 'schema', m.group('op').lower() + '_table')
    for m in _matches(index_ddl, masked):
        add(_split_name(m.group('n')), m.start('n'), 'table', 'schema', m.group('op').split()[0].lower() + '_index')

    for m in _matches(_RX_EXEC, masked):
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
        for m in list(_matches(_RX_EXEC_PAREN, masked)):
            depth, k = 1, m.end()
            while k < len(masked) and depth:
                depth += (masked[k] == "(") - (masked[k] == ")")
                k += 1
            dyn_ranges.append((m.end(), k))
            add([HOLE], m.start(), "unknown", "exec", "dynamic")
        for m in _matches(_RX_SPEXEC, masked):
            dyn_ranges.append((m.end(), min(len(masked), m.end() + 4000)))
            add([HOLE], m.start(), "unknown", "exec", "dynamic")
        for m in _matches(re.compile(r'\bOPENQUERY\s*\(', re.I), masked):
            dyn_ranges.append((m.end(), _close_paren(masked, m.end() - 1)))
        for start, end in dyn_ranges:
            for ls, le, content in literals:
                if start <= ls < end and content.strip():
                    line = first_line + _line_of(masked, ls) - 1
                    for a in extract_sql_accesses(content, line, _dynamic=True):
                        sig = (a.key, tuple(_split_name(a.qualifier)), a.access, a.op, a.line)
                        if sig not in seen:
                            seen.add(sig)
                            out.append(a)
    out.sort(key=lambda a: (a.line, a.access, a.key))
    return out
