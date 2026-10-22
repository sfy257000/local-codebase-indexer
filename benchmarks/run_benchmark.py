#!/usr/bin/env python3
"""Search quality benchmark.

Indexes a fixed corpus into a temporary directory, runs fixed queries with known
relevant answers and reports Recall@5, Recall@10, MRR, index time, query latency
and an estimated token saving, per category and overall.

    python benchmarks/run_benchmark.py                  # all suites
    python benchmarks/run_benchmark.py --suite synthetic
    python benchmarks/run_benchmark.py --save benchmarks/baseline.json
    python benchmarks/run_benchmark.py --compare benchmarks/baseline.json

A result is relevant when its file equals the expected file and, if the expected
answer names a symbol, the result's qualified symbol name ends with it.
Recall@k = relevant answers found in the top k / relevant answers expected.
MRR = mean of 1 / rank of the first relevant result (0 when nothing is found).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import sysconfig
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from local_codebase_indexer.embedder import tokenize  # noqa: E402
from local_codebase_indexer.query import CodebaseTools, _query_terms, render_search  # noqa: E402

TOP = 10
CHARS_PER_TOKEN = 4  # rough estimate, only meaningful as a ratio


def matches(result: dict, gold: dict) -> bool:
    if result["file_path"] != gold["file"]:
        return False
    sym = gold.get("symbol")
    return sym is None or bool(result["symbol"] and result["symbol"].endswith(sym))


NOISE_PACKAGES = ["email", "json", "http", "asyncio", "unittest"]


def _copy_stdlib(dest: str, packages: list[str]) -> None:
    lib = sysconfig.get_paths()["stdlib"]
    os.makedirs(dest, exist_ok=True)
    for pkg in packages:
        shutil.copytree(os.path.join(lib, pkg), os.path.join(dest, pkg),
                        ignore=shutil.ignore_patterns("__pycache__", "test", "tests", "*.pyc"))


def prepare_corpus(suite: dict, name: str, noise: bool, workdir: str) -> str:
    dest = os.path.join(workdir, name)
    if suite["suite"] == "synthetic":
        shutil.copytree(os.path.join(HERE, suite["corpus"]), dest)
        if noise:  # distractors: thousands of unrelated chunks the answers must compete with
            _copy_stdlib(os.path.join(dest, "_noise"), NOISE_PACKAGES)
    else:
        _copy_stdlib(dest, suite["corpus"].split(":", 1)[1].split(","))
    return dest


def lint_queries(suite: dict) -> list[str]:
    """Synonym / cross-lingual queries are only meaningful if they share no content word with their answers."""
    problems = []
    for q in suite["queries"]:
        if q["category"] not in ("synonym", "cross_lingual"):
            continue
        for gold in q["relevant"]:
            path = os.path.join(HERE, suite["corpus"], gold["file"])
            with open(path, encoding="utf-8") as f:
                words = set(tokenize(f.read()))
            shared = [t for t in _query_terms(q["query"]) if t in words]
            if shared:
                problems.append(f'{q["category"]}: {q["query"]!r} shares {shared} with {gold["file"]}')
    return problems


def evaluate(suite: dict, name: str, noise: bool, workdir: str) -> dict:
    root = prepare_corpus(suite, name, noise, workdir)
    tools = CodebaseTools(root)
    t0 = time.perf_counter()
    st = tools.refresh()
    index_seconds = time.perf_counter() - t0
    t1 = time.perf_counter()
    tools.refresh()
    reindex_seconds = time.perf_counter() - t1

    rows, latencies = [], []
    for q in suite["queries"]:
        t = time.perf_counter()
        res = tools.semantic_search(q["query"], limit=TOP)
        latencies.append((time.perf_counter() - t) * 1000)
        results = res["results"]
        found_rank = []
        for gold in q["relevant"]:
            ranks = [i for i, r in enumerate(results, 1) if matches(r, gold)]
            found_rank.append(ranks[0] if ranks else None)
        first = min((r for r in found_rank if r), default=None)
        n = len(q["relevant"])
        # tokens: what the tool returns for the default 5 results vs reading the whole files it points at
        top5 = results[:5]
        returned = len(render_search({**res, "results": top5})) / CHARS_PER_TOKEN
        whole = 0
        for f in {r["file_path"] for r in top5}:
            try:
                whole += os.path.getsize(os.path.join(root, f)) / CHARS_PER_TOKEN
            except OSError:
                pass
        rows.append({
            "category": q["category"], "query": q["query"],
            "recall5": sum(1 for r in found_rank if r and r <= 5) / n,
            "recall10": sum(1 for r in found_rank if r and r <= 10) / n,
            "rr": 1.0 / first if first else 0.0,
            "first_rank": first,
            "top3": [f'{r["file_path"]}::{r["symbol"] or "-"}' for r in results[:3]],
            "tokens_returned": returned, "tokens_whole_files": whole,
        })
    tools.indexer.close()
    return {
        "suite": name, "files": st.scanned, "chunks": st.chunks, "symbols": st.symbols,
        "index_seconds": round(index_seconds, 2), "noop_reindex_seconds": round(reindex_seconds, 3),
        "median_query_ms": round(statistics.median(latencies), 1),
        "p95_query_ms": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1),
        "rows": rows,
    }


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    ret = sum(r["tokens_returned"] for r in rows)
    whole = sum(r["tokens_whole_files"] for r in rows)
    return {
        "n": n,
        "recall5": round(sum(r["recall5"] for r in rows) / n, 3),
        "recall10": round(sum(r["recall10"] for r in rows) / n, 3),
        "mrr": round(sum(r["rr"] for r in rows) / n, 3),
        "token_ratio": round(ret / whole, 3) if whole else None,
    }


def by_category(rows: list[dict]) -> dict[str, dict]:
    cats: dict[str, list] = {}
    for r in rows:
        cats.setdefault(r["category"], []).append(r)
    return {c: summarize(v) for c, v in cats.items()}


def fmt_table(title: str, cats: dict[str, dict], overall: dict, base: dict | None) -> str:
    def cell(v, b):
        if b is None:
            return f"{v:.2f}"
        d = v - b
        return f"{v:.2f} ({d:+.2f})" if abs(d) >= 0.005 else f"{v:.2f}"
    lines = [f"\n== {title} ==", f'{"category":<18}{"n":>3}  {"Recall@5":<16}{"Recall@10":<16}{"MRR":<16}']
    for name, s in list(cats.items()) + [("OVERALL", overall)]:
        b = (base or {}).get(name)
        lines.append(f'{name:<18}{s["n"]:>3}  {cell(s["recall5"], b and b["recall5"]):<16}'
                     f'{cell(s["recall10"], b and b["recall10"]):<16}{cell(s["mrr"], b and b["mrr"]):<16}')
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite", choices=["synthetic", "synthetic-noisy", "stdlib", "all"], default="all",
                    help="synthetic-noisy = the synthetic corpus buried among ~2,700 unrelated python chunks")
    ap.add_argument("--save", help="write the full results as JSON (e.g. benchmarks/baseline.json)")
    ap.add_argument("--compare", help="a JSON file written by --save; shows deltas")
    ap.add_argument("--misses", action="store_true", help="list queries whose first relevant result is not in the top 5")
    args = ap.parse_args()

    plan = [("synthetic", "synthetic", False), ("synthetic-noisy", "synthetic", True), ("stdlib", "stdlib", False)]
    suites = [(n, f, noise) for n, f, noise in plan if args.suite in (n, "all")]
    baseline = json.load(open(args.compare, encoding="utf-8")) if args.compare else {}
    out = {"python": sys.version.split()[0], "suites": {}}
    workdir = tempfile.mkdtemp(prefix="indexer-bench-")
    try:
        for name, qfile, noise in suites:
            suite = json.load(open(os.path.join(HERE, f"queries_{qfile}.json"), encoding="utf-8"))
            if suite["suite"] == "synthetic":
                for problem in lint_queries(suite):
                    print("LINT:", problem)
            r = evaluate(suite, name, noise, workdir)
            cats = by_category(r["rows"])
            overall = summarize(r["rows"])
            base = baseline.get("suites", {}).get(name, {}).get("categories")
            print(fmt_table(f'{name}: {r["files"]} files, {r["symbols"]} symbols, {r["chunks"]} chunks', cats, overall, base))
            print(f'index {r["index_seconds"]}s (no-op re-index {r["noop_reindex_seconds"]}s)   '
                  f'query median {r["median_query_ms"]}ms, p95 {r["p95_query_ms"]}ms   '
                  f'tokens returned / whole files: {overall["token_ratio"]}')
            if args.misses:
                for row in r["rows"]:
                    if not row["first_rank"] or row["first_rank"] > 5:
                        print(f'  miss [{row["category"]}] {row["query"]!r} first_rank={row["first_rank"]} top3={row["top3"]}')
            out["suites"][name] = {**{k: v for k, v in r.items() if k != "rows"},
                                   "categories": {**cats, "OVERALL": overall}, "rows": r["rows"]}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\nsaved {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
