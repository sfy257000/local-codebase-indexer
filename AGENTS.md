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
local-codebase-indexer --root <project> data-access <TableOrProcedure>   # who reads / writes / executes it, which programs and pages reach it
local-codebase-indexer --root <project> impact [base]               # what a diff touches: symbols, tables, programs, pages
local-codebase-indexer --root <project> context <file> --lines 120 --padding 15
local-codebase-indexer --root <project> status                     # is the index fresh?
```

Put `--json` before or after the subcommand for JSON output. The index lives in `<project>/.ai_context/` (git-ignored automatically).

## Install as a Claude Code plugin

```text
/plugin marketplace add sfy257000/local-codebase-indexer
/plugin install local-codebase-indexer@local-codebase-indexer
```

Needs `uv` (`pip install uv`). Then restart Claude Code.

## Register as an MCP server (Claude Code)

```bash
claude mcp add local-indexer -- local-codebase-indexer --root <project> serve
```

Tools: `codebase_semantic_search`, `codebase_get_symbol_relations`, `codebase_get_optimized_context`, `codebase_get_data_access`, `codebase_impact_analysis`, `codebase_index_status`, `codebase_index_doctor`.

## Reading the results

- Relations and data-access entries carry a confidence: `confirmed`, `probable`, `candidate` / `uncertain`. Treat `candidate` and `uncertain` (dynamic SQL) as leads to verify in the source, not facts.
- If `status` reports `stale`, run `index`.

## Working on this repo itself

```bash
pip install -e ".[dev,onnx]" onnx && python -m pytest -q
python benchmarks/run_benchmark.py --compare benchmarks/baseline.json
```

The package is `local_codebase_indexer/`. See `README.md` for design notes and known limits, `TODO.md` for planned work.

## Diagnostics and bounded results (0.6.0)

- `local-codebase-indexer --root <project> doctor --path <file>` explains exclusions.
- `search <query> --file '*Repository*.cs' --language csharp --limit 5 --offset 5` filters before ranking. Follow `pagination.next_offset` in JSON.
- `data-access <table> --confidence confirmed --limit 20 --offset 20` pages each section independently. JSON has section totals and next offsets; `--full` really lists every entry.
- `impact` includes historical accesses and callers for removed code. `snapshot=before` line numbers refer to the base revision. Historical indexes are cached in `.ai_context/impact/`; deleting that cache is safe.
- A missing root is an error; an empty or failed indexing run returns exit status 3. `--json` and `--root` work before or after the subcommand.
