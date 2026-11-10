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

The package is `local_codebase_indexer/`. See `docs/reference.md` for design notes and known limits, `TODO.md` for planned work.

## Diagnostics and bounded results (0.6.0)

- `local-codebase-indexer --root <project> doctor --path <file>` explains exclusions.
- `search <query> --file '*Repository*.cs' --language csharp --limit 5 --offset 5` filters before ranking. Follow `pagination.next_offset` in JSON.
- `data-access <table> --confidence confirmed --limit 20 --offset 20` pages each section independently. JSON has section totals and next offsets; `--full` really lists every entry.
- `impact` includes historical accesses and callers for removed code. `snapshot=before` line numbers refer to the base revision. Historical indexes are cached in `.ai_context/impact/`; deleting that cache is safe.
- A missing root is an error; an empty or failed indexing run returns exit status 3. `--json` and `--root` work before or after the subcommand.

## Development improvements (0.7.0)

- Prefer `search <query> --cursor <pagination.next_cursor>` for subsequent pages; retain query/filters. Cursor errors require a new search. `--explain` / MCP `explain=true` adds matched terms, expansion sources, layer ranks and SQL evidence.
- `evaluate --queries .ai_context/eval.json --save .ai_context/report.json` measures your own verified navigation questions. Optional recall/latency/token budgets fail with exit status 3. Do not change gold answers or lower thresholds merely to make a run pass.
- Exit status 4 means the index is busy; retry after the other process finishes. Configure `INDEXER_LOCK_TIMEOUT` and `INDEXER_BUSY_TIMEOUT` only when needed. Do not delete active lock files.
- `impact A..B` and `A...B` use both Git snapshots and label target lines `after`; a single base revision compares with the working tree.
- Context clipping keeps focus lines. Synonym configuration hot-reloads. ONNX identities include model/tokenizer/configuration fingerprints.
- Future Release events validate and build once, then use the same files for PyPI and GitHub attachments. Validate locally with `python scripts/release_assets.py --tag v<package-version> --dist <directory>`; add `--upload` only when publishing was requested.

## First-run demo (0.8.0)

Run `local-codebase-indexer demo` without a database or model. It uses a temporary bundled synthetic project, checks four relationships, and leaves the current project alone. `--output <new-or-empty-directory>` keeps a copy; `--json` returns evidence and timings. This is a supported-path demonstration, not a production accuracy or time-savings measurement.

## SQL and legacy-source correctness (0.9.0)

- Run `python -m ruff check local_codebase_indexer scripts`, `python -m mypy`, and the full pytest suite before publishing. CI covers Linux and Windows on Python 3.10, 3.12, and 3.13.
- Dependency declarations live in `pyproject.toml`; `requirements.txt` was removed to avoid drift.
- Source decoding and context extraction handle BOM-tagged UTF-16/UTF-32. True binary files are excluded consistently by scanning and status.
- `data-access` has additive `schema_changes` entries (`access=schema`) for table/index DDL. They are not row writers. File-level SQL changes appear in impact with before/current/after labels and source evidence.
- VB6 `.frm`/`.bas`/`.cls` SQL strings get procedure owners. Complete SQL at known ADO/DAO receivers may be confirmed; unknown receivers or incomplete concatenation remain probable/uncertain. No VB6 call graph or runtime execution proof is implied.
- Explicit negations in `.ai_context/ignore` can include default source directories such as `!bin/` and `!bin/**`, overriding `.gitignore`. Include excluded parents first. `.git`, `.svn`, `.hg`, and `.ai_context` remain excluded.
- Keep `query.py` as the facade/search implementation; navigation, source context, and rendering live in `relations.py`, `context.py`, and `rendering.py`. Legacy public imports from `query` remain supported.
