#!/usr/bin/env python3
"""Isolated scaling/branch-switch benchmark; generated Python, not production code.

Reports actual chunk counts, first CLI latency, cached/uncached warm p50/p95,
peak RSS, database bytes, one/ten-file updates and a 100-file Git switch.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def elapsed(function):
    start = time.perf_counter()
    value = function()
    return round((time.perf_counter() - start) * 1000, 3), value


def peak_rss_mb():
    if os.name == "nt":
        import psutil

        return psutil.Process().memory_info().peak_wset / (1024 * 1024)
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024


def git(root, *args):
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Benchmark",
            "-c",
            "user.email=benchmark@example.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def query_worker(root, repeats, budget):
    os.environ["INDEXER_VECTOR_CACHE_MB"] = budget
    from local_codebase_indexer.query import CodebaseTools

    t = CodebaseTools(str(root), auto_refresh=False)
    try:
        query = "process shipment customer audit"
        first_ms, first = elapsed(lambda: t.semantic_search(query))
        timings = []
        for _ in range(repeats):
            duration, response = elapsed(lambda: t.semantic_search(query))
            assert response["results"] == first["results"]
            timings.append(duration)
        ordered = sorted(timings)
        return {
            "first_in_process_ms": first_ms,
            "warm_ms": timings,
            "p50_ms": statistics.median(timings),
            "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
            "peak_rss_mb": round(peak_rss_mb(), 2),
            "result_spans": [
                (r["file_path"], r["start_line"], r["end_line"])
                for r in first["results"]
            ],
        }
    finally:
        t.indexer.close()


def build_worker(args):
    from local_codebase_indexer.query import CodebaseTools

    with tempfile.TemporaryDirectory(prefix="indexer-scaling-") as tmp:
        root = Path(tmp)
        count = args.chunks
        for base in range(0, count, 100):
            content = "\n".join(
                f'def process_record_{i:06d}(customer):\n    """Process shipment customer audit record {i}."""\n    return customer, {i}\n'
                for i in range(base, min(base + 100, count))
            )
            (root / f"handlers_{base // 100:05d}.py").write_text(
                content, encoding="utf-8"
            )
        git(root, "init", "-q")
        git(root, "add", ".")
        git(root, "commit", "-qm", "baseline")
        baseline = git(root, "rev-parse", "HEAD").stdout.strip()
        t = CodebaseTools(str(root), auto_refresh=False)
        try:
            index_ms, stats = elapsed(t.refresh)
            if stats.failed:
                raise RuntimeError("Scaling corpus indexing failed")
            report = {
                "requested_functions": count,
                "actual_chunks": stats.chunks,
                "files": stats.scanned,
                "initial_index_ms": index_ms,
                "index_peak_rss_mb": round(peak_rss_mb(), 2),
                "database_bytes": sum(
                    p.stat().st_size for p in (root / ".ai_context").glob("indexer.db*")
                ),
            }
            timings = {}
            for budget in ("0", "128"):
                env = dict(
                    os.environ, INDEXER_VECTOR_CACHE_MB=budget, OPENBLAS_NUM_THREADS="1"
                )
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--query-worker",
                    "--root",
                    str(root),
                    "--repeats",
                    str(args.repeats),
                    "--budget",
                    budget,
                    "--save",
                    str(root / f"query-{budget}.json"),
                ]
                # Reports stay outside the indexed source extension set (.json is indexed), so remove them before updates.
                subprocess.run(
                    command,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                timings[budget] = json.loads(
                    (root / f"query-{budget}.json").read_text()
                )
                (root / f"query-{budget}.json").unlink()
            assert timings["0"]["result_spans"] == timings["128"]["result_spans"]
            report["warm_query"] = {
                "cache_disabled": timings["0"],
                "cache_128mb": timings["128"],
            }
            report["cold_cli_ms"], proc = elapsed(
                lambda: subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "local_codebase_indexer",
                        "--root",
                        str(root),
                        "search",
                        "process shipment customer audit",
                        "--json",
                    ],
                    env=dict(os.environ, OPENBLAS_NUM_THREADS="1"),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            )
            assert json.loads(proc.stdout)["results"]
            paths = sorted(root.glob("*.py"))
            updates = {}
            for n in (1, 10):
                for path in paths[:n]:
                    path.write_text(path.read_text() + f"\n# incremental pass {n}\n")
                ms, changed = elapsed(t.refresh)
                if changed.failed:
                    raise RuntimeError("Incremental indexing failed")
                updates[str(n) + "_files"] = {
                    "elapsed_ms": ms,
                    "stats": changed.as_dict(),
                }
            for path in paths[:100]:
                path.write_text(path.read_text() + "\n# branch alternative\n")
            git(root, "add", ".")
            git(root, "commit", "-qm", "alternative")
            t.refresh()
            switch_ms, _ = elapsed(lambda: git(root, "checkout", "--detach", baseline))
            update_ms, changed = elapsed(t.refresh)
            updates["git_switch"] = {
                "checkout_ms": switch_ms,
                "refresh_ms": update_ms,
                "rewritten_source_files": min(100, len(paths)),
                "stats": changed.as_dict(),
            }
            report["updates"] = updates
            report["peak_rss_mb"] = round(peak_rss_mb(), 2)
            return report
        finally:
            t.indexer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[10000, 50000, 100000])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--save", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--query-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--chunks", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--budget", default="128", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and repeats must be positive")
    sys.path.insert(0, str(HERE.parent))
    if args.query_worker:
        result = query_worker(args.root, args.repeats, args.budget)
    elif args.worker:
        result = build_worker(args)
    else:
        from real_projects import source_hash

        result = {
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "source_sha256": source_hash(HERE.parent),
            "repeats": args.repeats,
            "environment": {"OPENBLAS_NUM_THREADS": "1"},
            "sizes": {},
            "limitations": [
                "Generated repeated Python functions, not real C# projects or SQL data-flow accuracy.",
                "Warm queries are in-process; cold CLI includes process startup and incremental freshness scanning.",
                "p95 uses only the recorded small number of repetitions; no population-latency guarantee.",
            ],
        }
        with tempfile.TemporaryDirectory(prefix="indexer-perf-reports-") as tmp:
            for size in args.sizes:
                output = Path(tmp) / f"{size}.json"
                env = dict(os.environ, OPENBLAS_NUM_THREADS="1")
                env.pop("INDEXER_ONNX_MODEL", None)
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        "--chunks",
                        str(size),
                        "--repeats",
                        str(args.repeats),
                        "--save",
                        str(output),
                    ],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                )
                result["sizes"][str(size)] = json.loads(output.read_text())
                print(size, json.dumps(result["sizes"][str(size)]), flush=True)
    args.save.parent.mkdir(parents=True, exist_ok=True)
    args.save.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
