# Instructions for AI coding assistants

This repository is **local-codebase-indexer**: a local code index (SQLite, no cloud) that you can use as an MCP server or CLI to find code, callers, and SQL table/stored-procedure access with far fewer tokens than reading files.

## Install (pick one)

```bash
pip install local-codebase-indexer                      # from PyPI (Python 3.10+)
pipx install local-codebase-indexer                     # isolated
pip install "git+https://github.com/sfy257000/local-codebase-indexer.git"   # latest from GitHub
```

Check: `local-codebase-indexer --help`. No API keys or network access are needed after installation.

## Use it on a project

```bash
local-codebase-indexer --root <project> index                      # build / refresh the index
local-codebase-indexer --root <project> search "natural language or identifier" --limit 5
local-codebase-indexer --root <project> relations <SymbolName> --direction both
local-codebase-indexer --root <project> data-access <TableOrProcedure>   # who reads / writes / executes it
local-codebase-indexer --root <project> context <file> --lines 120 --padding 15
local-codebase-indexer --root <project> status                     # is the index fresh?
```

Put `--json` before the subcommand for JSON output. The index lives in `<project>/.ai_context/` (git-ignored automatically).

## Register as an MCP server (Claude Code)

```bash
claude mcp add local-indexer -- local-codebase-indexer --root <project> serve
```

Tools: `codebase_semantic_search`, `codebase_get_symbol_relations`, `codebase_get_optimized_context`, `codebase_get_data_access`, `codebase_index_status`.

## Reading the results

- Relations and data-access entries carry a confidence: `confirmed`, `probable`, `candidate` / `uncertain`. Treat `candidate` and `uncertain` (dynamic SQL) as leads to verify in the source, not facts.
- If `status` reports `stale`, run `index`.

## Working on this repo itself

```bash
pip install -e ".[dev,onnx]" && python -m pytest -q
python benchmarks/run_benchmark.py --compare benchmarks/baseline.json
```

The package is `local_codebase_indexer/`. See `README.md` for design notes and known limits, `TODO.md` for planned work.
