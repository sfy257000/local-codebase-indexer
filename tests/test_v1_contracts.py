import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from local_codebase_indexer.mcp_server import SUPPORTED_PROTOCOLS, handle


@pytest.mark.parametrize("version", [*SUPPORTED_PROTOCOLS, "2099-01-01"])
def test_protocol_negotiation(version):
    r = handle(
        None,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": version},
        },
    )
    assert r["result"]["protocolVersion"] == (
        version if version in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
    )


def test_protocol_version_type_error():
    assert (
        handle(
            None,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 42},
            },
        )["error"]["code"]
        == -32602
    )


def test_official_mcp_client_can_call_and_validate_all_seven_tools(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "x.sql").write_text(
        "CREATE TABLE dbo.Lot(id int);\nGO\nUPDATE dbo.Lot SET id=1;\n"
    )
    (tmp_path / "x.py").write_text("def sample():\n    return 1\n")
    for args in [
        ("init", "-q"),
        ("add", "."),
        (
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ),
    ]:
        subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
        )
    (tmp_path / "x.py").write_text("def sample():\n    return 2\n")
    calls = {
        "codebase_semantic_search": {"query": "Lot"},
        "codebase_get_symbol_relations": {"symbol_name": "sample"},
        "codebase_get_optimized_context": {"file_path": "x.py", "focus_lines": [1]},
        "codebase_get_data_access": {"object_name": "dbo.Lot"},
        "codebase_impact_analysis": {},
        "codebase_index_status": {},
        "codebase_index_doctor": {},
    }

    async def exercise():
        env = dict(os.environ)
        env.pop("INDEXER_ONNX_MODEL", None)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "local_codebase_indexer", "--root", str(tmp_path), "serve"],
            cwd=str(root),
            env=env,
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                assert {t.name for t in listing.tools} == set(calls)
                for tool in listing.tools:
                    response = await session.call_tool(tool.name, calls[tool.name])
                    assert not response.isError, (tool.name, response)
                    assert response.structuredContent["schema_version"] == 1
                    jsonschema.validate(response.structuredContent, tool.outputSchema)
                    assert (
                        response.content[0].type == "text" and response.content[0].text
                    )
                missing = await session.call_tool(
                    "codebase_get_data_access", {"object_name": "LotMissing"}
                )
                assert (
                    not missing.isError and missing.structuredContent["found"] is False
                )

    asyncio.run(exercise())


def test_recorded_native_codex_results_remain_compatible_with_the_public_contract():
    from local_codebase_indexer.contracts import output_schema

    root = Path(__file__).resolve().parents[1]
    record = json.loads(
        (root / "benchmarks/codex-tool-events.json").read_text(encoding="utf-8")
    )
    assert record["events"]
    for event in record["events"]:
        item = event["item"]
        assert item["status"] == "completed" and item["server"] == "local-indexer"
        result = item["result"]["structured_content"]
        jsonschema.validate(result, output_schema("codebase_get_data_access"))
        assert result["writers"][0]["confidence"] == "confirmed"


def test_structured_mcp_defaults_page_large_access_lists_and_preserve_totals(tmp_path):
    from local_codebase_indexer.query import CodebaseTools

    (tmp_path / "many.sql").write_text(
        "\n".join("UPDATE dbo.Lot SET id=1;" for _ in range(30))
    )
    t = CodebaseTools(str(tmp_path))
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "codebase_get_data_access",
                "arguments": {"object_name": "dbo.Lot"},
            },
        }
        short = handle(t, request)["result"]["structuredContent"]
        assert len(short["writers"]) == 5
        assert short["pagination"]["sections"]["writers"]["total"] == 30
        assert short["pagination"]["sections"]["writers"]["next_offset"] == 5
        request["params"]["arguments"]["offset"] = 5
        second = handle(t, request)["result"]["structuredContent"]
        assert second["writers"][0]["line"] == 6
        request["params"]["arguments"] = {"object_name": "dbo.Lot", "detail": True}
        full = handle(t, request)["result"]["structuredContent"]
        assert len(full["writers"]) == 30
    finally:
        t.indexer.close()


def test_structured_summary_keeps_whole_paths_and_reports_nested_truncation():
    from local_codebase_indexer.contracts import compact

    steps = [{"symbol": str(i), "relation": "call"} for i in range(20)]
    data = {"programs": [{"paths": [{"steps": steps} for _ in range(30)]}]}
    cuts = compact(data, {"paths": 5})
    assert len(data["programs"][0]["paths"]) == 5
    assert len(data["programs"][0]["paths"][0]["steps"]) == 20
    assert cuts == [{"path": "programs[0].paths", "total": 30, "returned": 5}]


def test_busy_index_returns_an_explicit_retryable_tool_error():
    from local_codebase_indexer.locking import IndexBusyError

    class BusyTools:
        def semantic_search(self, *args, **kwargs):
            raise IndexBusyError("Another writer owns the index")

    r = handle(
        BusyTools(),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "codebase_semantic_search",
                "arguments": {"query": "test"},
            },
        },
    )
    assert r["result"]["isError"]
    assert r["result"]["structuredContent"]["error"]["code"] == "index_busy"
    assert r["result"]["structuredContent"]["error"]["retryable"] is True
