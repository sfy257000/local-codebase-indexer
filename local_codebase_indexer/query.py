"""Query facade, hybrid search, index health, and data-access orchestration."""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import sqlite3
import textwrap
import time
from collections import defaultdict

import numpy as np

from . import __version__, dataflow
from . import db as dbmod
from .context import MAX_CONTEXT_CHARS as MAX_CONTEXT_CHARS
from .context import ContextTools
from .core import Indexer, cjk_bigrams, git_info
from .embedder import split_identifier
from .pagination import decode_cursor, encode_cursor, search_key
from .query_common import _consistent_read
from .relations import RelationTools
from .rendering import (
    render_context as render_context,
)
from .rendering import (
    render_data_access as render_data_access,
)
from .rendering import (
    render_doctor as render_doctor,
)
from .rendering import (
    render_impact as render_impact,
)
from .rendering import (
    render_relations as render_relations,
)
from .rendering import (
    render_search as render_search,
)
from .rendering import (
    render_status as render_status,
)
from .scanner import ScanReport, explain_path, iter_files
from .synonyms import Expander

RRF_K = 60
RECHECK_SECONDS = 15  # a long-lived server re-checks the index at most this often
_STARTED = time.time()  # when this process loaded the package
# the symbol-name layer is a weak signal (substring match), so it counts for less
LAYER_WEIGHTS = {"keyword": 1.0, "vector": 1.0, "symbol": 0.5}
_TYPE_KINDS = ("class", "interface", "struct", "record", "enum", "type")
_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "is", "are", "for", "and", "or", "where", "do", "we", "how",
    "what", "which", "that", "this", "with", "on", "it", "be", "does", "can", "find", "show", "me",
    "by", "at", "as", "from", "into", "i", "my", "our", "there", "when", "who", "so", "up",
}


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[一-鿿]+|\d+", query):
        if not re.match(r"[A-Za-z_0-9]", ident):
            terms.extend(cjk_bigrams(ident))  # matches the bigrams indexed in chunks_fts
            continue
        parts = split_identifier(ident) if re.match(r"[A-Za-z_]", ident) else [ident.lower()]
        if len(parts) > 1:
            terms.append(ident.lower())
        terms.extend(parts)
    out = []
    for t in terms:
        if t not in _STOPWORDS and t not in out and len(t) > 1:
            out.append(t)
    return out


def _server_info() -> dict:
    """Version of the running code and whether its source changed on disk after it was loaded (a long-lived MCP
    server keeps running the old code until it is restarted)."""
    pkg = os.path.dirname(os.path.abspath(__file__))
    try:
        changed = any(os.path.getmtime(os.path.join(pkg, f)) > _STARTED for f in os.listdir(pkg) if f.endswith(".py"))
    except OSError:
        changed = False
    return {"version": __version__, "code_changed_since_start": changed}



def _has_word(term: str, text: str) -> bool:
    """True if ``term`` equals the text or one of its camelCase / snake_case / path words."""
    low = text.lower()
    if term == low:
        return True
    if not term.isascii():  # Chinese: no word boundaries, use substring
        return term in text
    words: set[str] = set()
    for piece in re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", text):
        words.update(split_identifier(piece))
        words.add(piece.lower())
    return term in words




