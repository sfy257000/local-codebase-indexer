"""Bounded in-process vector cache, tied to committed index revisions."""

from __future__ import annotations

import math
import os

import numpy as np

from . import db


class VectorCache:
    def __init__(self) -> None:
        budget = float(os.environ.get("INDEXER_VECTOR_CACHE_MB", "128"))
        if not math.isfinite(budget * 1024 * 1024) or budget < 0:
            raise ValueError(
                "INDEXER_VECTOR_CACHE_MB must be a finite nonnegative number; 0 disables caching"
            )
        self.budget = int(budget * 1024 * 1024)
        self.key: tuple[str | None, str | None, str | None] | None = None
        self.value: tuple[np.ndarray, np.ndarray] | None = None
        self.hits = 0

    def clear(self):
        self.key, self.value = None, None

    def get(self, conn, scope: set[int] | None = None):
        key = (
            db.get_meta(conn, "index_revision"),
            db.get_meta(conn, "embedder"),
            db.get_meta(conn, "index_version"),
        )
        if db.get_meta(conn, "indexing") == "1":
            self.clear()
        elif self.key == key and self.value is not None:
            self.hits += 1
            ids, matrix = self.value
            if scope is not None:
                selected = np.isin(ids, list(scope))
                return ids[selected], matrix[selected]
            return self.value
        if self.key != key:
            self.clear()
        where = (
            " WHERE rowid IN (SELECT rowid FROM search_scope)"
            if scope is not None
            else ""
        )
        rows = conn.execute(
            "SELECT rowid,chunk_embedding FROM vss_chunks" + where + " ORDER BY rowid"
        ).fetchall()
        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        matrix = (
            np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(
                len(rows), -1
            )
            if rows
            else np.empty((0, 0), dtype=np.float32)
        )
        if (
            self.budget > 0
            and scope is None
            and ids.nbytes + matrix.nbytes <= self.budget
            and db.get_meta(conn, "indexing") != "1"
        ):
            self.key, self.value = key, (ids, matrix)
        return ids, matrix
