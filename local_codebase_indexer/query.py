"""Hybrid query pipeline and the three tools from index.md section 4."""

from __future__ import annotations

import os
import fnmatch
import difflib
import re
import sqlite3
import textwrap
import time
from functools import wraps
from collections import defaultdict

import numpy as np

from . import __version__, dataflow
from . import db as dbmod
from .callgraph import CONF_ORDER
from .core import Indexer, cjk_bigrams, git_info
from .scanner import ScanReport, explain_path, extra_roots, iter_files
from .embedder import split_identifier
from .synonyms import Expander
from .pagination import decode_cursor, encode_cursor, search_key

RRF_K = 60
RECHECK_SECONDS = 15  # a long-lived server re-checks the index at most this often
_STARTED = time.time()  # when this process loaded the package
# the symbol-name layer is a weak signal (substring match), so it counts for less
LAYER_WEIGHTS = {"keyword": 1.0, "vector": 1.0, "symbol": 0.5}
MAX_CONTEXT_CHARS = 24000
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


def _inside(path: str, base: str) -> bool:
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:  # different drives on Windows
        return False


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


def _consistent_read(*, refresh: bool = False):
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            if refresh:
                self._ensure()
            with dbmod.read_snapshot(self.conn, self.indexer.db_path):
                return method(self, *args, **kwargs)
        return wrapped
    return decorate


