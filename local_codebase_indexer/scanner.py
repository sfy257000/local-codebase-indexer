"""Project scanning that respects .gitignore (root and nested)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterator

import pathspec

from .textdecode import is_binary_data

MAX_FILE_BYTES = 5 * 1024 * 1024  # index.md: never slurp files larger than 5MB

HARD_IGNORE_DIRS = {".git", ".svn", ".hg", ".ai_context"}
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


def _ignored(path: str, is_dir: bool, specs: list, override=None) -> bool:
    ignored = False
    for base, spec in [*specs, *([override] if override else [])]:
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        if is_dir:
            rel += "/"
        match = spec.check_file(rel).include
        if match is not None:
            ignored = match
    return ignored


def _explicit_include(path: str, is_dir: bool, override) -> bool:
    if override is None:
        return False
    base, spec = override
    relative = os.path.relpath(path, base).replace(os.sep, '/') + ('/' if is_dir else '')
    return spec.check_file(relative).include is False


@dataclass
class ScanReport:
    counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)

    def record(self, path: str, reason: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1
        sample = self.examples.setdefault(reason, [])
        if len(sample) < 10:
            sample.append(path)


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


def iter_files(root: str, max_bytes: int = MAX_FILE_BYTES, report: ScanReport | None = None) -> Iterator[ScannedFile]:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise ValueError(f"project root is not an existing directory: {root}")
    spec = pathspec.GitIgnoreSpec.from_lines(DEFAULT_IGNORE)
    override = (root, pathspec.GitIgnoreSpec.from_lines(_read_lines(os.path.join(root, PROJECT_IGNORE_FILE))))
    yield from _walk(root, [(root, spec)], lambda p: os.path.relpath(p, root).replace(os.sep, "/"), max_bytes, report, override)
    for extra in extra_roots(root):
        yield from _walk(extra, [(extra, pathspec.GitIgnoreSpec.from_lines(DEFAULT_IGNORE))],
                         lambda p: p.replace(os.sep, "/"), max_bytes, report)


def _walk(top: str, specs: list, rel, max_bytes: int, report: ScanReport | None = None, override=None) -> Iterator[ScannedFile]:
    def record(path, reason):
        if report is not None:
            report.record(rel(path), reason)
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
            record(directory, "unreadable_directory")
            continue
        subdirs = []
        for e in entries:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                is_file = e.is_file(follow_symlinks=False)
            except OSError:
                record(e.path, "unreadable")
                continue
            if is_dir:
                if e.name in HARD_IGNORE_DIRS or (e.name in ALWAYS_IGNORE_DIRS and not _explicit_include(e.path, True, override)):
                    record(e.path, "excluded_directory")
                    continue
                if _ignored(e.path, True, specs, override):
                    record(e.path, "ignored_directory")
                    continue
                subdirs.append((e.path, specs))
            elif is_file:
                lang = language_for(e.name)
                if _ignored(e.path, False, specs, override):
                    record(e.path, "ignored_file")
                    continue
                if lang is None:
                    record(e.path, "unsupported_extension")
                    continue
                try:
                    st = e.stat()
                except OSError:
                    record(e.path, "unreadable")
                    continue
                if st.st_size > max_bytes:
                    record(e.path, "too_large")
                    continue
                try:
                    with open(e.path, 'rb') as stream:
                        binary = is_binary_data(stream.read(8192))
                except OSError:
                    binary = False  # Let indexing record the read failure and retry it.
                if binary:
                    record(e.path, 'binary')
                    continue
                record(e.path, "eligible")
                yield ScannedFile(
                    rel_path=rel(e.path),
                    abs_path=e.path,
                    mtime_ns=st.st_mtime_ns,
                    size=st.st_size,
                    language=lang,
                )
            else:
                record(e.path, "symlink_or_special_file")
        stack.extend(reversed(subdirs))


def explain_path(root: str, path: str) -> dict:
    """Explain a file without traversing excluded directories or reading its full contents."""
    root = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root, path))
    bases = [b for b in [root, *extra_roots(root)] if os.path.commonpath([b, target]) == b]
    if not bases:
        raise ValueError("path must stay inside the project root or an extra root")
    base = max(bases, key=len)
    relative = os.path.relpath(target, base)
    specs = [(base, pathspec.GitIgnoreSpec.from_lines(DEFAULT_IGNORE))]
    override = (base, pathspec.GitIgnoreSpec.from_lines(_read_lines(os.path.join(root, PROJECT_IGNORE_FILE)))) if base == root else None
    current = base
    for part in relative.split(os.sep):
        specs.append((current, pathspec.GitIgnoreSpec.from_lines(_read_lines(os.path.join(current, '.gitignore')))))
        current = os.path.join(current, part)
        if os.path.islink(current):
            reason = 'symlink_or_special_file'
            break
        if not os.path.exists(current):
            reason = 'missing'
            break
        is_dir = os.path.isdir(current)
        if is_dir and (part in HARD_IGNORE_DIRS or (part in ALWAYS_IGNORE_DIRS and not _explicit_include(current, True, override))):
            reason = 'excluded_directory'
            break
        if _ignored(current, is_dir, specs, override):
            reason = 'ignored_directory' if is_dir else 'ignored_file'
            break
    else:
        if os.path.isdir(target):
            reason = 'directory'
        elif language_for(target) is None:
            reason = 'unsupported_extension'
        elif os.path.getsize(target) > MAX_FILE_BYTES:
            reason = 'too_large'
        else:
            try:
                with open(target, 'rb') as f:
                    reason = 'binary' if is_binary_data(f.read(8192)) else 'eligible'
            except OSError:
                reason = 'unreadable'
    return {'path': path, 'reason': reason, 'eligible': reason == 'eligible'}
