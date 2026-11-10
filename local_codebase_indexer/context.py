"""Bounded source context extraction that retains requested focus lines."""
from __future__ import annotations

import os
import sqlite3

from .query_common import _consistent_read
from .scanner import extra_roots

MAX_CONTEXT_CHARS = 24000

def _inside(path: str, base: str) -> bool:
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:
        return False

class ContextTools:
    root: str
    conn: sqlite3.Connection

    # ------------------------------------------------------------------ Tool 3
    @_consistent_read(refresh=True)
    def optimized_context(self, file_path: str, focus_lines: list[int], padding_lines: int = 15) -> dict:
        abs_path = os.path.abspath(os.path.join(self.root, file_path))
        allowed = [self.root, *extra_roots(self.root)]
        if not any(_inside(abs_path, r) for r in allowed):
            raise ValueError("file_path must stay inside the project root or an extra root")
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(file_path)
        if not focus_lines:
            raise ValueError("focus_lines must contain at least one line number")
        pad = max(0, int(padding_lines if padding_lines is not None else 15))

        from .textdecode import iter_decoded_lines

        focuses = sorted({int(line) for line in focus_lines if int(line) >= 1})
        if not focuses:
            raise ValueError('focus_lines must contain positive line numbers')
        pad_ranges = [(max(1, line - pad), line + pad) for line in focuses]
        wanted: dict[int, str] = {}
        total = 0
        with open(abs_path, 'rb') as stream:
            for total, decoded in enumerate(iter_decoded_lines(stream), start=1):
                if any(a <= total <= b for a, b in pad_ranges):
                    wanted[total] = decoded
        if not any(line <= total for line in focuses):
            raise ValueError('All focus lines are outside the file')

        width = len(str(total))
        selected = {}
        clipped = []
        remaining = MAX_CONTEXT_CHARS
        separator = '\n   ...\n'
        # Reserve enough space for separators even when selected lines are disjoint.
        overhead = len(separator) + 1
        valid_focuses = [line for line in focuses if line in wanted]
        for i, line in enumerate(valid_focuses):
            text = f"{line:>{width}} | {wanted[line]}"
            allowance = max(0, remaining // (len(valid_focuses) - i) - overhead)
            if len(text) > allowance:
                clipped.append(line)
                marker = ' ... [line truncated]'
                text = text[:max(0, allowance - len(marker))] + marker[:allowance]
            if text:
                selected[line] = text
                remaining -= len(text) + overhead
        neighbors = sorted((n for n in wanted if n not in selected),
                           key=lambda n: (min(abs(n - line) for line in valid_focuses), n))
        for line in neighbors:
            text = f"{line:>{width}} | {wanted[line]}"
            if len(text) + overhead <= remaining:
                selected[line] = text
                remaining -= len(text) + overhead
        merged: list[list[int]] = []
        for line in sorted(selected):
            if merged and line == merged[-1][1] + 1:
                merged[-1][1] = line
            else:
                merged.append([line, line])
        parts = ['\n'.join(selected[n] for n in range(a, b + 1)) for a, b in merged]
        truncated = bool(clipped) or len(selected) < len(wanted)
        symbols = [
            {"symbol": r["qualified_name"], "type": r["symbol_type"], "start_line": r["start_line"], "end_line": r["end_line"]}
            for r in self.conn.execute(
                "SELECT * FROM symbols WHERE file_path=? AND symbol_type != 'namespace' AND start_line<=? AND end_line>=? "
                "ORDER BY (end_line-start_line) LIMIT 5",
                (file_path.replace("\\", "/"), max(valid_focuses), min(valid_focuses)))
        ]
        return {
            "file_path": file_path, "total_lines": total, "ranges": merged,
            "enclosing_symbols": symbols, "truncated": truncated,
            "omitted_lines": len(wanted) - len(selected), "truncated_lines": clipped,
            "context": separator.join(parts),
        }