class CodebaseTools:
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

        layer_scores = {}
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
                detail = {'matched_query_terms': matched, 'matched_expansions': expansions,
                          'layers': layer_scores.get(row['rowid'], {}) if row else {},
                          'note': 'Ranks and matched terms explain retrieval; scores are not probabilities.'}
                if result.get('data_access'):
                    detail['routing'] = result['data_access']
                    detail['note'] += ' Data-access routing takes precedence over the hybrid layer scores.'
                result['explanation'] = detail
        hints = []
        suggestions = []
        if not results:
            names = [r[0] for r in self.conn.execute("SELECT DISTINCT symbol_name FROM symbols")]
            suggestions = difflib.get_close_matches(query, names, n=5, cutoff=0.45)
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
                                        'programs', 'no_known_caller')]
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

    # ------------------------------------------------------------------ Tool 2
    @_consistent_read(refresh=True)
    def symbol_relations(self, symbol_name: str, relation_direction: str = "both") -> dict:
        if relation_direction not in ("upstream", "downstream", "both"):
            raise ValueError("relation_direction must be 'upstream', 'downstream' or 'both'")
        conn = self.conn
        # "Type.Method" or a full qualified name narrows overloaded names such as InsertAsync to one class
        if "." in symbol_name:
            defs = conn.execute(
                "SELECT * FROM symbols WHERE symbol_name=? AND symbol_type != 'namespace' "
                "AND (qualified_name=? OR qualified_name LIKE ?) ORDER BY file_path, start_line LIMIT 8",
                (symbol_name.rsplit(".", 1)[1], symbol_name, "%." + symbol_name),
            ).fetchall()
        else:
            defs = conn.execute(
                "SELECT * FROM symbols WHERE symbol_name=? AND symbol_type != 'namespace' ORDER BY file_path, start_line LIMIT 8",
                (symbol_name,),
            ).fetchall()
        if not defs:
            like = conn.execute(
                "SELECT DISTINCT symbol_name FROM symbols WHERE symbol_name LIKE ? LIMIT 10", (f"%{symbol_name}%",)
            ).fetchall()
            return {"symbol": symbol_name, "definitions": [], "suggestions": [r[0] for r in like]}

        type_defs: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in conn.execute(
            f"SELECT * FROM symbols WHERE symbol_type IN ({','.join('?' * len(_TYPE_KINDS))})", _TYPE_KINDS
        ):
            type_defs[r["symbol_name"]].append(r)

        out_defs = []
        for d in defs:
            entry = {
                "id": d["id"], "file_path": d["file_path"], "type": d["symbol_type"],
                "qualified_name": d["qualified_name"], "start_line": d["start_line"],
                "end_line": d["end_line"], "signature": d["signature"],
            }
            if relation_direction in ("upstream", "both"):
                entry["upstream"] = self._upstream(d)
            if relation_direction in ("downstream", "both"):
                entry["downstream"] = self._downstream(d, type_defs)
            out_defs.append(entry)
        return {
            "symbol": symbol_name, "definitions": out_defs,
            "note": ("C# method calls are resolved by receiver type (confirmed / probable / candidate); type references and "
                 "fields remain identifier-name matches; inheritance edges come from the AST"),
        }

    def _type_upstream(self, d: sqlite3.Row) -> dict:
        name = d["symbol_name"]
        conn = self.conn
        inheritors = [
            {"symbol": r["qualified_name"], "type": r["symbol_type"], "file_path": r["file_path"], "line": r["start_line"]}
            for r in conn.execute(
                "SELECT s.* FROM raw_refs r JOIN symbols s ON s.id = r.owner WHERE r.kind='base' AND r.name=? "
                "ORDER BY s.file_path, s.start_line LIMIT 40", (name,))
        ]
        refs = []
        for r in conn.execute(
            "SELECT r.file_path, r.line, r.owner, s.qualified_name AS user_symbol FROM raw_refs r "
            "LEFT JOIN symbols s ON s.id = r.owner WHERE r.kind='ident' AND r.name=? "
            "ORDER BY r.file_path, r.line LIMIT 200", (name,)):
            if r["file_path"] == d["file_path"] and d["start_line"] <= r["line"] <= d["end_line"]:
                continue  # inside the definition itself
            refs.append({"file_path": r["file_path"], "line": r["line"], "in_symbol": r["user_symbol"]})
        up = {"inheritors_or_implementers": inheritors, "referenced_by": refs[:60], "referenced_by_total": len(refs)}
        if d["symbol_type"] in _TYPE_KINDS:
            up["instantiated_by"] = self._callers(
                "c.callee_id IN (SELECT id FROM symbols WHERE id=? OR (parent_id=? AND symbol_type='constructor'))",
                (d["id"], d["id"]))[0]
        return up

    _METHOD_KINDS = ("method", "constructor", "operator")
    _SQL_KINDS = ("procedure", "view", "function", "table")

    def _callers(self, where: str, params: tuple, limit: int = 200) -> tuple[list[dict], int]:
        order = "CASE c.confidence WHEN 'confirmed' THEN 0 WHEN 'probable' THEN 1 ELSE 2 END, c.file_path, c.line"
        base = f"FROM calls c JOIN symbols s ON s.id = c.caller_id WHERE {where}"
        total = self.conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT s.qualified_name, s.symbol_type, c.file_path, c.line, c.confidence, c.reason {base} "
            f"ORDER BY {order} LIMIT ?", (*params, limit)).fetchall()
        return ([{"symbol": r["qualified_name"], "type": r["symbol_type"], "file_path": r["file_path"],
                  "line": r["line"], "confidence": r["confidence"], "reason": r["reason"]} for r in rows], total)

    def _upstream(self, d: sqlite3.Row) -> dict:
        kind = d["symbol_type"]
        if kind in self._METHOD_KINDS:
            callers, total = self._callers("c.callee_id=?", (d["id"],))
            impls = []
            parent = self.conn.execute("SELECT * FROM symbols WHERE id=?", (d["parent_id"],)).fetchone() if d["parent_id"] else None
            if parent is not None and parent["symbol_type"] == "interface":
                impls = [{"symbol": r["qualified_name"], "file_path": r["file_path"], "line": r["start_line"]}
                         for r in self.conn.execute(
                    "SELECT m.* FROM raw_refs r JOIN symbols m ON m.parent_id = r.owner "
                    "WHERE r.kind='base' AND r.name=? AND m.symbol_name=? AND m.symbol_type='method' "
                    "ORDER BY m.file_path, m.start_line", (parent["symbol_name"], d["symbol_name"]))]
            return {"callers": callers, "callers_total": total, "implementations": impls,
                    "inheritors_or_implementers": [], "referenced_by": [], "referenced_by_total": 0}
        if kind in self._SQL_KINDS:
            res = dataflow.data_access(self.conn, d["symbol_name"])
            callers = []
            for label, key in (("writes", "writers"), ("reads", "readers"), ("executes", "executed_by"), ("maps", "mapped_by")):
                for e in res.get(key, []):
                    callers.append({"symbol": e["source"], "type": e["source_type"], "file_path": e["file_path"],
                                    "line": e["line"], "confidence": e["confidence"],
                                    "reason": f'{label} ({e["op"]}, {e["via"]})' + (
                                        f' via {" > ".join(e["via_chain"])}' if e["via_chain"] else "")})
            return {"callers": callers[:200], "callers_total": len(callers), "implementations": [],
                    "inheritors_or_implementers": [], "referenced_by": [], "referenced_by_total": 0}
        return self._type_upstream(d)

    def _downstream(self, d: sqlite3.Row, type_defs: dict) -> dict:
        kind = d["symbol_type"]
        conn = self.conn
        if kind in self._METHOD_KINDS:
            order = "CASE c.confidence WHEN 'confirmed' THEN 0 WHEN 'probable' THEN 1 ELSE 2 END"
            calls = [{"symbol": r["qualified_name"], "type": r["symbol_type"], "file_path": r["file_path"],
                      "line": r["line"], "callee_line": r["start_line"], "confidence": r["confidence"],
                      "reason": r["reason"]}
                     for r in conn.execute(
                "SELECT s.qualified_name, s.symbol_type, s.file_path, s.start_line, c.line, c.confidence, c.reason "
                f"FROM calls c JOIN symbols s ON s.id = c.callee_id WHERE c.caller_id=? ORDER BY c.line, {order}",
                (d["id"],))]
            down = self._type_downstream(d, type_defs)
            down["calls"] = calls
            down["data_access"] = self._own_data_access(d["id"])
            return down
        if kind in ("procedure", "view", "function"):
            res = dataflow.data_access(conn, d["symbol_name"])
            t = res.get("touches", {})
            down = {"base_types": [], "uses_types": [], "calls": [], "data_access": []}
            for key in ("writes", "reads", "executes", "dynamic_sql"):
                for e in t.get(key, []):
                    down["data_access"].append({
                        "target": e["target"], "access": e["access"], "op": e["op"], "via": e["via"],
                        "confidence": e["confidence"], "line": e["line"], "file_path": e["file_path"],
                        "via_chain": e["via_chain"]})
            return down
        return self._type_downstream(d, type_defs)

    def _own_data_access(self, symbol_id: str) -> list[dict]:
        return [{"target": r["target_name"], "access": r["access"], "op": r["op"], "via": r["via"],
                 "confidence": r["confidence"], "line": r["line"], "file_path": r["file_path"], "via_chain": []}
                for r in self.conn.execute(
            "SELECT * FROM data_access WHERE source_id=? ORDER BY line", (symbol_id,))]

    def _type_downstream(self, d: sqlite3.Row, type_defs: dict) -> dict:
        conn = self.conn
        bases = []
        for r in conn.execute("SELECT name FROM raw_refs WHERE kind='base' AND owner=?", (d["id"],)):
            targets = [{"file_path": t["file_path"], "line": t["start_line"], "type": t["symbol_type"]}
                       for t in type_defs.get(r["name"], [])]
            bases.append({"name": r["name"], "defined_in": targets})
        used: dict[str, dict] = {}
        for r in conn.execute(
            "SELECT name, MIN(line) AS line FROM raw_refs WHERE kind='ident' AND file_path=? "
            "AND line BETWEEN ? AND ? GROUP BY name", (d["file_path"], d["start_line"], d["end_line"])):
            nm = r["name"]
            if nm == d["symbol_name"] or nm not in type_defs:
                continue
            used[nm] = {
                "name": nm, "first_line": r["line"],
                "defined_in": [{"file_path": t["file_path"], "line": t["start_line"], "type": t["symbol_type"]}
                               for t in type_defs[nm][:5]],
            }
        return {"base_types": bases, "uses_types": sorted(used.values(), key=lambda u: u["first_line"])[:60]}

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

        from .core import decode_bytes  # local import to avoid cycles at module load

        focuses = sorted({int(line) for line in focus_lines if int(line) >= 1})
        if not focuses:
            raise ValueError('focus_lines must contain positive line numbers')
        pad_ranges = [(max(1, line - pad), line + pad) for line in focuses]
        wanted: dict[int, str] = {}
        total = 0
        with open(abs_path, 'rb') as stream:
            for total, raw in enumerate(stream, start=1):
                if any(a <= total <= b for a, b in pad_ranges):
                    wanted[total] = decode_bytes(raw.rstrip(b'\r\n'))
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
        merged = []
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

