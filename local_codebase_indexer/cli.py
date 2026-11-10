"""Command line interface: ``local-codebase-indexer <tool_name> --params`` (index.md section 4)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .locking import IndexBusyError


def _tools(root: str):
    from .query import CodebaseTools
    return CodebaseTools(root)


def _emit(data, text: str, as_json: bool) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2) if as_json else text)


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return n


def _nonnegative(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="local-codebase-indexer", description="Local codebase indexer & context provider")
    p.add_argument("--root", default=os.environ.get("INDEXER_ROOT", os.getcwd()), help="project root (default: cwd)")
    p.add_argument("--json", action="store_true", help="print JSON instead of compact text")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("demo", help="trace a bundled SQL Server / C# / Razor sample without a database")
    s.add_argument("--output", help="keep the sample in a new or empty directory")

    s = sub.add_parser("index", help="build / incrementally refresh the index")
    s.add_argument("--force", action="store_true", help="re-index every file")

    s = sub.add_parser("search", aliases=["codebase_semantic_search"], help="hybrid semantic search")
    s.add_argument("query", nargs="?")
    s.add_argument("--query", dest="query_opt")
    s.add_argument("--limit", type=_positive, default=5)
    s.add_argument("--offset", type=_nonnegative, default=0)
    s.add_argument("--cursor", help="continue a search against the same index and settings")
    s.add_argument("--explain", action="store_true", help="show matched terms, expansion sources, and ranking evidence")
    s.add_argument("--file", dest="file_pattern", help="file glob, e.g. '*Repository*.cs'")
    s.add_argument("--language", help="csharp, python, javascript, typescript, tsx or text")
    s.add_argument("--related", action="store_true", help="also list dependency-graph neighbours of the top hits")

    s = sub.add_parser("relations", aliases=["codebase_get_symbol_relations"], help="symbol dependency graph")
    s.add_argument("symbol", nargs="?")
    s.add_argument("--symbol_name", dest="symbol_opt")
    s.add_argument("--direction", "--relation_direction", dest="direction", default="both",
                   choices=["upstream", "downstream", "both"])

    s = sub.add_parser("context", aliases=["slice", "codebase_get_optimized_context"],
                       help="extract only the needed lines of a file")
    s.add_argument("file", nargs="?")
    s.add_argument("--file_path", dest="file_opt")
    s.add_argument("--lines", "--focus_lines", dest="lines", nargs="+", type=int, required=True)
    s.add_argument("--padding", "--padding_lines", dest="padding", type=int, default=15)

    s = sub.add_parser("data-access", aliases=["dataflow", "codebase_get_data_access"],
                       help="who reads / writes / executes a table or stored procedure, and what a procedure touches")
    s.add_argument("object", nargs="?")
    s.add_argument("--object_name", dest="object_opt")
    s.add_argument("--full", action="store_true", help="list every entry instead of a 5-entry summary")
    s.add_argument("--limit", type=_positive, help="page size per section")
    s.add_argument("--offset", type=_nonnegative, default=0)
    s.add_argument("--confidence", choices=["confirmed", "probable", "uncertain"])

    s = sub.add_parser("impact", aliases=["codebase_impact_analysis"],
                       help="symbols, tables, programs and pages touched by a git diff")
    s.add_argument("base", nargs="?", help="git revision or range (default: uncommitted work against HEAD)")
    s.add_argument("--full", action="store_true", help="list every changed symbol")

    s = sub.add_parser("evaluate", help="evaluate real project navigation questions and enforce quality budgets")
    s.add_argument("--queries", required=True, help="versioned JSON question set")
    s.add_argument("--limit", type=_positive, default=5)
    s.add_argument("--baseline", help="earlier report for the same question set")
    s.add_argument("--save", help="save a JSON evaluation report")
    s.add_argument("--min-recall", type=float)
    s.add_argument("--max-p95-ms", type=float)
    s.add_argument("--max-mean-tokens", type=float)
    s.add_argument("--max-recall-drop", type=float, default=0.0)

    s = sub.add_parser("doctor", help="diagnose index coverage and excluded files")
    s.add_argument("--path", help="explain why a specific file is excluded")
    sub.add_parser("stats", help="show index statistics")
    sub.add_parser("status", aliases=["codebase_index_status"], help="index health: freshness, failures, consistency")
    sub.add_parser("serve", help="run as MCP server over stdio (JSON-RPC)")
    for parser in set(sub.choices.values()):
        parser.add_argument("--root", default=argparse.SUPPRESS, help="project root")
        parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    return p


def _fix_stdio(serving: bool) -> None:
    """MCP and piped output (an AI assistant calling the CLI) are UTF-8; the Windows locale codec (cp950, cp1252)
    would garble Chinese. On a console, keep its encoding but never crash on a character it cannot show."""
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            if serving or (name != "stdin" and not stream.isatty()):
                stream.reconfigure(encoding="utf-8", errors="replace")
            elif name != "stdin":
                stream.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _fix_stdio(serving=args.cmd == "serve")
    root = os.path.abspath(args.root)

    from .query import (
        render_context,
        render_data_access,
        render_doctor,
        render_impact,
        render_relations,
        render_search,
        render_status,
    )

    t = None
    try:
        if args.cmd == "serve":
            from .mcp_server import serve
            return serve(root)
        if args.cmd == "demo":
            from .demo import render_demo, run_demo
            res = run_demo(args.output)
            _emit(res, render_demo(res), args.json)
            return 0 if res['passed'] else 3
        t = _tools(root)
        if args.cmd == "index":
            st = t.refresh(force=args.force).as_dict()
            text = (f'indexed {st["scanned"]} files: +{st["added"]} ~{st["updated"]} ={st["unchanged"]} '
                    f'-{st["deleted"]} (skipped {st["skipped"]}, failed {st["failed"]}); {st["symbols"]} symbols, {st["chunks"]} chunks')
            if st["warnings"]:
                text += "\nwarnings:\n  " + "\n  ".join(st["warnings"][:20])
            if not st['scanned']:
                text += "\nNo indexable files. Run doctor to check the root, ignore rules and supported extensions."
                st['hints'] = ["Run doctor to check the root, ignore rules and supported extensions."]
            _emit(st, text, args.json)
            if st['failed'] or not st['scanned']:
                return 3
        elif args.cmd in ("search", "codebase_semantic_search"):
            q = args.query_opt or args.query
            if not q:
                print("error: a query is required", file=sys.stderr)
                return 2
            res = t.semantic_search(q, args.limit, args.related, args.file_pattern, args.language, args.offset, cursor=args.cursor, explain=args.explain)
            _emit(res, render_search(res), args.json)
        elif args.cmd in ("relations", "codebase_get_symbol_relations"):
            sym = args.symbol_opt or args.symbol
            if not sym:
                print("error: a symbol name is required", file=sys.stderr)
                return 2
            res = t.symbol_relations(sym, args.direction)
            _emit(res, render_relations(res), args.json)
        elif args.cmd in ("context", "slice", "codebase_get_optimized_context"):
            f = args.file_opt or args.file
            if not f:
                print("error: a file path is required", file=sys.stderr)
                return 2
            res = t.optimized_context(f, args.lines, args.padding)
            _emit(res, render_context(res), args.json)
        elif args.cmd in ("data-access", "dataflow", "codebase_get_data_access"):
            name = args.object_opt or args.object
            if not name:
                print("error: an object name is required", file=sys.stderr)
                return 2
            res = t.data_access(name, args.limit, args.offset, args.confidence)
            _emit(res, render_data_access(res, args.full), args.json)
        elif args.cmd in ("impact", "codebase_impact_analysis"):
            res = t.impact(args.base)
            _emit(res, render_impact(res, args.full), args.json)
        elif args.cmd in ("status", "codebase_index_status"):
            res = t.index_status()
            _emit(res, render_status(res), args.json)
            return 0 if res["state"] in ("fresh", "stale") else 3
        elif args.cmd == "evaluate":
            from .evaluation import evaluate, render_evaluation, save_report
            if args.save and any(os.path.realpath(args.save) == os.path.realpath(path)
                                 for path in (args.queries, args.baseline) if path):
                raise ValueError('Save the report separately from the input questions and baseline')
            with open(args.queries, encoding='utf-8') as stream:
                dataset = json.load(stream)
            baseline = None
            if args.baseline:
                with open(args.baseline, encoding='utf-8') as stream:
                    baseline = json.load(stream)
            res = evaluate(t, dataset, limit=args.limit, baseline=baseline,
                           min_recall=args.min_recall, max_p95_ms=args.max_p95_ms,
                           max_mean_tokens=args.max_mean_tokens, max_recall_drop=args.max_recall_drop)
            if args.save:
                save_report(res, args.save)
            _emit(res, render_evaluation(res), args.json)
            return 0 if res['passed'] else 3
        elif args.cmd == "doctor":
            res = t.doctor(args.path)
            _emit(res, render_doctor(res), args.json)
        elif args.cmd == "stats":
            st = t.stats()
            _emit(st, "\n".join(f"{k}: {v}" for k, v in st.items()), args.json)
    except IndexBusyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if t is not None:
            t.indexer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
