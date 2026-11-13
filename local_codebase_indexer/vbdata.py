"""Conservative SQL-string analysis for VB6 modules, classes, and forms.

This is not a VB runtime/call graph. Known ADO/DAO receivers can establish a
static execution site; otherwise SQL-looking strings are only probable leads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .csdata import looks_like_statement
from .sqlaccess import HOLE, Access, extract_sql_accesses

_STRING = r'"(?:[^"]|"")*"'
_METHOD = re.compile(r'^\s*(?:(?:Public|Private|Friend|Static)\s+)*(Sub|Function|Property\s+(?:Get|Let|Set))\s+(\w+)', re.I)
_END = re.compile(r'^\s*End\s+(Sub|Function|Property)\b', re.I)
_DECL = re.compile(r'\b(\w+)\s+As\s+(?:New\s+)?(?:ADODB\.(?:Connection|Command|Recordset)|DAO\.(?:Database|Recordset)|Database)\b', re.I)
_ASSIGN = re.compile(r'^\s*(?:(?:Public|Private|Dim|Static|Const)\s+)*(\w+)(?:\s+As\s+\w+)?\s*=\s*(.+)$', re.I)
_SINK = re.compile(r'\b(\w+)\s*\.\s*(Execute|Open|CommandText)\b', re.I)


@dataclass
class VbScope:
    name: str
    start: int
    end: int


def _statements(text: str):
    """Strip comments outside literals, combine continuations, retain physical lines."""
    pending, start = '', 1
    for number, line in enumerate(text.splitlines(), 1):
        pieces, current, i = [], '', 0
        while i < len(line):
            if line[i] == '"':
                match = re.match(_STRING, line[i:])
                if match:
                    current += match.group()
                    i += match.end()
                    continue
            if line[i] == "'":
                break
            if line[i] == ':':
                pieces.append(current)
                current = ''
            else:
                current += line[i]
            i += 1
        pieces.append(current)
        for piece in pieces:
            if re.match(r'^\s*Rem(?:\s|$)', piece, re.I):
                continue
            if not pending:
                start = number
            pending += piece
            if re.search(r'\s_\s*$', pending):
                pending = re.sub(r'\s_\s*$', ' ', pending)
                continue
            if pending.strip():
                yield start, pending.strip()
            pending = ''
    if pending.strip():
        yield start, pending.strip()


def _code_only(statement: str) -> str:
    return re.sub(_STRING, lambda match: ' ' * len(match.group()), statement)


def _local_bindings(statement: str) -> set[str]:
    method = _METHOD.match(statement)
    if method:
        start = statement.find('(', method.end())
        end = statement.rfind(')')
        body = statement[start + 1:end] if start >= 0 and end > start else ''
    else:
        declaration = re.match(r'(?i)^(?:(?:Public|Private|Dim|Static|Const)\s+)+(.+)$', statement)
        if not declaration:
            return set()
        body = declaration.group(1)
    parts, start, depth = [], 0, 0
    for token in re.finditer(rf'{_STRING}|[(),]', body):
        if token.group() == '(':
            depth += 1
        elif token.group() == ')':
            depth -= 1
        elif token.group() == ',' and not depth:
            parts.append(body[start:token.start()])
            start = token.end()
    parts.append(body[start:])
    names = set()
    for part in parts:
        name = re.match(r'(?i)^\s*(?:(?:ByVal|ByRef|Optional|ParamArray)\s+)*(\w+)', part)
        if name:
            names.add(name.group(1).lower())
    return names


def _evaluate(expression: str, env: dict[str, tuple[str, bool]]) -> tuple[str, bool] | None:
    expression = expression.strip()
    # Ignore subsequent API parameters; commas within SQL literals stay intact.
    tokens = list(re.finditer(rf'{_STRING}|[&+,()]|[\w.]+|\S', expression))
    parts, complete, known = [], True, False
    for token in tokens:
        value = token.group()
        if value == ',':
            break
        if value in {'(', ')', '&', '+'}:
            continue
        if value.startswith('"'):
            parts.append(value[1:-1].replace('""', '"'))
            known = True
        elif value.lower() in env:
            text, is_complete = env[value.lower()]
            parts.append(text)
            complete = complete and is_complete
            known = True
        else:
            parts.append(HOLE)
            complete = False
    return (''.join(parts), complete) if known else None


def parse_vb6(text: str, path: str, symbol_type):
    statements = list(_statements(text))
    scopes: list[VbScope] = []
    current = None
    for line, statement in statements:
        method = _METHOD.match(statement)
        if method:
            if current:
                scopes.append(VbScope(current.name, current.start, line - 1))
            current = VbScope(method.group(2), line, len(text.splitlines()))
        elif _END.match(statement) and current:
            current.end = line
            scopes.append(current)
            current = None
    if current:
        scopes.append(current)
    module = path.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    symbols = [symbol_type(f'{path}#vb6_method:{s.name}@{s.start}', s.name, f'{module}.{s.name}', 'vb6_method', None,
                           s.start, s.end, f'VB6 {s.name}') for s in scopes]
    globals_env: dict[str, tuple[str, bool]] = {}
    receivers_by_scope: dict[int, set[str]] = {0: set()}
    locals_by_scope: dict[int, set[str]] = {}
    for line, statement in statements:
        scope = next((s for s in scopes if s.start <= line <= s.end), None)
        receivers_by_scope.setdefault(scope.start if scope else 0, set()).update(
            m.group(1).lower() for m in _DECL.finditer(_code_only(statement)))
        if scope:
            locals_by_scope.setdefault(scope.start, set()).update(_local_bindings(statement))
    accesses = []
    # Module constants are available inside every procedure regardless of declaration order.
    for line, statement in statements:
        if not any(s.start <= line <= s.end for s in scopes):
            assignment = _ASSIGN.match(statement)
            if assignment:
                value = _evaluate(assignment.group(2), globals_env)
                if value:
                    globals_env[assignment.group(1).lower()] = value
    environments = {s.start: {**globals_env, **{name: (HOLE, False) for name in locals_by_scope.get(s.start, set())}}
                    for s in scopes}
    for line, statement in statements:
        scope = next((s for s in scopes if s.start <= line <= s.end), None)
        env = environments[scope.start] if scope else globals_env
        local_names = locals_by_scope.get(scope.start, set()) if scope else set()
        receivers = (receivers_by_scope[0] - local_names) | receivers_by_scope.get(scope.start if scope else 0, set())
        owner = next((s.id for s in symbols if scope and s.start == scope.start), None)
        sink = _SINK.search(_code_only(statement))
        assignment = _ASSIGN.match(statement) if not sink else None
        if assignment:
            value = _evaluate(assignment.group(2), env)
            env[assignment.group(1).lower()] = value if value else (HOLE, False)
        elif sink:
            value = _evaluate(statement[sink.end():].lstrip().removeprefix('='), env)
        else:
            value = None
        if value is None:
            if sink and sink.group(1).lower() in receivers:
                accesses.append(Access('?', '(dynamic)', '', 'unknown', 'exec', 'dynamic', line,
                                       'vb6_sql', 'uncertain', statement[:160], source_id=owner))
            continue
        sql, complete = value
        if not (sink or looks_like_statement(sql)):
            continue
        for access in extract_sql_accesses(sql, line):
            access.line = line
            access.source_id = owner
            access.via = 'vb6_sql'
            access.evidence = ' '.join(statement.split())[:160]
            if access.confidence != 'uncertain':
                access.confidence = 'confirmed' if sink and complete and sink.group(1).lower() in receivers else 'probable'
            accesses.append(access)
    return symbols, accesses
