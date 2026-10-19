"""Project scanning that respects .gitignore (root and nested)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterator

import pathspec

MAX_FILE_BYTES = 5 * 1024 * 1024  # index.md: never slurp files larger than 5MB

ALWAYS_IGNORE_DIRS = {
    ".git", ".svn", ".hg", ".ai_context", ".claude", "node_modules", "bin", "obj",
    "__pycache__", ".vs", ".idea", ".vscode", "dist", "build", ".venv", "venv",
    "packages", ".pytest_cache", ".mypy_cache",
}

LANGUAGE_BY_EXT = {
    ".cs": "csharp",
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".sql": "sql",
    # indexed as plain text windows
    ".vb": "text", ".bas": "text", ".frm": "text", ".cls": "text",
    ".cshtml": "text", ".aspx": "text", ".asmx": "text", ".vue": "text", ".html": "text",
    ".css": "text", ".xml": "text", ".config": "text", ".csproj": "text", ".json": "text",
    ".yml": "text", ".yaml": "text", ".md": "text", ".txt": "text", ".sh": "text",
    ".ps1": "text", ".ahk": "text", ".iss": "text",
}


@dataclass
class ScannedFile:
    rel_path: str  # posix style, relative to root
    abs_path: str
    mtime_ns: int
    size: int
    language: str


def language_for(path: str) -> str | None:
    return LANGUAGE_BY_EXT.get(os.path.splitext(path)[1].lower())


def _ignored(path: str, is_dir: bool, specs: list) -> bool:
    for base, spec in specs:
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        if is_dir:
            rel += "/"
        if spec.match_file(rel):
            return True
    return False


def iter_files(root: str, max_bytes: int = MAX_FILE_BYTES) -> Iterator[ScannedFile]:
    root = os.path.abspath(root)
    stack: list[tuple[str, list]] = [(root, [])]
    while stack:
        directory, specs = stack.pop()
        gi = os.path.join(directory, ".gitignore")
        if os.path.isfile(gi):
            try:
                with open(gi, encoding="utf-8", errors="ignore") as f:
                    specs = specs + [(directory, pathspec.GitIgnoreSpec.from_lines(f.read().splitlines()))]
            except OSError:
                pass
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for e in entries:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                is_file = e.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if e.name in ALWAYS_IGNORE_DIRS or _ignored(e.path, True, specs):
                    continue
                subdirs.append((e.path, specs))
            elif is_file:
                lang = language_for(e.name)
                if lang is None or _ignored(e.path, False, specs):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                if st.st_size > max_bytes:
                    continue
                yield ScannedFile(
                    rel_path=os.path.relpath(e.path, root).replace(os.sep, "/"),
                    abs_path=e.path,
                    mtime_ns=st.st_mtime_ns,
                    size=st.st_size,
                    language=lang,
                )
        stack.extend(reversed(subdirs))
