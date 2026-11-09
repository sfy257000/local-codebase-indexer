# Local Codebase Indexer

**English** | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

A local codebase indexer and context provider for AI coding assistants (Claude Code via MCP, GitHub Copilot / Codex via CLI). It helps them navigate large legacy codebases with far fewer tokens. Everything is stored in `.ai_context/indexer.db` inside your project, with no cloud services involved. The design follows [`index.md`](index.md).

## Install

```bash
pip install local-codebase-indexer              # Python 3.10+
# or, isolated from your other packages:
pipx install local-codebase-indexer
# optional real embeddings (see below): pip install "local-codebase-indexer[onnx]"
```

From a clone: `pip install -e .` (the `bin/indexer` launcher still works in a checkout).

## For AI assistants

If you are an AI coding assistant asked to set this up: run `pip install local-codebase-indexer`, then `claude mcp add local-indexer -- local-codebase-indexer --root <project> serve` (Claude Code) or use the CLI commands below. Full instructions are in [`AGENTS.md`](AGENTS.md).

### As a Claude Code plugin

```text
/plugin marketplace add sfy257000/local-codebase-indexer
/plugin install local-codebase-indexer@local-codebase-indexer
```

This registers the MCP server for the project you open (it runs `uvx local-codebase-indexer --root <project> serve`, so [uv](https://docs.astral.sh/uv/) must be installed: `pip install uv`). Restart Claude Code afterwards.

## Usage (CLI)

```bash
local-codebase-indexer --root <project> index                       # build / incrementally refresh the index (--force rebuilds all)
local-codebase-indexer search "database mapping for LotNo" --limit 5
local-codebase-indexer relations IExistOpenHandleService --direction both   # upstream | downstream | both
local-codebase-indexer context Services/LotService.cs --lines 120 --padding 15   # alias: slice
local-codebase-indexer data-access MES_Lot                           # who reads / writes / executes a table or procedure; what a procedure touches (--full: every entry)
local-codebase-indexer impact main...HEAD                            # symbols, tables, programs and pages a change touches (no argument: uncommitted work)
local-codebase-indexer stats
local-codebase-indexer status                                       # is the index fresh? failed files, git commit, consistency checks
```

- Without `--root` the current directory is used; `INDEXER_ROOT` also works.
- Put `--json` before or after the subcommand to get JSON output.
- `search`, `relations` and `context` run an incremental refresh first (mtime check, then md5 only when mtime changed).
- The tool names from the spec also work as subcommands: `codebase_semantic_search`, `codebase_get_symbol_relations`, `codebase_get_optimized_context` (options `--query`, `--symbol_name --relation_direction`, `--file_path --focus_lines --padding_lines`).

## Usage (Claude Code / MCP)

```bash
claude mcp add local-indexer -- local-codebase-indexer --root /path/to/project serve
# without installing: claude mcp add local-indexer -- uvx local-codebase-indexer --root /path/to/project serve
```

`serve` speaks JSON-RPC (MCP) over stdio and exposes seven tools: `codebase_semantic_search`, `codebase_get_symbol_relations`, `codebase_get_optimized_context`, `codebase_get_data_access`, `codebase_impact_analysis`, `codebase_index_status`, `codebase_index_doctor`. A long-lived server re-checks the index before a query when the last check is older than 15 s (a no-op check takes about a second on a few thousand files). `codebase_get_data_access` returns a summary (5 entries per section, every program) unless `detail` is set; `codebase_semantic_search` lists dependency-graph neighbours only with `related`.

## Diagnose missing code and control output

```bash
local-codebase-indexer --root <project> doctor
local-codebase-indexer --root <project> doctor --path src/Service.cs
local-codebase-indexer --root <project> search "retrieve invoice" --file '*.py' --language python --limit 5
local-codebase-indexer --root <project> search "retrieve invoice" --file '*.py' --language python --limit 5 --offset 5
local-codebase-indexer --root <project> data-access MES_Lot --confidence confirmed --limit 20 --offset 0
local-codebase-indexer --root <project> data-access MES_Lot --full
```

`doctor` reports supported text files, ignore rules, excluded directories, unsupported extensions, files over 5 MB, binary files and index health. `--path` explains a specific file, including a parent directory that prevents indexing. Directory counts are counts of excluded directories, not all the files hidden inside them. Nested `.gitignore` negations override parent file patterns; an excluded parent directory must first be re-included, just as with Git. Built-in excluded directories such as `node_modules` remain excluded.

Search filters apply **before** ranking. `--file` is a case-sensitive glob over the complete project-relative path (`*` also matches `/`); languages are `csharp`, `python`, `javascript`, `typescript`, `tsx`, and `text`. The JSON `pagination` object has `has_more` and `next_offset`; keep the same query and filters for subsequent pages. Offset pages assume the index remains unchanged; cursor pages reject a changed index or configuration. No-result responses suggest similar symbols where available and explain useful next steps. General verb paraphrases expand keyword/symbol matching without diluting vector queries; project-specific jargon still belongs in `.ai_context/synonyms.json`.

Data-access `--confidence` selects an exact level (`confirmed`, `probable`, or `uncertain`). `--limit` and `--offset` apply independently to each section, including programs and procedure accesses. JSON includes `pagination.sections` with totals and next offsets. MCP accepts the same `limit`, `offset`, and `confidence` fields; search uses `file_pattern` and `language`. `--full` / MCP `detail=true` lists all entries when no explicit page size is supplied. The old 40-entry display and 200-entry query caps have been removed.

`impact` includes both current symbols and affected symbols/accesses/callers from the base revision, so deleting a method or file remains visible. `snapshot: before` means its line numbers refer to the base revision, not the current file. The first request for a base revision builds a temporary historical index, which can take time on a large repository; a complete baseline is cached under `.ai_context/impact/` for reuse. Remove this directory to reclaim space. Historical analysis covers files in the Git archive; external `extra_roots` are not reconstructed. Static call-graph confidence and known language limitations still apply.

A nonexistent `--root` is rejected without creating it. `index` returns exit status 3 for an empty or partially failed run; use `doctor` and `status` to diagnose it. Invalid MCP inputs return an error while keeping the server available for the next request.

## Stable navigation and search explanations

Search ranking is independent of `--limit` and `--offset`. To continue a search safely, use the opaque `pagination.next_cursor` returned in JSON, or the cursor printed by the text/MCP response:

```bash
local-codebase-indexer --root <project> search "retrieve invoice" --limit 5 --explain --json
local-codebase-indexer --root <project> search "retrieve invoice" --limit 5 --cursor '<next_cursor>' --json
```

Keep the same query, file glob and language when using a cursor; the page size may change. Cursors are bound to a completed index revision, the embedding model, and synonym settings. A no-op refresh keeps them valid; a changed index or configuration rejects the cursor with an instruction to start again. `--offset` remains supported but has no revision guard. MCP search accepts `cursor` and `explain` too.

`--explain` adds matched query terms, matched synonym expansions with their built-in/project/mined-comment sources, and each retrieval layer's rank and RRF contribution. Data-access hits also carry SQL evidence, confidence, access mechanism and procedure chains. Existing `relations` output includes caller resolution reasons and confidence. These are static-analysis/retrieval explanations, not proof that a runtime path executes or a score is a probability. Explanations are omitted by default to keep responses compact.

Project synonym settings reload on subsequent queries, including deletion and replacement of the settings file. Invalid settings log a warning and fall back to built-in groups; the same invalid contents are not repeatedly logged. ONNX identities include a streamed content fingerprint of the model directory (including tokenizer and external weights), pooling, dimensions and query prefix. Use a dedicated model directory: changing other files in that directory can also cause a rebuild. Generated `.ai_context`, environment and cache directories are excluded from the fingerprint. Changes to loaded model files are rejected; restart the server to load the updated model. Processes sharing an index must use the same model/settings.

Large context requests retain focus lines first, fit neighboring lines within the character budget, and clip individual oversized lines. Returned ranges describe the content actually returned; `omitted_lines` and `truncated_lines` explain clipping. An entirely out-of-file focus request is an error.

Git ranges such as `A..B` and `A...B` analyze the actual target commit, even when HEAD or the working tree differs. Range results use `snapshot=after`; `revisions` identifies both commits. A single revision still compares that revision against the working tree. Historical line numbers always belong to their labeled snapshot.

## Evaluate your own project questions

Keep questions collected from real navigation work in `.ai_context/eval.json` (ignored by Git and the index):

```json
{
  "version": 1,
  "queries": [
    {
      "id": "invoice-read",
      "query": "Where do we retrieve an invoice?",
      "language": "python",
      "relevant": [{"file": "src/invoices.py", "symbol": "load_invoice"}]
    }
  ]
}
```

Replace the example with verified file paths and optional qualified-symbol suffixes from your project. Each question needs a unique ID and at least one expected answer. `file_pattern` and `language` are optional query filters. Avoid copying question text into the source corpus or authoring only questions that already rank well.

```bash
local-codebase-indexer --root <project> evaluate --queries <project>/.ai_context/eval.json --save <project>/.ai_context/baseline.json
local-codebase-indexer --root <project> evaluate --queries <project>/.ai_context/eval.json --baseline <project>/.ai_context/baseline.json --min-recall 0.8 --max-recall-drop 0.02 --max-p95-ms 500 --max-mean-tokens 1500 --save <project>/.ai_context/current.json
```

The report includes Recall@k, hit rate, MRR, p50/p95 latency, estimated output tokens, per-question answers and the index/model/configuration identity. Indexing time is separate from search latency. Tokens are estimated as `ceil(rendered characters / 4)`, not counted by a specific model tokenizer. Empty/invalid datasets fail; breached quality or performance budgets return exit status 3. Baseline comparisons require the same questions, answers, filters and result limit. Reports cannot overwrite their input questions or baseline.

`benchmarks/queries_project.json` supplies 15 curated questions about this repository's actual implementation. They are navigation smoke examples, not collected user queries or an independent quality benchmark. Run them with `local-codebase-indexer --root . evaluate --queries benchmarks/queries_project.json --save .ai_context/project-report.json`; use your own recorded questions for representative quality gates.

## Multiple local processes

Index initialization and writes are coordinated with an OS lock. Query reads use a consistent SQLite snapshot and wait for a writer to finish its batches. POSIX readers share a lock; Windows readers serialize. Locks are automatically released on process exit; do not delete `.lock` files while processes are running.

`INDEXER_LOCK_TIMEOUT` controls the maximum process-lock wait (default 10 seconds), and `INDEXER_BUSY_TIMEOUT` controls SQLite's wait for writers that do not use the indexer's lock (default 5 seconds). Both accept finite nonnegative seconds. CLI contention returns exit status 4 with a retry hint; MCP reports a tool error while keeping the session available. Interrupted batch indexing is reported as degraded and automatically forces a complete refresh on the next indexing run. Separate `CodebaseTools` instances/connections are required for separate threads.

## Release automation

Publishing a future GitHub Release runs tests, builds distributions once, validates package/tag/registry versions, and generates `SHA256SUMS`. PyPI and GitHub attachments receive the same validated artifacts. The attachment job uses GitHub Actions' built-in token with `contents: write`; no personal upload token is needed. Repeating the attachment job skips identical assets and refuses to overwrite conflicting checksums.

For local validation without publishing: `python scripts/release_assets.py --tag v<package-version> --dist <build-directory>`. The explicit `--upload --repo owner/name` flags perform uploads. Existing releases are not modified by editing this workflow.

## Three-layer index

| Layer | Implementation |
|---|---|
| Syntactic | tree-sitter for C#, Python, JavaScript, TypeScript/TSX; regex for SQL (PROCEDURE / VIEW / FUNCTION / TABLE); other text files (VB6 `.frm`, `.config`, `.md`, ...) are split into windowed chunks |
| Topology | `using` / `import` resolved to file dependencies, inheritance (base lists) and type usage (`invocation`), stored in the `dependencies` table |
| Semantic | 384-dim chunk embeddings in `vss_chunks`; queries fuse FTS5 keyword, symbol-name and vector rankings with Reciprocal Rank Fusion, then expand to the dependency neighbours of the top 3 files |

Encodings: UTF-8 first, then Big5, then cp1252. Files over 5MB are skipped. Root and nested `.gitignore` files are respected, and `.git`, `bin`, `obj`, `node_modules` and similar directories are always skipped. `*.min.js` / `*.min.css` are skipped too. Third-party or generated code that is committed to the repo can be excluded with `.ai_context/ignore` (gitignore syntax), e.g. `Scripts/*`, `!Scripts/Custom/`, `Web References/` (keep generated EF database-first models: their DbContext declares the DbSets).

## Differences from the spec

- **Vector storage**: `sqlite-vss` needs a native extension and is no longer maintained, so `vss_chunks` stores float32 BLOBs and cosine similarity is computed with numpy. No extension to install.
- **Embedding model**: the default is a built-in hashing embedder (identifier sub-words, bigrams, CJK character bigrams). It is fast and reproducible but **does not understand synonyms**. For real transformer embeddings, export `all-MiniLM-L6-v2` or `bge-small-en-v1.5` to ONNX, install `onnxruntime` and `tokenizers`, and set `INDEXER_ONNX_MODEL=<folder with model.onnx and tokenizer.json>` (mean pooling suits MiniLM, bge models recommend CLS pooling). Switching embedders triggers a full re-index. Measured (0.4.0): `all-MiniLM-L6-v2` raises benchmark MRR by 0.08-0.13, but on a real 30k-chunk C# / Chinese-comment codebase it scored lower than hashing on paraphrased queries (MRR 0.29 vs 0.48; multilingual MiniLM-L12: 0.41) while a full index took 35-43 minutes instead of 2. Hashing stays the default; for domain jargon, `.ai_context/synonyms.json` helps more than a general model.
- **Call relations** for C# are resolved with limited type inference (using / namespaces, field and constructor-injected types, `Lazy<T>.Value`, locals, `foreach (var x in items.Where(..))` element types, interface to implementation), and every edge carries a confidence: `confirmed` (the receiver type is known), `probable` (reached through an interface or a single candidate) or `candidate` (name match only). There is no Roslyn: no overload resolution by argument type, no delegates / lambdas / extension methods, and call chains are followed one level. Inheritance edges come from the AST and are reliable.
- Extra schema additions: `symbols.qualified_name`, `chunk_metadata.symbol_id`, and the `raw_refs`, `chunks_fts` and `meta` tables.

## Data access (SQL)

`data-access <table-or-procedure>` answers "what writes / reads / executes X". It is built from T-SQL (INSERT / UPDATE / DELETE / MERGE / TRUNCATE / SELECT INTO / FROM / JOIN / EXEC, with alias resolution; CTEs, temp tables and system objects excluded), from C# SQL strings (literals, `+`, interpolation, `string.Format`, consts, locals, `StringBuilder.Append` / `AppendLine` / `AppendFormat`) passed to Dapper, `SqlCommand` or EF `FromSql`, from stored-procedure names used with `CommandType.StoredProcedure`, from EF LINQ on `DbSet<T>` members (`ctx.Orders.Where(..)` / `.Add(..)` / `.Remove(..)`, `from o in ctx.Orders`; `probable`, since only the member name is matched) and from EF `ToTable` / `[Table]` mappings. A procedure's callers are indirect writers of the tables it writes (followed up to 4 hops). `programs` (USED BY PROGRAMS) follows confirmed / probable C# calls upward from those accessors to `*Controller` methods (up to 8 hops) (including from an implementation to the callers of the interface method it implements, for `foreach (var step in steps) step.Run()` pipelines) and groups them per controller file, with the Razor pages (`.cshtml`) whose `Url.Action("Action"[, "Controller"])` or AngularJS `ControllerPath + 'Action'` (controller from `$scope.Controller = 'X'`) targets those actions, so a table maps to the screens that touch it. A method that reads a DbSet and calls `SaveChanges` without an explicit `Add` / `Remove` may update tracked entities; it is listed as a possible (`uncertain`) writer. Each entry has a confidence; **dynamic SQL (`EXEC(@sql)`, `sp_executesql`, SQL built from unknown values) cannot be resolved and is listed separately as `uncertain`**, never mixed into the confirmed lists. EF accesses are `confirmed` when the context variable (`using (var db = new X())`, a field or property, or `this`) is typed as a class that declares or inherits the DbSet, `probable` otherwise. Methods on the way up that have no confirmed / probable caller at all are listed under NO KNOWN CALLER: entry points such as API endpoints, jobs or `Main`, or calls the graph could not resolve. Known gaps: `FROM a JOIN b ON ..., c` loses `c`, and a stored-procedure name passed to a non-database method is not seen.

Stored procedures that live only in the database can be exported to a folder outside the repo and listed in `.ai_context/extra_roots` (one folder per line, absolute or relative to the root); their files are indexed under their absolute paths.

Natural-language search routes questions such as "who modifies MES_ScrapRecord" or "哪些程式會寫入 MES_Lot" to this index first.

## Query expansion

Queries are expanded with a small synonym dictionary (`SP` / `預存程序` / `stored procedure` / `usp`, table, lot, scrap, ...; `local_codebase_indexer/synonyms.py`). Add or disable groups per project in `.ai_context/synonyms.json`: `{"groups": [["wpnlot", "在製批"]], "disable": ["sp"]}`. This helps abbreviations and Chinese/English terms, not free paraphrase.

The index also mines its own dictionary: Chinese bigrams in the comments right above a symbol are paired with the sub-words of its name (`/// 收帳` above `ReceiveLotAsync` gives 收帳 = receive). A pair is kept when at least 5 symbols support it, it covers at least 15% of the symbols commented with the bigram (smoothed) and the word is at least 15x likelier there than overall. On a 30k-chunk C# codebase this yields about 350 pairs (收帳 -> receive, 拆批 -> divide, 油墨 -> ink, 底片 -> film) and raised MRR on paraphrased queries from 0.57 to 0.64.

## Index health

`status` (also the MCP tool `codebase_index_status`) reports whether the index can be trusted without refreshing it: `fresh`, `stale` (files changed or git HEAD moved since the last update), `degraded` (failed files or an inconsistent database) or `not_indexed`. A file that fails to parse is removed from the index, listed with its error, and retried on every update, so search never returns stale lines for it. `index --force` rebuilds everything. Status also shows the running version and warns when the package's code changed on disk after the process started (restart the MCP server, i.e. open a new Claude Code session, to run it).

Speed (2.3k files, 30k chunks, hashing embedder): full build about 36 s into an empty database, a branch switch that rewrites 400 files about 10 s, a no-op check about 1 s. Each file is written inside a savepoint (atomic), and commits are batched every 500 files.

## Benchmark

`python benchmarks/run_benchmark.py` measures Recall@5, Recall@10 and MRR on fixed queries. See [`benchmarks/README.md`](benchmarks/README.md).

## Tests

```bash
python -m pytest -q
```

<!-- mcp-name: io.github.sfy257000/local-codebase-indexer -->
