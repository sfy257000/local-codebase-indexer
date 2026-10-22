"""Command line interface: ``bin/indexer <tool_name> --params`` (index.md section 4)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__


def _tools(root: str):
    from .query import CodebaseTools
    return CodebaseTools(root)


def _emit(data, text: str, as_json: bool) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2) if as_json else text)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="local-codebase-indexer", description="Local codebase indexer & context provider")
    p.add_argument("--root", default=os.environ.get("INDEXER_ROOT", os.getcwd()), help="project root (default: cwd)")
    p.add_argument("--json", action="store_true", help="print JSON instead of compact text")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("index", help="build / incrementally refresh the index")
    s.add_argument("--force", action="store_true", help="re-index every file")

    s = sub.add_parser("search", aliases=["codebase_semantic_search"], help="hybrid semantic search")
    s.add_argument("query", nargs="?")
    s.add_argument("--query", dest="query_opt")
    s.add_argument("--limit", type=int, default=5)

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

    sub.add_parser("stats", help="show index statistics")
    sub.add_parser("status", aliases=["codebase_index_status"], help="index health: freshness, failures, consistency")
    sub.add_parser("serve", help="run as MCP server over stdio (JSON-RPC)")
    return p


def _fix_stdio(serving: bool) -> None:
    """MCP speaks UTF-8 JSON; the Windows locale codec (cp950, cp1252) would garble Chinese queries. On a console,
    keep its encoding but never crash on a character it cannot show."""
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            if serving:
                stream.reconfigure(encoding="utf-8", errors="replace")
            elif name != "stdin":
                stream.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _fix_stdio(serving=args.cmd == "serve")
    root = os.path.abspath(args.root)

    if args.cmd == "serve":
        from .mcp_server import serve
        return serve(root)

    from .query import render_context, render_data_access, render_relations, render_search, render_status

    try:
        t = _tools(root)
        if args.cmd == "index":
            st = t.refresh(force=args.force).as_dict()
            text = (f'indexed {st["scanned"]} files: +{st["added"]} ~{st["updated"]} ={st["unchanged"]} '
                    f'-{st["deleted"]} (skipped {st["skipped"]}, failed {st["failed"]}); {st["symbols"]} symbols, {st["chunks"]} chunks')
            if st["warnings"]:
                text += "\nwarnings:\n  " + "\n  ".join(st["warnings"][:20])
            _emit(st, text, args.json)
        elif args.cmd in ("search", "codebase_semantic_search"):
            q = args.query_opt or args.query
            if not q:
                print("error: a query is required", file=sys.stderr)
                return 2
            res = t.semantic_search(q, args.limit)
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
            res = t.data_access(name)
            _emit(res, render_data_access(res), args.json)
        elif args.cmd in ("status", "codebase_index_status"):
            res = t.index_status()
            _emit(res, render_status(res), args.json)
            return 0 if res["state"] in ("fresh", "stale") else 3
        elif args.cmd == "stats":
            st = t.stats()
            _emit(st, "\n".join(f"{k}: {v}" for k, v in st.items()), args.json)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
