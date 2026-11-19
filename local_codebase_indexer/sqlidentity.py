"""Lossless SQL object names and conservative resolution within the indexed corpus.

Names are case-insensitive, as in the original analyzer. Empty qualifiers remain
unknown; no default database or schema is invented. Delimited dots are not separators.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from functools import lru_cache


def parts(name: str, *, folded: bool = True) -> tuple[str, ...]:
    name = name.strip()
    raw, start, closing, i = [], 0, "", 0
    while i < len(name):
        c = name[i]
        if closing:
            if c == closing:
                if i + 1 < len(name) and name[i + 1] == closing:
                    i += 1
                else:
                    closing = ""
        elif c in '["':
            closing = "]" if c == "[" else '"'
        elif c == ".":
            raw.append(name[start:i].strip())
            start = i + 1
        i += 1
    if closing:
        raise ValueError("Unclosed SQL identifier delimiter")
    raw.append(name[start:].strip())
    result = []
    for value in raw:
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1].replace("]]", "]")
        elif value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('""', '"')
        result.append(value.lower() if folded else value)
    if not result[-1] or len(result) > 4:
        raise ValueError("Use a SQL object name with one to four parts")
    return tuple(result)


def encode(value: tuple[str, ...] | list[str]) -> str:
    return json.dumps(
        [s.lower() for s in value], ensure_ascii=False, separators=(",", ":")
    )


def decode(value: str) -> tuple[str, ...]:
    return tuple(json.loads(value))


def display(value: tuple[str, ...]) -> str:
    return ".".join("[" + s.replace("]", "]]") + "]" if s else "" for s in value)


def compatible(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    return all(not x or not y or x == y for x, y in zip(reversed(a), reversed(b)))


@lru_cache(maxsize=8)
def _use_contexts(masked_sql: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    quoted = [
        (m.start(), m.end())
        for m in re.finditer(r'\[(?:[^\]]|\]\])*\]|"(?:[^"]|"")*"', masked_sql)
    ]
    starts = [a for a, _ in quoted]
    positions, databases = [], []
    for match in re.finditer(
        r'(?im)(?:^|;)[ \t]*USE[ \t]+(\[(?:[^\]]|\]\])+\]|"(?:[^"]|"")+"|[\w$]+)',
        masked_sql,
    ):
        index = bisect_right(starts, match.start()) - 1
        if index >= 0 and match.start() < quoted[index][1]:
            continue
        positions.append(match.end())
        databases.append(parts(match.group(1))[0])
    return tuple(positions), tuple(databases)


def contextual(value: list[str], masked_sql: str, position: int) -> tuple[str, ...]:
    """Honor explicit USE statements, ignoring text inside quoted identifiers."""
    names = tuple(s.lower() for s in value)
    if len(names) >= 3:
        return names
    positions, databases = _use_contexts(masked_sql)
    index = bisect_right(positions, position) - 1
    if index >= 0:
        return (databases[index], *(names if len(names) == 2 else ("", *names)))
    return names


def refines(specific: tuple[str, ...], partial: tuple[str, ...]) -> bool:
    """More qualifiers must preserve every known component, including across gaps."""
    if len(specific) < len(partial):
        return False
    return all(
        not known or known == value
        for known, value in zip(reversed(partial), reversed(specific))
    ) and sum(bool(x) for x in specific) > sum(bool(x) for x in partial)


def combine(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    if not compatible(a, b):
        raise ValueError("Conflicting SQL object scopes")
    size = max(len(a), len(b))
    return tuple(
        x or y for x, y in zip(("",) * (size - len(a)) + a, ("",) * (size - len(b)) + b)
    )


def from_row(row) -> tuple[str, ...]:
    if row["target_identity"]:
        return decode(row["target_identity"])
    # EF-derived accesses and old caller-constructed Access instances.
    qualifier = parts(row["qualifier"]) if row["qualifier"] else ()
    return (*qualifier, row["target_name"].lower())
