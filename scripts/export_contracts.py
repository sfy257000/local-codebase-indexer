#!/usr/bin/env python3
"""Generate/check the committed public MCP output schemas."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_codebase_indexer.mcp_server import SUPPORTED_PROTOCOLS, TOOLS  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    folder = ROOT / "docs/contracts"
    folder.mkdir(exist_ok=True)
    for tool in TOOLS:
        path = folder / (tool["name"] + ".json")
        text = json.dumps(tool["outputSchema"], ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                raise SystemExit("Stale result contract: " + str(path))
        else:
            path.write_text(text, encoding="utf-8")
    manifest = ROOT / "docs/mcp-tools.json"
    text = (
        json.dumps(
            {
                "schema_version": 1,
                "protocol_versions": SUPPORTED_PROTOCOLS,
                "tools": TOOLS,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    if args.check:
        if not manifest.exists() or manifest.read_text(encoding="utf-8") != text:
            raise SystemExit("Stale tool input/output contract manifest")
    else:
        manifest.write_text(text, encoding="utf-8")
    print(
        "Verified seven result contracts"
        if args.check
        else "Exported seven result contracts"
    )


if __name__ == "__main__":
    main()
