"""Incremental indexing pipeline (index.md section 3, step 1)."""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import sqlite3
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field

from . import db as dbmod
from .callgraph import resolve_calls
from .embedder import get_embedder, split_identifier
from .langparse import CLASS_LIKE, parse_file
from .scanner import iter_files

_TYPE_KINDS = tuple(CLASS_LIKE)


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "big5", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def cjk_bigrams(run: str) -> list[str]:
    """Overlapping character bigrams of a Chinese run (the run itself when it is a single character)."""
    return [run[i:i + 2] for i in range(len(run) - 1)] or [run]


def search_text_for(header: str, body: str) -> str:
    """Text for FTS5: original tokens plus camelCase / snake_case sub-words."""
    text = header + "\n" + body
    extra = []
    for ident in set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)):
        parts = split_identifier(ident)
        if len(parts) > 1:
            extra.extend(parts)
    # FTS5's unicode61 tokenizer keeps a whole run of Chinese characters as one token,
    # so index overlapping character bigrams as separate words
    for run in set(_CJK_RUN.findall(text)):
        extra.extend(cjk_bigrams(run))
    return text + "\n" + " ".join(extra)


def embedding_text_for(header: str, body: str) -> str:
    return (header + "\n" + body)[:2500]


def git_info(root: str) -> dict | None:
    """HEAD commit, branch and number of uncommitted paths; None outside a git work tree."""
    def run(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    head = run("rev-parse", "HEAD")
    if head is None:
        return None
    branch = (run("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    porcelain = run("status", "--porcelain") or ""
    return {
        "commit": head.strip(),
        "branch": branch,
        "dirty_files": len([ln for ln in porcelain.splitlines() if ln.strip()]),
    }


@dataclass
class IndexStats:
    scanned: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    chunks: int = 0
    symbols: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Indexer:
    def __init__(self, root: str, db_path: str | None = None, embedder=None):
        self.root = os.path.abspath(root)
        self.db_path = db_path or dbmod.default_db_path(self.root)
        self.conn = dbmod.connect(self.db_path)
        self.embedder = embedder or get_embedder()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ public
    def update(self, force: bool = False) -> IndexStats:
        stats = IndexStats()
        conn = self.conn

        if (dbmod.get_meta(conn, "embedder") != self.embedder.name
                or dbmod.get_meta(conn, "index_version") != dbmod.INDEX_VERSION):
            # different embedding space or data layout (or first run): rebuild everything
            dbmod.clear_all(conn)
            dbmod.set_meta(conn, "embedder", self.embedder.name)
            dbmod.set_meta(conn, "index_version", dbmod.INDEX_VERSION)
            conn.commit()

        existing = {r["file_path"]: (r["last_modified"], r["md5_hash"]) for r in
                    conn.execute("SELECT file_path, last_modified, md5_hash FROM files")}
        seen: set[str] = set()
        changed = False

        for sf in iter_files(self.root):
            stats.scanned += 1
            seen.add(sf.rel_path)
            prev = existing.get(sf.rel_path)
            if prev and not force and prev[0] == sf.mtime_ns:
                stats.unchanged += 1
                continue
            try:
                with open(sf.abs_path, "rb") as f:
                    data = f.read()
            except OSError as exc:
                self._record_failure(sf.rel_path, f"{type(exc).__name__}: {exc}")
                stats.failed += 1
                changed = True
                stats.warnings.append(f"{sf.rel_path}: {exc}")
                continue
            if b"\x00" in data[:8192]:
                stats.skipped += 1
                continue
            md5 = hashlib.md5(data).hexdigest()
            if prev and not force and prev[1] == md5:
                conn.execute("UPDATE files SET last_modified=? WHERE file_path=?", (sf.mtime_ns, sf.rel_path))
                stats.unchanged += 1
                continue
            try:
                self._index_file(sf.rel_path, sf.language, sf.mtime_ns, md5, decode_bytes(data))
            except Exception as exc:  # one bad file must not abort the run
                self._record_failure(sf.rel_path, f"{type(exc).__name__}: {exc}")
                stats.failed += 1
                changed = True
                stats.warnings.append(f"{sf.rel_path}: {type(exc).__name__}: {exc}")
                continue
            changed = True
            if prev:
                stats.updated += 1
            else:
                stats.added += 1

        for gone in set(existing) - seen:
            self._remove_file(gone)
            stats.deleted += 1
            changed = True

        # failures for files that are no longer scanned (deleted, newly ignored) are obsolete
        conn.executemany("DELETE FROM index_failures WHERE file_path=?",
                         [(r[0],) for r in conn.execute("SELECT file_path FROM index_failures").fetchall()
                          if r[0] not in seen])
        if changed or force:
            resolve_dependencies(conn)
            resolve_calls(conn)
        self._record_run_meta()
        conn.commit()
        stats.chunks = conn.execute("SELECT COUNT(*) FROM chunk_metadata").fetchone()[0]
        stats.symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        return stats

    # ------------------------------------------------------------------ internals
    def _record_run_meta(self) -> None:
        conn = self.conn
        dbmod.set_meta(conn, "last_indexed_at", str(int(time.time())))
        info = git_info(self.root)
        if info is None:
            for k in ("git_commit", "git_branch", "git_dirty_files"):
                conn.execute("DELETE FROM meta WHERE key=?", (k,))
        else:
            dbmod.set_meta(conn, "git_commit", info["commit"])
            dbmod.set_meta(conn, "git_branch", info["branch"])
            dbmod.set_meta(conn, "git_dirty_files", str(info["dirty_files"]))

    def _record_failure(self, rel_path: str, error: str) -> None:
        """A file that cannot be indexed must not leave its previous (now stale) data behind."""
        with self.conn:
            self._remove_file(rel_path)
            self.conn.execute(
                "INSERT OR REPLACE INTO index_failures(file_path, error, failed_at) VALUES(?,?,?)",
                (rel_path, error[:500], int(time.time())),
            )

    def _remove_file(self, rel_path: str) -> None:
        c = self.conn
        ids = [r[0] for r in c.execute("SELECT rowid FROM chunk_metadata WHERE file_path=?", (rel_path,))]
        c.executemany("DELETE FROM vss_chunks WHERE rowid=?", [(i,) for i in ids])
        c.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
        c.execute("DELETE FROM chunk_metadata WHERE file_path=?", (rel_path,))
        c.execute("DELETE FROM symbols WHERE file_path=?", (rel_path,))
        c.execute("DELETE FROM raw_refs WHERE file_path=?", (rel_path,))
        c.execute("DELETE FROM data_access WHERE file_path=?", (rel_path,))
        c.execute("DELETE FROM call_sites WHERE file_path=?", (rel_path,))
        c.execute("DELETE FROM files WHERE file_path=?", (rel_path,))
        c.execute("DELETE FROM index_failures WHERE file_path=?", (rel_path,))

    def _index_file(self, rel_path: str, language: str, mtime_ns: int, md5: str, text: str) -> None:
        parsed = parse_file(language, text, rel_path)
        embeddings = self.embedder.embed(
            [embedding_text_for(ch.header, ch.text) for ch in parsed.chunks]
        )
        c = self.conn
        with c:
            self._remove_file(rel_path)
            c.execute(
                "INSERT INTO files(file_path, last_modified, md5_hash, language) VALUES(?,?,?,?)",
                (rel_path, mtime_ns, md5, language),
            )
            c.executemany(
                "INSERT OR REPLACE INTO symbols(id, file_path, symbol_name, symbol_type, parent_id,"
                " start_line, end_line, signature, qualified_name, type_name, min_args, max_args)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(s.id, rel_path, s.name, s.type, s.parent_id, s.start, s.end, s.signature, s.qname,
                  s.type_name or None, s.min_args, s.max_args)
                 for s in parsed.symbols],
            )
            c.executemany(
                "INSERT INTO data_access(file_path, source_id, line, target_key, target_name, qualifier,"
                " target_kind, access, op, via, confidence, evidence, detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(rel_path, a.source_id, a.line, a.key, a.name, a.qualifier, a.kind, a.access, a.op, a.via,
                  a.confidence, a.evidence, a.detail) for a in parsed.accesses],
            )
            c.executemany(
                "INSERT INTO call_sites(file_path, caller_id, line, name, argc, recv_kind, recv_name, recv_type,"
                " recv_expr, is_ctor) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(rel_path, cs.caller_id, cs.line, cs.name, cs.argc, cs.recv_kind, cs.recv_name, cs.recv_type,
                  cs.recv_expr, int(cs.is_ctor)) for cs in parsed.call_sites],
            )
            c.executemany(
                "INSERT OR IGNORE INTO raw_refs(file_path, kind, name, line, owner) VALUES(?,?,?,?,?)",
                [(rel_path, r.kind, r.name, r.line, r.owner) for r in parsed.refs],
            )
            for ch, emb in zip(parsed.chunks, embeddings):
                cur = c.execute(
                    "INSERT INTO chunk_metadata(file_path, start_line, end_line, raw_text, symbol_id)"
                    " VALUES(?,?,?,?,?)",
                    (rel_path, ch.start, ch.end, ch.text, ch.symbol_id),
                )
                rid = cur.lastrowid
                c.execute("INSERT INTO vss_chunks(rowid, chunk_embedding) VALUES(?,?)", (rid, emb.tobytes()))
                c.execute("INSERT INTO chunks_fts(rowid, search_text) VALUES(?,?)",
                          (rid, search_text_for(ch.header, ch.text)))


