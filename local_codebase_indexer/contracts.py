"""Versioned additive result contracts shared by CLI JSON and MCP structured output."""

from __future__ import annotations

SCHEMA_VERSION = 1
REQUIRED = {
    "codebase_semantic_search": {
        "query": "string",
        "results": "array",
        "related_files": "array",
    },
    "codebase_get_symbol_relations": {"symbol": "string", "definitions": "array"},
    "codebase_get_optimized_context": {
        "file_path": "string",
        "context": "string",
        "truncated": "boolean",
    },
    "codebase_get_data_access": {
        "object": "string",
        "key": "string",
        "found": "boolean",
    },
    "codebase_impact_analysis": {
        "changed": "array",
        "tables": "array",
        "programs": "array",
    },
    "codebase_index_status": {"root": "string", "state": "string"},
    "codebase_index_doctor": {"root": "string", "eligible_files": "integer"},
}


def output_schema(name: str) -> dict:
    properties = {
        "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
        "result_type": {"type": "string", "const": name},
    }
    properties.update({key: {"type": kind} for key, kind in REQUIRED[name].items()})
    required = list(properties)
    properties["truncation"] = {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["path", "total", "returned"],
            "properties": {
                "path": {"type": "string"},
                "total": {"type": "integer"},
                "returned": {"type": "integer"},
            },
        },
    }
    location: dict = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "line": {"type": "integer", "minimum": 1},
        },
        "required": ["file_path", "line"],
    }
    if name == "codebase_semantic_search":
        properties["mode"] = {"type": "string", "enum": ["hybrid", "lexical"]}
        properties["results"] = {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "file_path",
                    "start_line",
                    "end_line",
                    "snippet",
                    "score",
                    "matched_by",
                ],
                "properties": {
                    "file_path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "snippet": {"type": "string"},
                    "score": {"type": "number"},
                    "matched_by": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    if name == "codebase_get_data_access":
        entry = {
            "type": "object",
            "required": [
                "source",
                "target",
                "file_path",
                "line",
                "access",
                "confidence",
            ],
            "properties": {
                **location["properties"],
                "source": {"type": "string"},
                "target": {"type": "string"},
                "confidence": {
                    "type": "string",
                    "enum": ["confirmed", "probable", "uncertain"],
                },
                "object_identity": {"type": "string"},
                "path": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["symbol", "relation"],
                        "properties": {"symbol": {"type": "string"}},
                    },
                },
            },
        }
        for section in (
            "readers",
            "writers",
            "executed_by",
            "mapped_by",
            "schema_changes",
            "possible",
        ):
            properties[section] = {"type": "array", "items": entry}
        properties["ambiguous"] = {"type": "boolean"}
        properties["candidates"] = {"type": "array", "items": {"type": "string"}}
        properties["coverage"] = {
            "type": "object",
            "required": ["complete_within_limits", "warnings"],
            "properties": {
                "complete_within_limits": {"type": "boolean"},
                "warnings": {
                    "type": "array",
                    "items": {"type": "object", "required": ["code"]},
                },
            },
        }
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": True,
    }


def result(name: str, data: dict) -> dict:
    return {**data, "schema_version": SCHEMA_VERSION, "result_type": name}


def compact(data: dict, caps: dict[str, int]) -> list[dict]:
    """Bound delivered nested collections, retaining totals without breaking paths."""
    omitted: list[dict] = []

    def visit(value, path):
        if isinstance(value, dict):
            for key, item in list(value.items()):
                address = f"{path}.{key}" if path else key
                if isinstance(item, list) and key in caps and len(item) > caps[key]:
                    omitted.append(
                        {"path": address, "total": len(item), "returned": caps[key]}
                    )
                    value[key] = item = item[: caps[key]]
                visit(item, address)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                visit(item, f"{path}[{i}]")

    visit(data, "")
    if omitted:
        data["truncation"] = omitted
    return omitted