def render_search(res: dict) -> str:
    out = [f'Search: "{res["query"]}"  ({len(res["results"])} results)']
    if not res["results"]:
        out.append("No matches.")
        if res.get('suggestions'):
            out.append("Similar symbols: " + ", ".join(res['suggestions']))
        out.extend(res.get('hints', []))
    for i, r in enumerate(res["results"], 1):
        head = f'[{i}] {r["file_path"]}:{r["start_line"]}-{r["end_line"]}'
        if r["symbol"]:
            head += f'  {r["symbol_type"]} {r["symbol"]}'
        out.append(f'{head}  (score {r["score"]}, via {"+".join(r["matched_by"])})')
        if r["signature"]:
            out.append(f'    sig: {r["signature"]}')
        da = r.get("data_access")
        if da:
            chain = f' via {" > ".join(da["via_chain"])}' if da["via_chain"] else ""
            out.append(f'    data access: {da["access"]}/{da["op"]} {da["target"]} '
                       f'[{da["confidence"]}, {da["via"]}] line {da["line"]}{chain}')
        if r.get('explanation'):
            why = r['explanation']
            out.append('    matched query terms: ' + (', '.join(why['matched_query_terms']) or '(none)'))
            for layer, score in why['layers'].items():
                out.append(f"    {layer}: rank {score['rank']}, RRF contribution {score['rrf_contribution']:.8f}")
            for item in why['matched_expansions']:
                origins = ', '.join(source['source'] for source in item['sources'])
                out.append(f"    expansion: {item['term']} -> {', '.join(item['matched_terms'])} ({origins})")
            if why.get('routing') and why['routing'].get('evidence'):
                out.append('    evidence: ' + why['routing']['evidence'][:240])
        out.extend("    " + ln for ln in r["snippet"].split("\n"))
    page = res.get('pagination', {})
    if page.get('has_more'):
        out.append(f"More results: --cursor {page['next_cursor']}" if page.get('next_cursor') else
                   f"More results: --offset {page['next_offset']}")
    if res["related_files"]:
        out.append("Related files (dependency graph):")
        for rf in res["related_files"]:
            t = f' [{", ".join(rf["types"])}]' if rf["types"] else ""
            out.append(f'  {rf["file_path"]}{t}  <- {", ".join(rf["relations"])}')
    return "\n".join(out)


