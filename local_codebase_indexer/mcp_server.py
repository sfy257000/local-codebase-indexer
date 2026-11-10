"""Minimal Model Context Protocol server (JSON-RPC 2.0 over stdio, newline-delimited)."""

from __future__ import annotations

import json
import sqlite3
import sys

from . import __version__
from .query import (
    CodebaseTools,
    render_context,
    render_data_access,
    render_doctor,
    render_impact,
    render_relations,
    render_search,
    render_status,
)

PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict] = [
    {
        "name": "codebase_semantic_search",
        "description": "Find code from a description when you do not know the identifiers (Chinese or English; hybrid keyword + symbol + vector search). For an exact identifier or string literal, plain grep is faster and complete.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Semantic explanation of what you are looking for."},
                "limit": {"type": "integer", "description": "Maximum chunks to return (default 5).", "default": 5, "minimum": 1, "maximum": 50},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "cursor": {"type": "string", "description": "Continue using pagination.next_cursor; query and filters must remain unchanged."},
                "explain": {"type": "boolean", "default": False, "description": "Explain matched query terms, synonym sources, layer ranks, and data-access evidence."},
                "file_pattern": {"type": "string", "description": "File glob, e.g. *Repository*.cs"},
                "language": {"type": "string", "enum": ["csharp", "python", "javascript", "typescript", "tsx", "text"]},
                "related": {"type": "boolean", "description": "Also list files linked to the top hits in the dependency graph.", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codebase_get_symbol_relations",
        "description": "Who calls a method (resolved through interfaces / DI to the implementation, with confidence levels), what it calls, and who implements or inherits a type. Use instead of grep when the call goes through an interface. Overloaded names: pass Type.Method.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_name": {"type": "string", "description": 'Class, interface or method name; "Type.Method" or a full qualified name picks one class, e.g. "IExistOpenHandleService" or "SFC_MLotRepository.InsertAsync".'},
                "relation_direction": {"type": "string", "enum": ["upstream", "downstream", "both"]},
            },
            "required": ["symbol_name"],
        },
    },
    {
        "name": "codebase_get_optimized_context",
        "description": "Return only the relevant lines around focus lines of a file to minimise token usage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Target source file (relative to project root)."},
                "focus_lines": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "Target lines identified by search tools."},
                "padding_lines": {"type": "integer", "minimum": 0, "description": "Surrounding lines to extract (default 15).", "default": 15},
            },
            "required": ["file_path", "focus_lines"],
        },
    },
    {
        "name": "codebase_get_data_access",
        "description": "Who reads, writes or executes a database table / stored procedure / view, and which programs (controller actions, followed through the C# call graph) end up touching it. Covers SQL files, C# SQL strings (literals, concatenation, StringBuilder) via Dapper / ADO.NET, EF LINQ on DbSets and EF mappings, plus callers of procedures. Prefer this over grep for impact analysis of a table. Dynamic SQL is reported as uncertain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "description": 'Table, view or procedure name, optionally schema qualified, e.g. "MES_Lot" or "dbo.usp_Lot_UpdateStatus".'},
                "detail": {"type": "boolean", "description": "List every entry instead of the 5-entry summary.", "default": False},
                "limit": {"type": "integer", "minimum": 1, "description": "Page size per section."},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "confidence": {"type": "string", "enum": ["confirmed", "probable", "uncertain"]},
            },
            "required": ["object_name"],
        },
    },
    {
        "name": "codebase_impact_analysis",
        "description": "Impact of a change: symbols on changed lines (git diff), tables / procedures they access, and the programs, Razor pages and caller-less entry points that reach them. Use before committing or when reviewing a branch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": 'Git revision or range to diff against, e.g. "main", "HEAD~3", "origin/main...HEAD". Omit for uncommitted work (staged, unstaged, untracked).'},
                "detail": {"type": "boolean", "description": "List every changed symbol instead of the first 15.", "default": False},
            },
        },
    },
    {
        "name": "codebase_index_status",
        "description": "Report whether the index can be trusted: freshness, git commit it was built from, files that failed to index, and consistency checks. Does not refresh the index.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {"name": "codebase_index_doctor", "description": "Diagnose index coverage and explain why a file is excluded.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
]


