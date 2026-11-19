#!/usr/bin/env python3
"""Evaluate frozen navigation questions on pinned real public repositories.

No models or database servers are needed. Each source variant runs in a separate
process and fresh snapshot. Gold is never generated from retrieval output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(command, **kwargs):
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        **kwargs,
    )


def source_hash(root):
    h = hashlib.sha256()
    for p in sorted((root / "local_codebase_indexer").glob("*.py")):
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def snapshot(repo, commit, destination):
    destination.mkdir()
    with tempfile.TemporaryFile() as stream:
        subprocess.run(
            ["git", "-C", str(repo), "archive", commit],
            stdout=stream,
            check=True,
            timeout=120,
        )
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive:
            for entry in archive:
                relative = Path(entry.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Invalid archive member")
                if not entry.isfile():
                    continue
                path = destination / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(entry) as src, path.open("wb") as out:
                    shutil.copyfileobj(src, out)


def worker(args):
    sys.path.insert(0, str(args.source))
    import local_codebase_indexer

    if (
        not Path(local_codebase_indexer.__file__)
        .resolve()
        .is_relative_to(args.source.resolve())
    ):
        raise ValueError("Imported package does not match the requested source tree")
    from local_codebase_indexer.evaluation import evaluate
    from local_codebase_indexer.query import CodebaseTools

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    t = CodebaseTools(str(args.root), auto_refresh=False)
    try:
        if args.mode == "lexical":
            t.semantic_search = partial(t.semantic_search, mode="lexical")
        report = evaluate(t, dataset)
        report["retrieval_mode"] = args.mode
        status = t.index_status()
        if status["failed_files"] or not status["consistency"]["ok"]:
            raise RuntimeError("Failed or inconsistent source index")
        files = [
            r[0]
            for r in t.conn.execute("SELECT file_path FROM files ORDER BY file_path")
        ]
        h = hashlib.sha256()
        for f in files:
            h.update(f.encode() + b"\0" + (args.root / f).read_bytes() + b"\0")
        report.update(
            files=len(files),
            chunks=t.stats()["chunks"],
            corpus_sha256=h.hexdigest(),
            source_sha256=source_hash(args.source),
            python=platform.python_version(),
        )
        args.save.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        t.indexer.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, default=HERE / "real-projects.json")
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "indexer-real-projects",
    )
    p.add_argument("--source", type=Path, default=HERE.parent)
    p.add_argument("--baseline-source", type=Path)
    p.add_argument("--save", type=Path, required=True)
    p.add_argument(
        "--lexical",
        action="store_true",
        help="also record candidate lexical mode without changing the hybrid recall gate",
    )
    p.add_argument(
        "--mode",
        choices=["hybrid", "lexical"],
        default="hybrid",
        help=argparse.SUPPRESS,
    )
    p.add_argument("--max-recall-drop", type=float, default=0.0)
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--dataset", type=Path, help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.save.resolve() == args.manifest.resolve():
        p.error("Save results separately from the frozen question manifest")
    if not 0 <= args.max_recall_drop <= 1:
        p.error("max-recall-drop must be between 0 and 1")
    if not (args.source / "local_codebase_indexer" / "__init__.py").is_file():
        p.error("source must be a package source checkout")
    if args.worker:
        worker(args)
        return 0
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    variants = {"candidate": args.source.resolve()}
    if args.baseline_source:
        variants = {"baseline": args.baseline_source.resolve(), **variants}
    if args.lexical:
        variants["candidate_lexical"] = args.source.resolve()
    results = {
        "environment": {"OPENBLAS_NUM_THREADS": "1"},
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "dataset_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "platform": platform.platform(),
        "authorship": manifest["authorship"],
        "projects": {},
    }
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="indexer-real-eval-") as tmp:
        work = Path(tmp)
        for project in manifest["projects"]:
            name = project["id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not re.fullmatch(
                r"[0-9a-f]{40}", project["commit"]
            ):
                raise ValueError("Invalid project identifier or pinned commit")
            if not project["repository"].startswith("https://github.com/"):
                raise ValueError("Curated public GitHub repositories only")
            repo = args.cache_dir / name
            if not repo.exists():
                run(["git", "clone", "--no-checkout", project["repository"], str(repo)])
            run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "cat-file",
                    "-e",
                    project["commit"] + "^{commit}",
                ]
            )
            dataset = work / (name + "-questions.json")
            dataset.write_text(
                json.dumps(
                    {"version": 1, "queries": project["queries"]}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
            variants_results = {}
            for label, source in variants.items():
                root = work / (name + "-" + label)
                snapshot(repo, project["commit"], root)
                for q in project["queries"]:
                    for gold in q["relevant"]:
                        gold_path = (root / gold["file"]).resolve()
                        if (
                            not gold_path.is_relative_to(root)
                            or not gold_path.is_file()
                        ):
                            raise ValueError("Gold file not in pinned snapshot")
                output = work / (name + "-" + label + ".json")
                env = dict(os.environ, OPENBLAS_NUM_THREADS="1")
                env.pop("INDEXER_ONNX_MODEL", None)
                run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        "--mode",
                        "lexical" if label == "candidate_lexical" else "hybrid",
                        "--source",
                        str(source),
                        "--root",
                        str(root),
                        "--dataset",
                        str(dataset),
                        "--save",
                        str(output),
                    ],
                    env=env,
                    cwd=work,
                )
                variants_results[label] = json.loads(output.read_text(encoding="utf-8"))
                print(
                    name,
                    label,
                    json.dumps(variants_results[label]["metrics"]),
                    flush=True,
                )
            if len({r["corpus_sha256"] for r in variants_results.values()}) != 1:
                raise RuntimeError("Variants did not index the same source corpus")
            results["projects"][name] = {
                "repository": project["repository"],
                "commit": project["commit"],
                **variants_results,
            }
    results["overall"] = {}
    for label in variants:
        rows = [
            row
            for project in results["projects"].values()
            for row in project[label]["rows"]
        ]
        results["overall"][label] = {
            "queries": len(rows),
            "recall_at_5": statistics.mean(r["recall_at_k"] for r in rows),
            "mrr": statistics.mean(r["reciprocal_rank"] for r in rows),
        }
    results["limitations"] = [
        "Project-authored source-reviewed questions, not independent users or end-to-end agent tasks.",
        "Retrieval measures file+symbol relevance, not SQL impact precision, runtime execution, or investigation-time savings.",
        "Single run per question; latency includes the first uncached retrieval and is machine-specific.",
    ]
    if not 0 <= args.max_recall_drop <= 1:
        raise ValueError("max-recall-drop must be between 0 and 1")
    results["passed"] = all(
        "baseline" not in project
        or project["candidate"]["metrics"]["recall_at_k"] + args.max_recall_drop + 1e-12
        >= project["baseline"]["metrics"]["recall_at_k"]
        for project in results["projects"].values()
    )
    args.save.parent.mkdir(parents=True, exist_ok=True)
    args.save.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("overall", json.dumps(results["overall"]))
    return 0 if results["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