def render_relations(res: dict) -> str:
    if not res["definitions"]:
        msg = f'Symbol "{res["symbol"]}" not found.'
        if res.get("suggestions"):
            msg += " Similar: " + ", ".join(res["suggestions"])
        return msg
    out = []
    for d in res["definitions"]:
        out.append(f'{d["type"]} {d["qualified_name"]}  {d["file_path"]}:{d["start_line"]}-{d["end_line"]}')
        if d.get("signature"):
            out.append(f'  sig: {d["signature"]}')
        up = d.get("upstream")
        if up is not None:
            out.append("  UPSTREAM (who depends on it)")
            for x in up.get("callers", [])[:30]:
                out.append(f'    [{x["confidence"]}] {x["type"]} {x["symbol"]}  {x["file_path"]}:{x["line"]}  ({x["reason"]})')
            if up.get("callers_total", 0) > 30:
                out.append(f'    ... {up["callers_total"] - 30} more')
            for x in up.get("implementations", []):
                out.append(f'    implemented by {x["symbol"]}  {x["file_path"]}:{x["line"]}')
            for x in up.get("instantiated_by", [])[:15]:
                out.append(f'    [{x["confidence"]}] instantiated in {x["symbol"]}  {x["file_path"]}:{x["line"]}')
            for x in up["inheritors_or_implementers"]:
                out.append(f'    inherited/implemented by {x["type"]} {x["symbol"]}  {x["file_path"]}:{x["line"]}')
            for x in up["referenced_by"][:25]:
                out.append(f'    referenced at {x["file_path"]}:{x["line"]}' + (f' in {x["in_symbol"]}' if x["in_symbol"] else ""))
            if up["referenced_by_total"] > 25:
                out.append(f'    ... {up["referenced_by_total"] - 25} more references')
        down = d.get("downstream")
        if down is not None:
            out.append("  DOWNSTREAM (what it depends on)")
            for x in down.get("calls", [])[:40]:
                out.append(f'    [{x["confidence"]}] calls {x["type"]} {x["symbol"]}  {x["file_path"]}:{x["callee_line"]}'
                           f'  (line {x["line"]}; {x["reason"]})')
            for x in down.get("data_access", [])[:40]:
                chain = f' via {" > ".join(x["via_chain"])}' if x.get("via_chain") else ""
                out.append(f'    [{x["confidence"]}] {x["access"]}/{x["op"]} {x["target"]}  (line {x["line"]}; {x["via"]}){chain}')
            for b in down["base_types"]:
                where = ", ".join(f'{t["file_path"]}:{t["line"]}' for t in b["defined_in"]) or "external/unresolved"
                out.append(f'    base/interface {b["name"]}  -> {where}')
            for u in down["uses_types"][:30]:
                where = ", ".join(f'{t["file_path"]}:{t["line"]}' for t in u["defined_in"])
                out.append(f'    uses {u["name"]} (line {u["first_line"]})  -> {where}')
    if res.get("note"):
        out.append(f'note: {res["note"]}')
    return "\n".join(out)