def call_tool(tools: CodebaseTools, name: str, args: dict) -> str:
    if name == "codebase_semantic_search":
        return render_search(tools.semantic_search(args["query"], args.get("limit", 5), args.get("related", False),
                                                   args.get("file_pattern"), args.get("language"), args.get("offset", 0),
                                                   cursor=args.get("cursor"), explain=args.get("explain", False)))
    if name == "codebase_get_symbol_relations":
        return render_relations(tools.symbol_relations(args["symbol_name"], args.get("relation_direction", "both")))
    if name == "codebase_get_optimized_context":
        return render_context(tools.optimized_context(args["file_path"], args["focus_lines"], args.get("padding_lines", 15)))
    if name == "codebase_get_data_access":
        return render_data_access(tools.data_access(args["object_name"], args.get("limit"), args.get("offset", 0), args.get("confidence")), bool(args.get("detail")))
    if name == "codebase_impact_analysis":
        return render_impact(tools.impact(args.get("base")), bool(args.get("detail")))
    if name == "codebase_index_status":
        return render_status(tools.index_status())
    if name == "codebase_index_doctor":
        return render_doctor(tools.doctor(args.get("path")))
    raise KeyError(f"unknown tool: {name}")


def _validate(value, schema: dict, path: str = 'arguments') -> None:
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool}
    expected = schema.get('type')
    if expected in types and (not isinstance(value, types[expected]) or (expected == 'integer' and isinstance(value, bool))):
        raise ValueError(f"{path} must be {expected}")
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if expected == 'integer':
        if value < schema.get('minimum', value) or value > schema.get('maximum', value):
            raise ValueError(f"{path} is outside the allowed range")
    if expected == 'string' and not value.strip():
        raise ValueError(f"{path} must not be empty")
    if expected == 'object':
        for key in schema.get('required', []):
            if key not in value:
                raise ValueError(f"missing argument: {key}")
        for key, prop in schema.get('properties', {}).items():
            if key in value:
                _validate(value[key], prop, path + '.' + key)
    if expected == 'array':
        for i, item in enumerate(value):
            _validate(item, schema.get('items', {}), f"{path}[{i}]")


def handle(tools: CodebaseTools, msg) -> dict | None:
    mid = msg.get('id') if isinstance(msg, dict) else None
    if not isinstance(mid, (str, int, type(None))) or isinstance(mid, bool):
        mid = None
    def err(code, message):
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}}
    def ok(result):
        return {'jsonrpc': '2.0', 'id': mid, 'result': result}
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
        return err(-32600, 'Invalid Request: expected a JSON-RPC 2.0 request object')
    if 'id' in msg and msg['id'] is not None and mid is None:
        return err(-32600, 'Invalid Request: id must be a string or number')
    if 'id' not in msg:  # Notifications never get a response or invoke tools.
        return None
    params = msg.get('params', {})
    if not isinstance(params, dict):
        return err(-32602, 'params must be an object')
    method = msg['method']
    if method == 'initialize':
        return ok({'protocolVersion': PROTOCOL_VERSION, 'capabilities': {'tools': {}},
                   'serverInfo': {'name': 'local-codebase-indexer', 'version': __version__}})
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok({'tools': TOOLS})
    if method != 'tools/call':
        return err(-32601, f'method not found: {method}')
    name = params.get('name')
    spec = next((t for t in TOOLS if t['name'] == name), None)
    if spec is None or not isinstance(name, str):
        return err(-32602, 'unknown tool')
    try:
        args = params.get('arguments', {})
        _validate(args, spec['inputSchema'])
        text = call_tool(tools, name, args)
        return ok({'content': [{'type': 'text', 'text': text}], 'isError': False})
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        return ok({'content': [{'type': 'text', 'text': f'error: {exc}'}], 'isError': True})
    except Exception as exc:
        print(f'[indexer] tool failed: {type(exc).__name__}', file=sys.stderr)
        return err(-32603, 'Internal error; the server is still available')


def serve(root: str) -> int:
    tools = CodebaseTools(root)
    try:
        for raw in sys.stdin:
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                resp: dict | None = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
            else:
                resp = handle(tools, msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
                sys.stdout.flush()
    finally:
        tools.indexer.close()
    return 0