class CodebaseTools(RelationTools, ContextTools):
    """Facade exposing codebase_semantic_search / get_symbol_relations / get_optimized_context."""

    def __init__(self, root: str, auto_refresh: bool = True, embedder=None, db_path: str | None = None):
        self.indexer = Indexer(root, embedder=embedder, db_path=db_path)
        self.root = self.indexer.root
        self.conn: sqlite3.Connection = self.indexer.conn
        self.embedder = self.indexer.embedder
        self.auto_refresh = auto_refresh
        self.expander = Expander(self.root)
        self._checked_at: float | None = None

    def refresh(self, force: bool = False):
        self.expander.reload()
        stats = self.indexer.update(force=force)
        self._checked_at = time.monotonic()
        return stats

    def _ensure(self) -> None:
        """Incremental refresh before a query; a long-lived MCP server re-checks once the last check is older than
        ``RECHECK_SECONDS`` (a no-op check costs about a second on a few thousand files)."""
        self.expander.reload()
        if hasattr(self.embedder, 'assert_unchanged'):
            self.embedder.assert_unchanged()
        if self.auto_refresh and (self._checked_at is None
                                  or time.monotonic() - self._checked_at > RECHECK_SECONDS):
            self.refresh()

    # ------------------------------------------------------------------ Tool 1
    @_consistent_read(refresh=True)
    def semantic_search(self, query: str, limit: int = 5, related: bool = False,
                        file_pattern: str | None = None, language: str | None = None, offset: int = 0,
                        cursor: str | None = None, explain: bool = False) -> dict:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonempty string")
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        from .scanner import LANGUAGE_BY_EXT
        if language is not None and language not in set(LANGUAGE_BY_EXT.values()):
            raise ValueError("unsupported language; choose " + ", ".join(sorted(set(LANGUAGE_BY_EXT.values()))))
        limit = max(1, min(int(limit or 5), 50))
        revision = dbmod.get_meta(self.conn, 'index_revision') or 'unindexed'
        key = search_key(query, file_pattern, language, self.expander.version, self.embedder.name)
        if cursor is not None:
            if offset:
                raise ValueError('Use cursor or offset, not both.')
            offset = decode_cursor(cursor, revision, key)
        wanted = offset + limit + 1
        scope = None
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS search_scope (rowid INTEGER PRIMARY KEY)")
        self.conn.execute("DELETE FROM search_scope")
        if file_pattern or language:
            scope = {r['rowid'] for r in self.conn.execute(
                "SELECT c.rowid, c.file_path, f.language FROM chunk_metadata c JOIN files f ON f.file_path=c.file_path")
                if (not file_pattern or fnmatch.fnmatchcase(r['file_path'], file_pattern))
                and (not language or r['language'] == language)}
            self.conn.executemany("INSERT INTO search_scope VALUES(?)", [(rid,) for rid in scope])
        scope_sql = " AND rowid IN (SELECT rowid FROM search_scope)" if scope is not None else ""
        chunk_scope = " AND c.rowid IN (SELECT rowid FROM search_scope)" if scope is not None else ""
        # Rank independently of page size/offset; changing candidate pools changes RRF scores.
        mined = self._mined_expansion(query)
        extra = list(dict.fromkeys(self.expander.expand(query) + mined))
        vector_extra = list(dict.fromkeys(self.expander.expand(query, for_embedding=True) + mined))
        terms = _query_terms(query)
        for t in _query_terms(" ".join(extra)):
            if t not in terms:
                terms.append(t)
        ranked: dict[str, list[int]] = {"keyword": [], "symbol": [], "vector": []}
        ranks_override: dict[str, dict[int, int]] = {}
        exact_ids: set[int] = set()  # chunks whose symbol name contains a full identifier typed in the query

        # keyword layer: FTS5 / bm25 over chunk text
        if terms:
            match = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
            try:
                rows = self.conn.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?" + scope_sql + " ORDER BY bm25(chunks_fts), rowid",
                    (match,),
                ).fetchall()
                ranked["keyword"] = [r[0] for r in rows]
            except sqlite3.OperationalError:
                pass

        # symbol-name layer: explicit tokens against symbol / file names (whole sub-word match)
        if terms:
            scores: dict[int, int] = defaultdict(int)
            for t in terms:
                like = f"%{t}%"
                for r in self.conn.execute(
                    "SELECT c.rowid, s.symbol_name, s.qualified_name FROM chunk_metadata c "
                    "JOIN symbols s ON s.id = c.symbol_id "
                    "WHERE (lower(s.symbol_name) LIKE ? OR lower(s.qualified_name) LIKE ?)" + chunk_scope,
                    (like, like),
                ):
                    if _has_word(t, r["symbol_name"]) or _has_word(t, r["qualified_name"] or ""):
                        scores[r[0]] += 1
                for r in self.conn.execute(
                    "SELECT rowid, file_path FROM chunk_metadata WHERE lower(file_path) LIKE ?" + scope_sql + " ORDER BY rowid", (like,)
                ):
                    if _has_word(t, r["file_path"]):
                        scores[r[0]] += 1
            # a full identifier typed by the user (SFC_MLot) must match names containing it whole, whatever the
            # case or underscores (SFC_Mlot, SFC_MLotRepository); sub-word matches alone would favour every *Lot
            for ident in self._whole_identifiers(query):
                squashed = re.sub(r"[^a-z0-9]", "", ident.lower())
                like = f"%{squashed}%"
                for r in self.conn.execute(
                    "SELECT c.rowid FROM chunk_metadata c JOIN symbols s ON s.id = c.symbol_id "
                    "WHERE replace(lower(s.symbol_name), '_', '') LIKE ?" + chunk_scope + " ORDER BY c.rowid", (like,)):
                    scores[r[0]] += 3
                    exact_ids.add(r[0])
                for r in self.conn.execute(
                    "SELECT rowid FROM chunk_metadata WHERE replace(lower(file_path), '_', '') LIKE ?" + scope_sql + " ORDER BY rowid", (like,)):
                    scores[r[0]] += 2
                    exact_ids.add(r[0])  # in a file named after it (WD0096Service.cs: SetRecord)
            ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
            ranked["symbol"] = [rid for rid, _ in ordered]
            # equal match counts must share a rank, otherwise arbitrary row order decides
            tie_rank: dict[int, int] = {}
            first_rank: dict[int, int] = {}
            for pos, (rid, sc) in enumerate(ordered, start=1):
                tie_rank[rid] = first_rank.setdefault(sc, pos)
            ranks_override["symbol"] = tie_rank

        # vector layer: cosine similarity over local embeddings
        rows = self.conn.execute("SELECT rowid, chunk_embedding FROM vss_chunks WHERE 1=1" + scope_sql + " ORDER BY rowid").fetchall()
        if rows:
            ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
            mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
            qtext = query + " " + " ".join(vector_extra) if vector_extra else query
            qv = (self.embedder.embed_query(qtext) if hasattr(self.embedder, "embed_query")
                  else self.embedder.embed([qtext])[0])
            if float(np.linalg.norm(qv)) > 0:
                sims = mat @ qv
                top = np.argsort(-sims, kind='stable')
                ranked["vector"] = [int(ids[i]) for i in top if sims[i] > 0]

        layer_scores: dict[int, dict] = {}
        fused: dict[int, float] = defaultdict(float)
        via: dict[int, list[str]] = defaultdict(list)
        for name, lst in ranked.items():
            for rank, rid in enumerate(lst, start=1):
                rank = ranks_override.get(name, {}).get(rid, rank)
                contribution = LAYER_WEIGHTS[name] / (RRF_K + rank)
                fused[rid] += contribution
                if explain:
                    layer_scores.setdefault(rid, {})[name] = {'rank': rank, 'rrf_contribution': round(contribution, 8)}
                via[rid].append(name)
        top_ids = [rid for rid, _ in sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))]
        if exact_ids:  # a typed identifier beats sub-word noise (every *Lot class), ties keep the fused order
            exact = [r for r in top_ids if r in exact_ids]
            # within its scope, names that also match the rest of the query come first ("WD0096 存檔" -> SetRecord)
            idents = {p for i in self._whole_identifiers(query) for p in _query_terms(i)}
            rest = [t for t in terms if t not in idents]
            if rest and exact:
                names = dict(self.conn.execute(
                    f"SELECT c.rowid, s.symbol_name FROM chunk_metadata c JOIN symbols s ON s.id = c.symbol_id "
                    f"WHERE c.rowid IN ({','.join('?' * len(exact))})", exact).fetchall())
                hits = {r: sum(_has_word(t, names.get(r) or "") for t in rest) for r in exact}
                exact.sort(key=lambda r: -hits[r])  # stable: fused order within equal hits
            top_ids = exact + [r for r in top_ids if r not in exact_ids]

        results, seen_spans = [], set()
        routed, bare = self._route_data_access(query)
        routed_hits = []
        allowed_paths = {r[0] for r in self.conn.execute("SELECT file_path FROM files WHERE language=?", (language,))} if language else None
        for acc in routed:
            hit = self._chunk_result(acc)
            if hit is not None and (not file_pattern or fnmatch.fnmatchcase(hit["file_path"], file_pattern)) and (
                    allowed_paths is None or hit['file_path'] in allowed_paths) and (hit["file_path"], hit["start_line"], hit["end_line"]) not in seen_spans:
                seen_spans.add((hit["file_path"], hit["start_line"], hit["end_line"]))
                routed_hits.append(hit)
        if bare and routed_hits and top_ids:
            # only a name was typed: the definition (best ordinary match) first, then who touches it
            first = self._ordinary_hit(top_ids[0], fused, via)
            if first is not None:
                span = (first["file_path"], first["start_line"], first["end_line"])
                same = [h for h in routed_hits if (h["file_path"], h["start_line"], h["end_line"]) == span]
                first = same[0] if same else first  # keep the copy that carries the data access
                routed_hits = [h for h in routed_hits if (h["file_path"], h["start_line"], h["end_line"]) != span]
                results.append(first)
                seen_spans.add(span)
        results.extend(routed_hits)
        for rid in top_ids:
            if len(results) >= wanted:
                break
            row = self.conn.execute(
                "SELECT c.file_path, c.start_line, c.end_line, c.raw_text, c.symbol_id, "
                "s.symbol_type, s.qualified_name, s.signature "
                "FROM chunk_metadata c LEFT JOIN symbols s ON s.id = c.symbol_id WHERE c.rowid=?",
                (rid,),
            ).fetchone()
            if row is None:
                continue
            span = (row["file_path"], row["start_line"], row["end_line"])
            if span in seen_spans:
                continue
            seen_spans.add(span)
            snippet_lines = textwrap.dedent(row["raw_text"].rstrip()).split("\n")
            results.append({
                "file_path": row["file_path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "symbol": row["qualified_name"],
                "symbol_type": row["symbol_type"],
                "signature": row["signature"],
                "score": round(fused[rid], 5),
                "matched_by": via[rid],
                "snippet": "\n".join(snippet_lines[:12]) + ("\n..." if len(snippet_lines) > 12 else ""),
            })
            if len(results) >= wanted:
                break

        has_more = len(results) > offset + limit
        results = results[offset:offset + limit]
        if explain:
            for result in results:
                row = self.conn.execute(
                    'SELECT rowid, raw_text FROM chunk_metadata WHERE file_path=? AND start_line=? AND end_line=?',
                    (result['file_path'], result['start_line'], result['end_line'])).fetchone()
                document = set(_query_terms((row['raw_text'] if row else result['snippet']) + ' ' +
                                           result['file_path'] + ' ' + (result['symbol'] or '')))
                matched = [t for t in _query_terms(query) if t in document]
                expansions = []
                for term in extra:
                    matches = [t for t in _query_terms(term) if t in document]
                    if not matches:
                        continue
                    sources = self.expander.sources(query, term)
                    if term in mined:
                        grams = sorted({run[i:i + 2] for run in re.findall(r'[一-鿿]+', query) for i in range(len(run) - 1)})
                        evidence = [dict(row) for row in self.conn.execute(
                            f"SELECT gram, n AS supporting_symbols FROM mined_terms WHERE word=? "
                            f"AND gram IN ({','.join('?' * len(grams))}) ORDER BY n DESC, gram", (term, *grams))]
                        sources.append({'source': 'mined_comments', 'word': term, 'evidence': evidence})
                    expansions.append({'term': term, 'matched_terms': matches, 'sources': sources})
                detail: dict = {'matched_query_terms': matched, 'matched_expansions': expansions,
                          'layers': layer_scores.get(row['rowid'], {}) if row else {},
                          'note': 'Ranks and matched terms explain retrieval; scores are not probabilities.'}
                if result.get('data_access'):
                    detail['routing'] = result['data_access']
                    detail['note'] += ' Data-access routing takes precedence over the hybrid layer scores.'
                result['explanation'] = detail
        hints: list[str] = []
        suggestions = []
        if not results:
            symbol_names = [r[0] for r in self.conn.execute("SELECT DISTINCT symbol_name FROM symbols")]
            suggestions = difflib.get_close_matches(query, symbol_names, n=5, cutoff=0.45)
            hints = ["Run doctor --path <file> to explain missing files.",
                     "Try an identifier or fewer words; remove file/language filters to widen the search."]
        return {"query": query, "results": results,
                "pagination": {"offset": offset, "limit": limit, "has_more": has_more,
                               "next_offset": offset + limit if has_more else None,
                               "next_cursor": encode_cursor(offset + limit, revision, key) if has_more else None,
                               "index_revision": revision},
                "filters": {"file_pattern": file_pattern, "language": language},
                "suggestions": suggestions, "hints": hints,
                "related_files": self._related_files(results[:3]) if related else []}

    # ------------------------------------------------------------------ data access routing
    def _mined_expansion(self, query: str) -> list[str]:
        """Identifier words that this codebase's comments pair with the query's Chinese bigrams (core.mine_terms)."""
        grams = sorted({run[i:i + 2] for run in re.findall(r"[一-鿿]+", query) for i in range(len(run) - 1)})
        if not grams:
            return []
        rows = self.conn.execute(
            f"SELECT word FROM mined_terms WHERE gram IN ({','.join('?' * len(grams))}) ORDER BY n DESC, gram, word", grams)
        return list(dict.fromkeys(r[0] for r in rows))

    def _ordinary_hit(self, rid: int, fused: dict, via: dict) -> dict | None:
        row = self.conn.execute(
            "SELECT c.file_path, c.start_line, c.end_line, c.raw_text, s.symbol_type, s.qualified_name, s.signature "
            "FROM chunk_metadata c LEFT JOIN symbols s ON s.id = c.symbol_id WHERE c.rowid=?", (rid,)).fetchone()
        if row is None:
            return None
        lines = textwrap.dedent(row["raw_text"].rstrip()).split("\n")
        return {"file_path": row["file_path"], "start_line": row["start_line"], "end_line": row["end_line"],
                "symbol": row["qualified_name"], "symbol_type": row["symbol_type"], "signature": row["signature"],
                "score": round(fused[rid], 5), "matched_by": via[rid],
                "snippet": "\n".join(lines[:12]) + ("\n..." if len(lines) > 12 else "")}

    @staticmethod
    def _whole_identifiers(query: str) -> list[str]:
        """Identifier-looking tokens (underscore, digit or camelCase inside) of at least 5 characters."""
        out = []
        for ident in re.findall(r"[A-Za-z_][\w$]*", query):
            if len(ident) >= 5 and (re.search(r"[_\d]", ident[1:]) or
                                    (re.search(r"[a-z]", ident) and re.search(r"[A-Z]", ident[1:]))):
                out.append(ident)
        return out

    _OBJECT_HINT = re.compile(r"\b(table|tables|procedure|procedures|proc|stored|view|views|sp)\b|資料表|預存程序|存储过程|数据表", re.I)

    def _route_data_access(self, query: str) -> tuple[list[dict], bool]:
        """Queries such as "what writes to MES_Lot" name a known table / procedure and an intent. A ranked chunk
        list cannot enumerate every writer, so those queries get the accessors from the data access index first."""
        intents = dataflow.detect_intents(query)
        bare = False
        if not intents:
            if len(query.split()) > 3:
                return [], False
            intents, bare = {"touch"}, True
        keys = dataflow.known_object_keys(self.conn)
        hinted = bool(self._OBJECT_HINT.search(query))
        found: list[str] = []
        for ident in re.findall(r"[A-Za-z_][\w$]*(?:\.[\w$]+){0,3}", query):
            key = dataflow.object_key(ident)
            looks_like_object = bool(re.search(r"[_.\d]", ident) or (re.search(r"[a-z]", ident) and re.search(r"[A-Z]", ident[1:])))
            if len(key) >= 3 and key in keys and (looks_like_object or hinted) and key not in found:
                found.append(key)
        out: list[dict] = []
        seen = set()
        for key in found[:2]:
            for acc in dataflow.accessors_for_search(self.conn, key, intents):
                sig = (acc["source_id"], acc["file_path"], acc["line"])
                if sig not in seen:
                    seen.add(sig)
                    out.append(acc)
        if bare:  # leave room for the ordinary name matches (the class, the repository, the file)
            out = out[:3]
        return out, bare

    def _chunk_result(self, acc: dict) -> dict | None:
        conn = self.conn
        if acc["source_id"]:
            row = conn.execute(
                "SELECT c.file_path, c.start_line, c.end_line, c.raw_text, s.symbol_type, s.qualified_name, s.signature "
                "FROM chunk_metadata c LEFT JOIN symbols s ON s.id = c.symbol_id WHERE c.symbol_id=? "
                "ORDER BY (c.start_line<=? AND c.end_line>=?) DESC, c.start_line LIMIT 1",
                (acc["source_id"], acc["line"], acc["line"])).fetchone()
        else:
            row = conn.execute(
                "SELECT c.file_path, c.start_line, c.end_line, c.raw_text, NULL AS symbol_type, NULL AS qualified_name, "
                "NULL AS signature FROM chunk_metadata c WHERE c.file_path=? AND c.start_line<=? AND c.end_line>=? LIMIT 1",
                (acc["file_path"], acc["line"], acc["line"])).fetchone()
        if row is None:
            return None
        snippet_lines = textwrap.dedent(row["raw_text"].rstrip()).split("\n")
        return {
            "file_path": row["file_path"], "start_line": row["start_line"], "end_line": row["end_line"],
            "symbol": row["qualified_name"], "symbol_type": row["symbol_type"], "signature": row["signature"],
            "score": 1.0, "matched_by": ["data_access"],
            "data_access": {k: acc[k] for k in ("target", "access", "op", "via", "confidence", "line", "depth", "via_chain", "evidence")},
            "snippet": "\n".join(snippet_lines[:12]) + ("\n..." if len(snippet_lines) > 12 else ""),
        }

    @_consistent_read(refresh=True)
    def data_access(self, object_name: str, limit: int | None = None, offset: int = 0,
                    confidence: str | None = None) -> dict:
        if offset < 0 or (limit is not None and limit < 1):
            raise ValueError("limit must be positive and offset nonnegative")
        if confidence is not None and confidence not in ('confirmed', 'probable', 'uncertain'):
            raise ValueError("confidence must be confirmed, probable or uncertain")
        res = dataflow.data_access(self.conn, object_name)
        if not res['found']:
            return res
        sections = {}
        groups = [(res, k, k) for k in ('readers', 'writers', 'executed_by', 'mapped_by', 'possible',
                                        'programs', 'no_known_caller', 'schema_changes')]
        groups += [(res['touches'], k, 'touches.' + k) for k in res.get('touches', {})]
        for container, key, label in groups:
            entries = container[key]
            if confidence:
                entries = [e for e in entries if e.get('confidence') == confidence]
            total = len(entries)
            page = entries[offset:None if limit is None else offset + limit]
            container[key] = page
            sections[label] = {'total': total, 'returned': len(page),
                               'next_offset': offset + len(page) if offset + len(page) < total else None}
        res['filters'] = {'confidence': confidence}
        if limit is not None or offset:
            res['pagination'] = {'offset': offset, 'limit': limit, 'confidence': confidence, 'sections': sections}
        return res

    def doctor(self, path: str | None = None) -> dict:
        report = ScanReport()
        binary = unreadable = 0
        for sf in iter_files(self.root, report=report):
            try:
                with open(sf.abs_path, 'rb') as f:
                    if b'\x00' in f.read(8192):
                        binary += 1
                        report.record(sf.rel_path, 'binary')
            except OSError:
                unreadable += 1
                report.record(sf.rel_path, 'unreadable')
        eligible = report.counts.get('eligible', 0) - binary - unreadable
        return {'root': self.root, 'eligible_files': eligible, 'counts': report.counts,
                'examples': report.examples, 'path': explain_path(self.root, path) if path else None,
                'index': self.index_status(),
                'hints': (["No indexable files: check --root, ignore rules, supported extensions and file sizes."]
                          if not eligible else []),
                'note': 'Excluded directory counts describe directories, not the number of files hidden within them.'}

    def _related_files(self, seeds: list[dict]) -> list[dict]:
        """Graph traversal: dependencies of / on the top seed files."""
        seed_files = {s["file_path"] for s in seeds}
        related: dict[str, set[str]] = defaultdict(set)
        for f in seed_files:
            for r in self.conn.execute(
                "SELECT target_path AS p, dependency_type AS t FROM dependencies WHERE source_path=?", (f,)
            ):
                related[r["p"]].add("uses:" + r["t"])
            for r in self.conn.execute(
                "SELECT source_path AS p, dependency_type AS t FROM dependencies WHERE target_path=?", (f,)
            ):
                related[r["p"]].add("used-by:" + r["t"])
        out = []
        for p, rels in related.items():
            if p in seed_files:
                continue
            types = [r["symbol_name"] for r in self.conn.execute(
                f"SELECT symbol_name FROM symbols WHERE file_path=? AND symbol_type IN ({','.join('?' * len(_TYPE_KINDS))}) LIMIT 4",
                (p, *_TYPE_KINDS))]
            out.append({"file_path": p, "relations": sorted(rels), "types": types})
        out.sort(key=lambda d: (-len(d["relations"]), d["file_path"]))
        return out[:8]

    # ------------------------------------------------------------------ impact of a change
    def impact(self, base: str | None = None) -> dict:
        from .impact import analyze_impact
        return analyze_impact(self, base)

    # ------------------------------------------------------------------ Tool 4
    @_consistent_read()
    def index_status(self) -> dict:
        """Report how trustworthy the index is. Never refreshes the index itself."""
        conn = self.conn
        meta = lambda k: dbmod.get_meta(conn, k)  # noqa: E731
        indexed = {r["file_path"]: r["last_modified"] for r in conn.execute("SELECT file_path, last_modified FROM files")}

        on_disk = {sf.rel_path: sf.mtime_ns for sf in iter_files(self.root)}
        new = sorted(set(on_disk) - set(indexed))
        gone = sorted(set(indexed) - set(on_disk))
        touched = sorted(p for p in set(on_disk) & set(indexed) if on_disk[p] != indexed[p])
        failed = [dict(file_path=r["file_path"], error=r["error"], failed_at=r["failed_at"])
                  for r in conn.execute("SELECT * FROM index_failures ORDER BY file_path")]
        failed_paths = {f["file_path"] for f in failed}
        new = [p for p in new if p not in failed_paths]  # already reported as failures

        git_now = git_info(self.root)
        git_then = {"commit": meta("git_commit"), "branch": meta("git_branch"),
                    "dirty_files": int(meta("git_dirty_files") or 0)} if meta("git_commit") else None
        head_moved = bool(git_now and git_then and git_now["commit"] != git_then["commit"])

        problems = self._consistency_problems()
        if meta('indexing') == '1':
            problems.append('Previous indexing run was interrupted; run index to recover.')
        last = meta("last_indexed_at")

        outdated_format = bool(indexed) and meta("index_version") != dbmod.INDEX_VERSION
        if not indexed and not failed:
            state = "not_indexed"
        elif problems or failed:
            state = "degraded"
        elif new or gone or touched or head_moved or outdated_format:
            state = "stale"
        else:
            state = "fresh"

        def cap(items):
            return {"count": len(items), "examples": items[:10]}

        return {
            "state": state,
            "server": _server_info(),
            "root": self.root,
            "embedder": meta("embedder"),
            "index_version": meta("index_version"),
            "index_revision": meta("index_revision"),
            "index_format_outdated": outdated_format,
            "last_indexed_at": int(last) if last else None,
            "indexed_files": len(indexed),
            "symbols": conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "chunks": conn.execute("SELECT COUNT(*) FROM chunk_metadata").fetchone()[0],
            "git": {"indexed": git_then, "current": git_now, "head_moved": head_moved},
            "pending_changes": {
                "new_files": cap(new), "deleted_files": cap(gone),
                "possibly_modified_files": cap(touched),
                "note": "modified is judged by mtime only; a touched file with identical content is re-checked by md5 on the next update",
            },
            "failed_files": failed,
            "consistency": {"ok": not problems, "problems": problems},
        }

    def _consistency_problems(self) -> list[str]:
        c = self.conn
        one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        checks = [
            ("chunk_metadata rows without a vector",
             "SELECT COUNT(*) FROM chunk_metadata WHERE rowid NOT IN (SELECT rowid FROM vss_chunks)"),
            ("vectors without chunk_metadata",
             "SELECT COUNT(*) FROM vss_chunks WHERE rowid NOT IN (SELECT rowid FROM chunk_metadata)"),
            ("chunk_metadata rows without a full-text entry",
             "SELECT COUNT(*) FROM chunk_metadata WHERE rowid NOT IN (SELECT rowid FROM chunks_fts)"),
            ("full-text entries without chunk_metadata",
             "SELECT COUNT(*) FROM chunks_fts WHERE rowid NOT IN (SELECT rowid FROM chunk_metadata)"),
            ("chunks of files that are not indexed",
             "SELECT COUNT(*) FROM chunk_metadata WHERE file_path NOT IN (SELECT file_path FROM files)"),
            ("symbols of files that are not indexed",
             "SELECT COUNT(*) FROM symbols WHERE file_path NOT IN (SELECT file_path FROM files)"),
            ("references of files that are not indexed",
             "SELECT COUNT(*) FROM raw_refs WHERE file_path NOT IN (SELECT file_path FROM files)"),
            ("dependencies pointing at files that are not indexed",
             "SELECT COUNT(*) FROM dependencies WHERE source_path NOT IN (SELECT file_path FROM files)"
             " OR target_path NOT IN (SELECT file_path FROM files)"),
            ("data access rows of files that are not indexed",
             "SELECT COUNT(*) FROM data_access WHERE file_path NOT IN (SELECT file_path FROM files)"),
            ("call sites of files that are not indexed",
             "SELECT COUNT(*) FROM call_sites WHERE file_path NOT IN (SELECT file_path FROM files)"),
            ("calls whose caller or callee symbol is missing",
             "SELECT COUNT(*) FROM calls WHERE caller_id NOT IN (SELECT id FROM symbols)"
             " OR callee_id NOT IN (SELECT id FROM symbols)"),
        ]
        return [f"{n} {label}" for label, sql in checks if (n := one(sql))]

    # ------------------------------------------------------------------ misc
    @_consistent_read()
    def stats(self) -> dict:
        c = self.conn
        langs = {r[0]: r[1] for r in c.execute("SELECT language, COUNT(*) FROM files GROUP BY language")}
        return {
            "root": self.root, "db": self.indexer.db_path, "embedder": self.embedder.name,
            "files": c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "symbols": c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "chunks": c.execute("SELECT COUNT(*) FROM chunk_metadata").fetchone()[0],
            "dependencies": c.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0],
            "calls": c.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
            "data_access": c.execute("SELECT COUNT(*) FROM data_access").fetchone()[0],
            "languages": langs,
        }


# ---------------------------------------------------------------------- text rendering
