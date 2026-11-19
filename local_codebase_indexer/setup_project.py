"""Project-local MCP configuration, preserving unrelated client settings."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str, *, overwrite: bool = False) -> None:
    if path.is_symlink() or (path.exists() and not overwrite):
        raise ValueError(
            f"Refusing to replace {path}; choose a new file or explicitly allow overwrite"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as f:
            temporary = f.name
            f.write(text)
        if overwrite:
            os.replace(temporary, path)
        else:
            # Atomic exclusive publication of the already complete temporary file.
            os.link(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def configure(root: str, client: str) -> dict:
    project = Path(root).resolve()
    if not project.is_dir():
        raise ValueError("Project root must be an existing directory")
    server = {
        "command": "local-codebase-indexer",
        "args": ["--root", str(project), "serve"],
    }
    if client == "generic":
        return {"root": str(project), "client": client, "server": server, "written": []}
    path = project / (".mcp.json" if client == "claude" else ".codex/config.toml")
    if not path.parent.resolve().is_relative_to(project) or path.is_symlink():
        raise ValueError(
            "Client configuration must stay inside the project without file symlinks"
        )
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if client == "claude":
        config = json.loads(current) if current else {}
        if not isinstance(config, dict) or not isinstance(
            config.get("mcpServers", {}), dict
        ):
            raise ValueError(
                "Existing .mcp.json must contain an object with an mcpServers object"
            )
        servers = config.setdefault("mcpServers", {})
    elif client == "codex":
        try:
            import tomllib
        except ImportError:  # Python 3.10
            import tomli as tomllib  # type: ignore[no-redef]
        config = tomllib.loads(current)
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("Existing mcp_servers setting must be a table")
    else:
        raise ValueError("client must be claude, codex, or generic")
    if "local-indexer" in servers:
        if servers["local-indexer"] != server:
            raise ValueError(
                "An existing local-indexer server has different settings; merge it manually"
            )
        return {
            "root": str(project),
            "client": client,
            "server": server,
            "written": [],
            "unchanged": [str(path)],
        }
    if client == "claude":
        servers["local-indexer"] = server
        text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    else:
        text = (
            current.rstrip()
            + '\n\n[mcp_servers.local-indexer]\ncommand = "local-codebase-indexer"\nargs = '
            + json.dumps(server["args"], ensure_ascii=False)
            + "\n"
        )
    atomic_write(path, text, overwrite=path.exists())
    return {
        "root": str(project),
        "client": client,
        "server": server,
        "written": [str(path)],
    }
