#!/usr/bin/env python3
"""Exercise real Claude/Codex CLIs against stdio MCP with a local mock model.

No hosted model, real API key, account, or user-level client settings are needed.
This verifies client/tool transport, not model reasoning or answer accuracy.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_codebase_indexer.setup_project import configure  # noqa: E402


def find_tool(tools, namespace=None):
    for tool in tools:
        name = tool.get("name", "")
        if name.endswith("codebase_get_data_access"):
            return name, namespace
        nested = tool.get("tools")
        if isinstance(nested, list):
            found = find_tool(nested, name)
            if found:
                return found
    return None


def has_evidence(value):
    if isinstance(value, dict):
        if value.get("result_type") == "codebase_get_data_access" and any(
            w.get("access") == "write"
            and w.get("confidence") == "confirmed"
            and "UPDATE dbo.Lot" in w.get("evidence", "")
            for w in value.get("writers", [])
        ):
            return True
        return any(has_evidence(item) for item in value.values())
    if isinstance(value, list):
        return any(has_evidence(item) for item in value)
    if isinstance(value, str):
        if "WRITTEN BY" in value and "dbo.Lot" in value:
            return True
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return False
        return has_evidence(decoded) if not isinstance(decoded, str) else False
    return False


class MockModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_json({"models": []})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        request = json.loads(raw)
        if self.path.endswith("count_tokens"):
            self.send_json({"input_tokens": 1})
            return
        state = self.server.state
        state["requests"] += 1
        state["advertised_tool_count"] = len(request.get("tools", []))
        tool = find_tool(request.get("tools", []))
        if tool:
            state["tool_advertised"] = True
        # The result can be a string or a structured tool content block.
        received = has_evidence(request)
        state["tool_result_received"] |= received
        final = received or state["requests"] > 1 or not tool
        if "messages" in request:
            content = (
                [{"type": "text", "text": "Local MCP evidence verified."}]
                if final
                else [
                    {
                        "type": "tool_use",
                        "id": "toolu_local_1",
                        "name": tool[0],
                        "input": {"object_name": "dbo.Lot"},
                    }
                ]
            )
            message = {
                "id": "msg_local",
                "type": "message",
                "role": "assistant",
                "model": request.get("model", "local"),
                "content": content,
                "stop_reason": "end_turn" if final else "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            if not request.get("stream"):
                self.send_json(message)
                return
            events = [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {**message, "content": [], "stop_reason": None},
                    },
                )
            ]
            if final:
                events += [
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                    ),
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": content[0]["text"]},
                        },
                    ),
                ]
            else:
                events += [
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {**content[0], "input": {}},
                        },
                    ),
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(content[0]["input"]),
                            },
                        },
                    ),
                ]
            events += [
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": message["stop_reason"],
                            "stop_sequence": None,
                        },
                        "usage": {"output_tokens": 1},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            if final:
                item = {
                    "id": "msg_local",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Local MCP evidence verified.",
                            "annotations": [],
                        }
                    ],
                }
            else:
                item = {
                    "id": "fc_local",
                    "type": "function_call",
                    "call_id": "call_local_1",
                    "name": tool[0],
                    "arguments": json.dumps({"object_name": "dbo.Lot"}),
                    "status": "completed",
                }
                if tool[1]:
                    item["namespace"] = tool[1]
            response = {
                "id": "resp_local_" + str(state["requests"]),
                "object": "response",
                "status": "completed",
                "model": request.get("model", "local"),
                "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
            if not request.get("stream"):
                self.send_json(response)
                return
            events = [
                (
                    "response.created",
                    {
                        "type": "response.created",
                        "response": {**response, "status": "in_progress", "output": []},
                    },
                ),
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": item,
                    },
                ),
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": item,
                    },
                ),
                (
                    "response.completed",
                    {"type": "response.completed", "response": response},
                ),
            ]
        payload = "".join(
            "event: " + name + "\ndata: " + json.dumps(event) + "\n\n"
            for name, event in events
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--claude")
    p.add_argument("--codex")
    p.add_argument("--save", type=Path, required=True)
    args = p.parse_args()
    args.save.parent.mkdir(parents=True, exist_ok=True)
    if not args.claude and not args.codex:
        p.error("Provide at least one client binary")
    results = {
        "schema_version": 1,
        "model": "local deterministic mock; no reasoning benchmark",
        "clients": {},
    }
    with tempfile.TemporaryDirectory(prefix="indexer-client-smoke-") as tmp:
        root = Path(tmp)
        (root / "x.sql").write_text(
            "CREATE TABLE dbo.Lot(id int);\nGO\nUPDATE dbo.Lot SET id=1;"
        )
        config = configure(str(root), "claude")
        del config
        for client, binary in (("claude", args.claude), ("codex", args.codex)):
            if not binary:
                continue
            server = ThreadingHTTPServer(("127.0.0.1", 0), MockModel)
            server.state = {
                "requests": 0,
                "tool_advertised": False,
                "tool_result_received": False,
            }
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            env = dict(
                os.environ,
                PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                DISABLE_AUTOUPDATER="1",
                ENABLE_TOOL_SEARCH="false",
            )
            for key in (
                "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
            ):
                env.pop(key, None)
            version = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, check=True
            ).stdout.strip()
            if client == "claude":
                env.update(
                    ANTHROPIC_BASE_URL=base, ANTHROPIC_API_KEY="local-test-not-a-secret"
                )
                command = [
                    binary,
                    "-p",
                    "Use the local MCP data access tool for dbo.Lot.",
                    "--strict-mcp-config",
                    "--allowedTools",
                    "mcp__local-indexer__codebase_get_data_access",
                    "--output-format",
                    "json",
                    "--mcp-config",
                    str(root / ".mcp.json"),
                ]
            else:
                configured = subprocess.run(
                    [binary, "mcp", "list", "--json"],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                disable = []
                for entry in json.loads(configured.stdout):
                    if entry["name"] != "local-indexer":
                        disable += [
                            "-c",
                            "mcp_servers." + entry["name"] + ".enabled=false",
                        ]
                command = [
                    binary,
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--json",
                    "-c",
                    "sqlite_home=" + json.dumps(str(root / "codex-state")),
                    "-c",
                    "log_dir=" + json.dumps(str(root / "codex-logs")),
                    "-c",
                    'history.persistence="none"',
                    "-c",
                    'approval_policy="never"',
                    "-c",
                    'model="local-test"',
                    "-c",
                    'model_provider="local_mock"',
                    "-c",
                    'model_providers.local_mock.name="Local integration mock"',
                    "-c",
                    f'model_providers.local_mock.base_url="{base}/v1"',
                    "-c",
                    'model_providers.local_mock.wire_api="responses"',
                    "-c",
                    "model_providers.local_mock.requires_openai_auth=false",
                    "-c",
                    "features.responses_websockets=false",
                    "-c",
                    "mcp_servers.local-indexer.command="
                    + json.dumps(
                        str(Path(sys.executable).parent / "local-codebase-indexer")
                    ),
                    "-c",
                    "mcp_servers.local-indexer.args="
                    + json.dumps(["--root", str(root), "serve"]),
                    "Use the local MCP data access tool for dbo.Lot.",
                ]
                command[2:2] = disable
            try:
                proc = subprocess.run(
                    command,
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                state = server.state.copy()
                if client == "codex":
                    events = []
                    for line in proc.stdout.splitlines():
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            pass
                    state["tool_result_received"] = any(
                        event.get("type") == "item.completed"
                        and event.get("item", {}).get("type") == "mcp_tool_call"
                        and has_evidence(event["item"].get("result"))
                        for event in events
                    )
                passed = (
                    proc.returncode == 0
                    and state["tool_advertised"]
                    and state["tool_result_received"]
                )
                results["clients"][client] = {
                    "version": version,
                    "returncode": proc.returncode,
                    **state,
                    "passed": passed,
                }
                if not passed:
                    # Only this generated project and local mock are involved; keep useful transport diagnostics.
                    (args.save.parent / (client + "-failure.log")).write_text(
                        proc.stdout + "\n" + proc.stderr, encoding="utf-8"
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            print(client, json.dumps(results["clients"][client]), flush=True)
    results["passed"] = all(r["passed"] for r in results["clients"].values())
    args.save.parent.mkdir(parents=True, exist_ok=True)
    args.save.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return 0 if results["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