# ---------------------------------------------------------------------- dependency resolution

def _resolve_relative(source: str, spec: str, files: set[str]) -> str | None:
    base = posixpath.normpath(posixpath.join(posixpath.dirname(source), spec))
    for cand in (base, *(base + e for e in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".py", ".cs")),
                 *(posixpath.join(base, "index" + e) for e in (".ts", ".tsx", ".js", ".jsx"))):
        if cand in files:
            return cand
    return None


def resolve_dependencies(conn: sqlite3.Connection) -> None:
    """Rebuild the file-level dependency graph from raw_refs and symbols."""
    conn.execute("DELETE FROM dependencies")
    files = {r[0] for r in conn.execute("SELECT file_path FROM files")}

    type_files: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute(
        f"SELECT symbol_name, file_path FROM symbols WHERE symbol_type IN ({','.join('?' * len(_TYPE_KINDS))})",
        _TYPE_KINDS,
    ):
        type_files[r[0]].add(r[1])

    ns_files: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT symbol_name, file_path FROM symbols WHERE symbol_type='namespace'"):
        ns_files[r[0]].add(r[1])

    deps: set[tuple[str, str, str]] = set()

    for r in conn.execute("SELECT file_path, name FROM raw_refs WHERE kind='import'"):
        src, name = r[0], r[1]
        targets: set[str] = set()
        if name.startswith("."):
            t = _resolve_relative(src, name, files)
            if t:
                targets.add(t)
        elif name in ns_files:
            targets |= ns_files[name]
        else:
            slug = name.replace(".", "/")
            for cand in (slug + ".py", slug + "/__init__.py"):
                targets |= {f for f in files if f == cand or f.endswith("/" + cand)}
        for t in targets:
            if t != src:
                deps.add((src, t, "import"))

    for r in conn.execute("SELECT file_path, name FROM raw_refs WHERE kind='base'"):
        for t in type_files.get(r[1], ()):
            if t != r[0]:
                deps.add((r[0], t, "inheritance"))

    for r in conn.execute("SELECT DISTINCT file_path, name FROM raw_refs WHERE kind='ident'"):
        targets = type_files.get(r[1])
        if targets and len(targets) <= 5:
            for t in targets:
                if t != r[0]:
                    deps.add((r[0], t, "invocation"))

    conn.executemany("INSERT OR IGNORE INTO dependencies VALUES(?,?,?)", sorted(deps))
