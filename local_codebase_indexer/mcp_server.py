"""Minimal Model Context Protocol server (JSON-RPC 2.0 over stdio, newline-delimited)."""

from __future__ import annotations

import json
import sys

from . import __version__
from .query import CodebaseTools, render_context, render_data_access, render_relations, render_search, render_status

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "codebase_semantic_search",
        "description": "Find code components from a natural language description (hybrid keyword + symbol + vector search with dependency expansion).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Semantic explanation of what you are looking for."},
                "limit": {"type": "integer", "description": "Maximum chunks to return (default 5).", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codebase_get_symbol_relations",
        "description": "Trace inheritance, usages and cross-file dependencies of a class, interface or method.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_name": {"type": "string", "description": 'Name of the class, interface or variable, e.g. "IExistOpenHandleService".'},
                "relation_direction": {"type": "string", "enum": ["upstream", "downstream", "both"]},
            },
            "required": ["symbol_name", "relation_direction"],
        },
    },
    {
        "name": "codebase_get_optimized_context",
        "description": "Return only the relevant lines around focus lines of a file to minimise token usage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Target source file (relative to project root)."},
                "focus_lines": {"type": "array", "items": {"type": "integer"}, "description": "Target lines identified by search tools."},
                "padding_lines": {"type": "integer", "description": "Surrounding lines to extract (default 15).", "default": 15},
            },
            "required": ["file_path", "focus_lines"],
        },
    },
    {
        "name": "codebase_get_data_access",
        "description": "Who reads, writes or executes a database table / stored procedure / view (SQL files and C# SQL strings, Dapper, ADO.NET, EF mappings), including callers of procedures that write it, and what a procedure itself touches. Dynamic SQL is reported as uncertain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "description": 'Table, view or procedure name, optionally schema qualified, e.g. "MES_Lot" or "dbo.usp_Lot_UpdateStatus".'},
            },
            "required": ["object_name"],
        },
    },
    {
        "name": "codebase_index_status",
        "description": "Report whether the index can be trusted: freshness, git commit it was built from, files that failed to index, and consistency checks. Does not refresh the index.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(tools: CodebaseTools, name: str, args: dict) -> str:
    if name == "codebase_semantic_search":
        return render_search(tools.semantic_search(args["query"], args.get("limit", 5)))
    if name == "codebase_get_symbol_relations":
        return render_relations(tools.symbol_relations(args["symbol_name"], args.get("relation_direction", "both")))
    if name == "codebase_get_optimized_context":
        return render_context(tools.optimized_context(args["file_path"], args["focus_lines"], args.get("padding_lines", 15)))
    if name == "codebase_get_data_access":
        return render_data_access(tools.data_access(args["object_name"]))
    if name == "codebase_index_status":
        return render_status(tools.index_status())
    raise KeyError(f"unknown tool: {name}")


def handle(tools: CodebaseTools, msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "local-codebase-indexer", "version": __version__},
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        try:
            text = call_tool(tools, params.get("name", ""), params.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except KeyError as exc:
            if str(exc).startswith("'unknown tool"):
                return err(-32602, str(exc).strip("'\""))
            return ok({"content": [{"type": "text", "text": f"missing argument: {exc}"}], "isError": True})
        except (ValueError, FileNotFoundError) as exc:
            return ok({"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True})
    if mid is None:  # notification (e.g. notifications/initialized): no response
        return None
    return err(-32601, f"method not found: {method}")


def serve(root: str) -> int:
    tools = CodebaseTools(root)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        resp = handle(tools, msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0
