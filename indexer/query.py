"""Hybrid query pipeline and the three tools from index.md section 4."""

from __future__ import annotations

import os
import re
import sqlite3
import textwrap
from collections import defaultdict

import numpy as np

from .core import Indexer
from .embedder import split_identifier

RRF_K = 60
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
        parts = split_identifier(ident) if re.match(r"[A-Za-z_]", ident) else [ident.lower()]
        if len(parts) > 1:
            terms.append(ident.lower())
        terms.extend(parts)
    out = []
    for t in terms:
        if t not in _STOPWORDS and t not in out and len(t) > 1:
            out.append(t)
    return out


def _has_word(term: str, text: str) -> bool:
    """True if ``term`` equals the text or one of its camelCase / snake_case / path words."""
    low = text.lower()
    if term == low:
        return True
    words: set[str] = set()
    for piece in re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", text):
        words.update(split_identifier(piece))
        words.add(piece.lower())
    return term in words


class CodebaseTools:
    """Facade exposing codebase_semantic_search / get_symbol_relations / get_optimized_context."""

    def __init__(self, root: str, auto_refresh: bool = True, embedder=None):
        self.indexer = Indexer(root, embedder=embedder)
        self.root = self.indexer.root
        self.conn: sqlite3.Connection = self.indexer.conn
        self.embedder = self.indexer.embedder
        self.auto_refresh = auto_refresh
        self._fresh = False

    def refresh(self, force: bool = False):
        stats = self.indexer.update(force=force)
        self._fresh = True
        return stats

    def _ensure(self) -> None:
        if self.auto_refresh and not self._fresh:
            self.refresh()

    # ------------------------------------------------------------------ Tool 1
    def semantic_search(self, query: str, limit: int = 5) -> dict:
        self._ensure()
        limit = max(1, min(int(limit or 5), 50))
        pool = max(limit * 6, 30)
        terms = _query_terms(query)
        ranked: dict[str, list[int]] = {"keyword": [], "symbol": [], "vector": []}
        ranks_override: dict[str, dict[int, int]] = {}

        # keyword layer: FTS5 / bm25 over chunk text
        if terms:
            match = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
            try:
                rows = self.conn.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                    (match, pool),
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
                    "WHERE lower(s.symbol_name) LIKE ? OR lower(s.qualified_name) LIKE ? LIMIT 500",
                    (like, like),
                ):
                    if _has_word(t, r["symbol_name"]) or _has_word(t, r["qualified_name"] or ""):
                        scores[r[0]] += 1
                for r in self.conn.execute(
                    "SELECT rowid, file_path FROM chunk_metadata WHERE lower(file_path) LIKE ? LIMIT 300", (like,)
                ):
                    if _has_word(t, r["file_path"]):
                        scores[r[0]] += 1
            ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:pool]
            ranked["symbol"] = [rid for rid, _ in ordered]
            # equal match counts must share a rank, otherwise arbitrary row order decides
            tie_rank: dict[int, int] = {}
            for pos, (rid, sc) in enumerate(ordered, start=1):
                tie_rank[rid] = next(i for i, (_, s2) in enumerate(ordered, start=1) if s2 == sc)
            ranks_override["symbol"] = tie_rank

        # vector layer: cosine similarity over local embeddings
        rows = self.conn.execute("SELECT rowid, chunk_embedding FROM vss_chunks").fetchall()
        if rows:
            ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
            mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
            qv = self.embedder.embed([query])[0]
            if float(np.linalg.norm(qv)) > 0:
                sims = mat @ qv
                top = np.argsort(-sims)[:pool]
                ranked["vector"] = [int(ids[i]) for i in top if sims[i] > 0]

        fused: dict[int, float] = defaultdict(float)
        via: dict[int, list[str]] = defaultdict(list)
        for name, lst in ranked.items():
            for rank, rid in enumerate(lst, start=1):
                rank = ranks_override.get(name, {}).get(rid, rank)
                fused[rid] += LAYER_WEIGHTS[name] / (RRF_K + rank)
                via[rid].append(name)
        top_ids = [rid for rid, _ in sorted(fused.items(), key=lambda kv: -kv[1])]

        results, seen_spans = [], set()
        for rid in top_ids:
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
            if len(results) >= limit:
                break

        return {"query": query, "results": results, "related_files": self._related_files(results[:3])}

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
    def symbol_relations(self, symbol_name: str, relation_direction: str = "both") -> dict:
        self._ensure()
        if relation_direction not in ("upstream", "downstream", "both"):
            raise ValueError("relation_direction must be 'upstream', 'downstream' or 'both'")
        conn = self.conn
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
            "note": "inheritance edges are AST based; usage edges are identifier-name matches (no type resolution)",
        }

    def _upstream(self, d: sqlite3.Row) -> dict:
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
        return {"inheritors_or_implementers": inheritors, "referenced_by": refs[:60], "referenced_by_total": len(refs)}

    def _downstream(self, d: sqlite3.Row, type_defs: dict) -> dict:
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
    def optimized_context(self, file_path: str, focus_lines: list[int], padding_lines: int = 15) -> dict:
        abs_path = os.path.abspath(os.path.join(self.root, file_path))
        if os.path.commonpath([abs_path, self.root]) != self.root:
            raise ValueError("file_path must stay inside the project root")
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(file_path)
        if not focus_lines:
            raise ValueError("focus_lines must contain at least one line number")
        pad = max(0, int(padding_lines if padding_lines is not None else 15))

        from .core import decode_bytes  # local import to avoid cycles at module load

        pad_ranges = sorted((max(1, int(l) - pad), int(l) + pad) for l in focus_lines if int(l) >= 1)
        wanted: dict[int, str] = {}
        total = 0
        # stream line by line: never loads a whole (possibly huge) file into memory
        with open(abs_path, "rb") as f:
            for total, raw in enumerate(f, start=1):
                if any(a <= total <= b for a, b in pad_ranges):
                    wanted[total] = decode_bytes(raw.rstrip(b"\r\n"))
        lines = wanted

        ranges = sorted((a, min(total, b)) for a, b in pad_ranges)
        merged: list[list[int]] = []
        for s, e in ranges:
            if merged and s <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])

        width = len(str(total))
        parts, size, truncated = [], 0, False
        for s, e in merged:
            block = "\n".join(f"{n:>{width}} | {lines.get(n, '')}" for n in range(s, e + 1))
            if size + len(block) > MAX_CONTEXT_CHARS:
                truncated = True
                break
            parts.append(block)
            size += len(block)

        symbols = [
            {"symbol": r["qualified_name"], "type": r["symbol_type"], "start_line": r["start_line"], "end_line": r["end_line"]}
            for r in self.conn.execute(
                "SELECT * FROM symbols WHERE file_path=? AND symbol_type != 'namespace' AND start_line<=? AND end_line>=? "
                "ORDER BY (end_line-start_line) LIMIT 5",
                (file_path.replace("\\", "/"), max(int(l) for l in focus_lines), min(int(l) for l in focus_lines)))
        ]
        return {
            "file_path": file_path, "total_lines": total, "ranges": merged,
            "enclosing_symbols": symbols, "truncated": truncated,
            "context": "\n   ...\n".join(parts),
        }

    # ------------------------------------------------------------------ misc
    def stats(self) -> dict:
        c = self.conn
        langs = {r[0]: r[1] for r in c.execute("SELECT language, COUNT(*) FROM files GROUP BY language")}
        return {
            "root": self.root, "db": self.indexer.db_path, "embedder": self.embedder.name,
            "files": c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "symbols": c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "chunks": c.execute("SELECT COUNT(*) FROM chunk_metadata").fetchone()[0],
            "dependencies": c.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0],
            "languages": langs,
        }