def _da_line(e: dict) -> str:
    chain = f' via {" > ".join(e["via_chain"])}' if e["via_chain"] else ""
    return (f'    [{e["confidence"]}] {e["source_type"]} {e["source"]}  {e["file_path"]}:{e["line"]}  '
            f'{e["op"]} ({e["via"]}){chain}')


def _more(n: int, shown: int, full: bool) -> str:
    return f"    ... {n - shown} more" + ("" if full else " (detail=true / --full lists all)")


def render_data_access(res: dict, full: bool = False) -> str:
    """Five entries per section by default; full and paginated output never silently truncate."""
    if not res["found"]:
        msg = f'No table or procedure named "{res["object"]}" found.'
        if res.get("suggestions"):
            msg += " Similar: " + ", ".join(res["suggestions"])
        return msg
    out = [f'{res["kind"]} {res["object"]}']
    for d in res["definitions"]:
        out.append(f'  defined in {d["file_path"]}:{d["line"]}')
    sections = [("WRITTEN BY", "writers"), ("READ BY", "readers"), ("EXECUTED BY", "executed_by"),
                ("MAPPED BY (EF)", "mapped_by")]
    cap = max([len(res.get(k, [])) for k in ('writers', 'readers', 'executed_by', 'mapped_by',
                                                      'possible', 'no_known_caller')]
                  + [len(v) for v in res.get('touches', {}).values()] + [5]) if full or res.get("pagination") else 5
    for title, key in sections:
        items = res[key]
        if items:
            out.append(f"  {title} ({len(items)})")
            out.extend(_da_line(e) for e in items[:cap])
            if len(items) > cap:
                out.append(_more(len(items), cap, full))
    if res.get("programs"):
        out.append(f'  USED BY PROGRAMS ({len(res["programs"])})')
        for p in res["programs"]:
            out.append(f'    [{p["confidence"]}] {p["program"]}  {"/".join(p["access"])}  {p["file_path"]}  '
                       f'({", ".join(p["actions"] if full or res.get("pagination") else p["actions"][:6])}{", ... (use --full)" if len(p["actions"]) > 6 and not full and not res.get("pagination") else ""})')
            if p.get("pages"):
                out.append(f'        pages: {", ".join(p["pages"] if full or res.get("pagination") else p["pages"][:5])}{" ... (use --full)" if len(p["pages"]) > 5 and not full and not res.get("pagination") else ""}')
    roots = res.get("no_known_caller") or []
    if roots:
        out.append(f"  NO KNOWN CALLER - entry points (API, jobs, Main) or unresolved calls ({len(roots)})")
        out.extend(f'    {r["symbol"]}  {"/".join(r["access"])}  {r["file_path"]}:{r["line"]}' for r in roots[:cap])
        if len(roots) > cap:
            out.append(_more(len(roots), cap, full))
    t = res.get("touches")
    if t:
        for title, key in (("WRITES", "writes"), ("READS", "reads"), ("EXECUTES", "executes")):
            if t[key]:
                out.append(f"  {title} ({len(t[key])})")
                for e in t[key][:cap]:
                    chain = f' via {" > ".join(e["via_chain"])}' if e["via_chain"] else ""
                    out.append(f'    [{e["confidence"]}] {e["target"]}  {e["file_path"]}:{e["line"]}  {e["op"]}{chain}')
                if len(t[key]) > cap:
                    out.append(_more(len(t[key]), cap, full))
        if t["dynamic_sql"]:
            out.append(f'  DYNAMIC SQL inside ({len(t["dynamic_sql"])}) - targets cannot be determined')
            for e in t["dynamic_sql"][:(cap if full or res.get("pagination") else cap // 4 + 1)]:
                out.append(f'    {e["file_path"]}:{e["line"]}  {e["evidence"][:100]}')
            if not full and not res.get('pagination') and len(t['dynamic_sql']) > cap // 4 + 1:
                out.append(_more(len(t['dynamic_sql']), cap // 4 + 1, False))
    if res["possible"]:
        out.append(f'  POSSIBLE (uncertain: dynamic SQL, EF SaveChanges on tracked entities) ({len(res["possible"])})')
        out.extend(_da_line(e) for e in res["possible"][:(cap if full or res.get("pagination") else cap // 2 or 1)])
    if not res.get('pagination') and not res['possible'] and not any(res[k] for _t, k in sections) and not (t and any(t[k] for k in ("reads", "writes", "executes"))):
        out.append("  no recorded access")
    page = res.get('pagination')
    if page:
        for section, info in page['sections'].items():
            out.append(f"  {section}: {info['returned']} returned / {info['total']} total")
            if info['next_offset'] is not None:
                out.append(f"    more: --offset {info['next_offset']} --limit {page['limit']} (MCP: offset={info['next_offset']})")
    if res['possible'] and not full and not page and len(res['possible']) > (cap // 2 or 1):
        out.append(_more(len(res['possible']), cap // 2 or 1, False))
    for n in res.get("notes", []) if full else []:
        out.append(f"note: {n}")
    return "\n".join(out)


def render_impact(res: dict, full: bool = False) -> str:
    cap = max(len(res['changed']), len(res['tables']), len(res['no_known_caller']), 15) if full else 15
    out = [f'Impact of changes since {res["base"]}: {len(res["changed"])} changed symbols']
    for c in res["changed"][:cap]:
        lines = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in (c["lines"] if full else c["lines"][:4]))
        out.append(f'  {c["type"]} {c["symbol"]}  {c["file_path"]}:{lines} [{c.get("snapshot", "current")}]')
    if len(res["changed"]) > cap:
        out.append(_more(len(res["changed"]), cap, full))
    if res.get('removed_files'):
        out.append('  removed files: ' + ', '.join(res['removed_files']))
    if res.get('note'):
        out.append('  ' + res['note'])
    if res["unmapped_files"]:
        out.append(f'  changed outside any symbol: {", ".join(res["unmapped_files"] if full else res["unmapped_files"][:10])}'
                   + (" ... (use --full)" if len(res["unmapped_files"]) > 10 and not full else ""))
    if res["tables"]:
        tables: dict[str, set[str]] = defaultdict(set)
        names: dict[str, str] = {}
        for t in res["tables"]:  # SQL Server names are case-insensitive: SFC_MLot and SFC_Mlot are one table
            names.setdefault(t["target"].lower(), t["target"])
            tables[t["target"].lower()].add(t["access"])
        out.append(f"  TABLES / PROCEDURES ACCESSED BY CHANGED CODE ({len(tables)})")
        out.extend(f'    {names[k]}  {"/".join(sorted(acc))}' for k, acc in sorted(tables.items())[:cap])
    if res["programs"]:
        out.append(f'  AFFECTED PROGRAMS ({len(res["programs"])})')
        for p in res["programs"]:
            out.append(f'    [{p["confidence"]}] {p["program"]}  {p["file_path"]}  ({", ".join(p["actions"][:6])}'
                       f'{", ..." if len(p["actions"]) > 6 else ""})')
            if p.get("pages"):
                out.append(f'        pages: {", ".join(p["pages"] if full or res.get("pagination") else p["pages"][:5])}{" ... (use --full)" if len(p["pages"]) > 5 and not full and not res.get("pagination") else ""}')
    if res["no_known_caller"]:
        out.append(f'  NO KNOWN CALLER ({len(res["no_known_caller"])})')
        out.extend(f'    {r["symbol"]}  {r["file_path"]}:{r["line"]} [{r.get("snapshot", "current")}]' for r in res["no_known_caller"][:cap])
    if not res["changed"] and not res["unmapped_files"]:
        out.append("  no changes")
    return "\n".join(out)


def render_status(st: dict) -> str:
    import datetime as _dt

    when = _dt.datetime.fromtimestamp(st["last_indexed_at"]).strftime("%Y-%m-%d %H:%M:%S") if st["last_indexed_at"] else "never"
    srv = st.get("server") or {}
    out = [f'index state: {st["state"].upper()}   server: {srv.get("version", "?")}',
           f'last indexed: {when}   files: {st["indexed_files"]}   symbols: {st["symbols"]}   chunks: {st["chunks"]}',
           f'embedder: {st["embedder"]}   index version: {st["index_version"]}']
    if srv.get("code_changed_since_start"):
        out.append("warning: the indexer's code changed on disk after this process started; restart the MCP server "
                   "(new Claude Code session) to run it")
    g = st["git"]
    if g["current"] is None and g["indexed"] is None:
        out.append("git: not a git work tree")
    else:
        for label, info in (("indexed at", g["indexed"]), ("current   ", g["current"])):
            if info:
                out.append(f'git {label}: {info["commit"][:10]} ({info["branch"] or "?"}), {info["dirty_files"]} uncommitted paths')
        if g["head_moved"]:
            out.append("git: HEAD has moved since the last index update")
    pc = st["pending_changes"]
    for key, label in (("new_files", "new"), ("deleted_files", "deleted"), ("possibly_modified_files", "possibly modified")):
        if pc[key]["count"]:
            more = " ..." if pc[key]["count"] > len(pc[key]["examples"]) else ""
            out.append(f'{label} since last update ({pc[key]["count"]}): ' + ", ".join(pc[key]["examples"]) + more)
    if st["failed_files"]:
        out.append(f'failed files ({len(st["failed_files"])}), not searchable until fixed:')
        out.extend(f'  {f["file_path"]}: {f["error"]}' for f in st["failed_files"][:20])
    if st["consistency"]["ok"]:
        out.append("consistency: ok")
    else:
        out.append("consistency problems (run `index --force` to rebuild):")
        out.extend("  " + p for p in st["consistency"]["problems"])
    if st.get("index_format_outdated"):
        out.append("index was built by an older version and will be rebuilt on the next update")
    if st["state"] in ("stale", "not_indexed"):
        out.append("hint: run `local-codebase-indexer index` (search/relations/context also refresh automatically)")
    return "\n".join(out)


def render_context(res: dict) -> str:
    head = f'{res["file_path"]} ({res["total_lines"]} lines), ranges: ' + ", ".join(f"{a}-{b}" for a, b in res["ranges"])
    if res["enclosing_symbols"]:
        head += "\nenclosing: " + "; ".join(
            f'{s["type"]} {s["symbol"]} ({s["start_line"]}-{s["end_line"]})' for s in res["enclosing_symbols"])
    tail = "\n[truncated: output limit reached; request fewer or narrower ranges]" if res["truncated"] else ""
    return head + "\n" + res["context"] + tail


def render_doctor(res: dict) -> str:
    out = [f"Project: {res['root']}", f"Indexable text files: {res['eligible_files']}",
           f"Index state: {res['index']['state']}"]
    for reason, count in sorted(res['counts'].items()):
        out.append(f"  {reason}: {count}")
        out.extend('    ' + path for path in res['examples'][reason][:3])
    if res['path']:
        out.append(f"{res['path']['path']}: {res['path']['reason']}")
    out.extend(res['hints'])
    out.append(res['note'])
    return '\n'.join(out)
