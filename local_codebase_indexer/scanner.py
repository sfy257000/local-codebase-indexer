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


# vendored / generated files that are committed, so .gitignore does not exclude them
DEFAULT_IGNORE = ["*.min.js", "*.min.css", "*-min.js"]
# project-specific extra excludes (gitignore syntax), e.g. third-party scripts checked into the repo
PROJECT_IGNORE_FILE = os.path.join(".ai_context", "ignore")
# folders outside the project to index too (one per line, relative to the root or absolute), e.g. exported SQL
EXTRA_ROOTS_FILE = os.path.join(".ai_context", "extra_roots")


@dataclass
class ScannedFile:
    rel_path: str  # posix style, relative to root; absolute for files under an extra root
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


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()
    except OSError:
        return []


def extra_roots(root: str) -> list[str]:
    """Existing folders listed in ``.ai_context/extra_roots`` (blank lines and ``#`` comments skipped)."""
    root = os.path.abspath(root)
    out = []
    for line in _read_lines(os.path.join(root, EXTRA_ROOTS_FILE)):
        line = line.strip()
        if line and not line.startswith("#"):
            p = os.path.abspath(os.path.join(root, line))
            if os.path.isdir(p) and p not in out:
                out.append(p)
    return out


def iter_files(root: str, max_bytes: int = MAX_FILE_BYTES) -> Iterator[ScannedFile]:
    root = os.path.abspath(root)
    spec = pathspec.GitIgnoreSpec.from_lines(DEFAULT_IGNORE + _read_lines(os.path.join(root, PROJECT_IGNORE_FILE)))
    yield from _walk(root, [(root, spec)], lambda p: os.path.relpath(p, root).replace(os.sep, "/"), max_bytes)
    for extra in extra_roots(root):
        yield from _walk(extra, [(extra, pathspec.GitIgnoreSpec.from_lines(DEFAULT_IGNORE))],
                         lambda p: p.replace(os.sep, "/"), max_bytes)


def _walk(top: str, specs: list, rel, max_bytes: int) -> Iterator[ScannedFile]:
    stack: list[tuple[str, list]] = [(top, specs)]
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
                    rel_path=rel(e.path),
                    abs_path=e.path,
                    mtime_ns=st.st_mtime_ns,
                    size=st.st_size,
                    language=lang,
                )
        stack.extend(reversed(subdirs))