# ---------------------------------------------------------------------- text rendering

def render_search(res: dict) -> str:
    out = [f'Search: "{res["query"]}"  ({len(res["results"])} results)']
    if not res["results"]:
        out.append("No matches.")
    for i, r in enumerate(res["results"], 1):
        head = f'[{i}] {r["file_path"]}:{r["start_line"]}-{r["end_line"]}'
        if r["symbol"]:
            head += f'  {r["symbol_type"]} {r["symbol"]}'
        out.append(f'{head}  (score {r["score"]}, via {"+".join(r["matched_by"])})')
        if r["signature"]:
            out.append(f'    sig: {r["signature"]}')
        out.extend("    " + ln for ln in r["snippet"].split("\n"))
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
            for x in up["inheritors_or_implementers"]:
                out.append(f'    inherited/implemented by {x["type"]} {x["symbol"]}  {x["file_path"]}:{x["line"]}')
            for x in up["referenced_by"][:25]:
                out.append(f'    referenced at {x["file_path"]}:{x["line"]}' + (f' in {x["in_symbol"]}' if x["in_symbol"] else ""))
            if up["referenced_by_total"] > 25:
                out.append(f'    ... {up["referenced_by_total"] - 25} more references')
        down = d.get("downstream")
        if down is not None:
            out.append("  DOWNSTREAM (what it depends on)")
            for b in down["base_types"]:
                where = ", ".join(f'{t["file_path"]}:{t["line"]}' for t in b["defined_in"]) or "external/unresolved"
                out.append(f'    base/interface {b["name"]}  -> {where}')
            for u in down["uses_types"][:30]:
                where = ", ".join(f'{t["file_path"]}:{t["line"]}' for t in u["defined_in"])
                out.append(f'    uses {u["name"]} (line {u["first_line"]})  -> {where}')
    if res.get("note"):
        out.append(f'note: {res["note"]}')
    return "\n".join(out)


def render_context(res: dict) -> str:
    head = f'{res["file_path"]} ({res["total_lines"]} lines), ranges: ' + ", ".join(f"{a}-{b}" for a, b in res["ranges"])
    if res["enclosing_symbols"]:
        head += "\nenclosing: " + "; ".join(
            f'{s["type"]} {s["symbol"]} ({s["start_line"]}-{s["end_line"]})' for s in res["enclosing_symbols"])
    tail = "\n[truncated: output limit reached; request fewer or narrower ranges]" if res["truncated"] else ""
    return head + "\n" + res["context"] + tail
