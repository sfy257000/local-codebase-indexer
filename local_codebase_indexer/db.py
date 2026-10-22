"""SQLite storage layer: schema from index.md section 2 plus a few helper tables."""

from __future__ import annotations

import os
import sqlite3

INDEX_DIR_NAME = ".ai_context"
DB_NAME = "indexer.db"

SCHEMA = """
-- 1. File metadata cache
CREATE TABLE IF NOT EXISTS files (
    file_path TEXT PRIMARY KEY,
    last_modified INTEGER NOT NULL,
    md5_hash TEXT NOT NULL,
    language TEXT NOT NULL
);

-- 2. Syntactic symbol table (AST extraction)
CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    symbol_type TEXT NOT NULL,
    parent_id TEXT,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    signature TEXT,
    qualified_name TEXT,
    type_name TEXT,
    min_args INTEGER,
    max_args INTEGER,
    FOREIGN KEY(file_path) REFERENCES files(file_path)
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(symbol_name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);

-- 3. Dependency topology (file level)
CREATE TABLE IF NOT EXISTS dependencies (
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    dependency_type TEXT NOT NULL,
    PRIMARY KEY (source_path, target_path, dependency_type)
);
CREATE INDEX IF NOT EXISTS idx_dep_target ON dependencies(target_path);

-- Raw, unresolved references captured from the AST (import / base / ident).
-- Dependencies and symbol relations are resolved from this table.
CREATE TABLE IF NOT EXISTS raw_refs (
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    line INTEGER NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (file_path, kind, name, owner)
);
CREATE INDEX IF NOT EXISTS idx_refs_name ON raw_refs(kind, name);

-- 5. Chunk metadata mapping
CREATE TABLE IF NOT EXISTS chunk_metadata (
    rowid INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    symbol_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunk_file ON chunk_metadata(file_path);

-- 4. Vector storage. index.md names this vss_chunks (sqlite-vss); this
-- implementation stores float32 BLOBs and does cosine search with numpy so no
-- native SQLite extension is required.
CREATE TABLE IF NOT EXISTS vss_chunks (
    rowid INTEGER PRIMARY KEY,
    chunk_embedding BLOB NOT NULL
);

-- Keyword layer (FTS5) over chunk text; rowid == chunk_metadata.rowid
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(search_text, tokenize='unicode61');

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Data access: which table / procedure a SQL object or C# method reads, writes, executes or maps.
-- target_key is the lower-case last name segment; "?" marks dynamic SQL whose target is unknown.
CREATE TABLE IF NOT EXISTS data_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    source_id TEXT,
    line INTEGER NOT NULL,
    target_key TEXT NOT NULL,
    target_name TEXT NOT NULL,
    qualifier TEXT NOT NULL DEFAULT '',
    target_kind TEXT NOT NULL,
    access TEXT NOT NULL,
    op TEXT NOT NULL,
    via TEXT NOT NULL,
    confidence TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_da_target ON data_access(target_key);
CREATE INDEX IF NOT EXISTS idx_da_file ON data_access(file_path);
CREATE INDEX IF NOT EXISTS idx_da_source ON data_access(source_id);

-- C# call sites as seen in the syntax tree, and the resolved symbol-level call graph derived from them.
CREATE TABLE IF NOT EXISTS call_sites (
    file_path TEXT NOT NULL,
    caller_id TEXT,
    line INTEGER NOT NULL,
    name TEXT NOT NULL,
    argc INTEGER NOT NULL,
    recv_kind TEXT NOT NULL,
    recv_name TEXT NOT NULL DEFAULT '',
    recv_type TEXT NOT NULL DEFAULT '',
    recv_expr TEXT NOT NULL DEFAULT '',
    is_ctor INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cs_file ON call_sites(file_path);
CREATE TABLE IF NOT EXISTS calls (
    caller_id TEXT NOT NULL,
    callee_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_id);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);

-- Files that could not be indexed on the last attempt. They are retried on every update.
CREATE TABLE IF NOT EXISTS index_failures (
    file_path TEXT PRIMARY KEY,
    error TEXT NOT NULL,
    failed_at INTEGER NOT NULL
);
"""

# Bump when the stored data layout or parsing output changes; a mismatch triggers a full rebuild.
INDEX_VERSION = "4"


def index_dir(root: str) -> str:
    return os.path.join(root, INDEX_DIR_NAME)


def default_db_path(root: str) -> str:
    return os.path.join(index_dir(root), DB_NAME)


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # keep the index out of version control
    gi = os.path.join(os.path.dirname(db_path), ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as f:
            f.write("*\n")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that older index files lack (the index version check then rebuilds their content)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(symbols)")}
    for col, decl in (("type_name", "TEXT"), ("min_args", "INTEGER"), ("max_args", "INTEGER")):
        if col not in have:
            conn.execute(f"ALTER TABLE symbols ADD COLUMN {col} {decl}")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def clear_all(conn: sqlite3.Connection) -> None:
    for t in ("files", "symbols", "dependencies", "raw_refs", "chunk_metadata", "vss_chunks", "chunks_fts",
              "index_failures", "data_access", "call_sites", "calls"):
        conn.execute(f"DELETE FROM {t}")
