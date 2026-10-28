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

## Usage (CLI)

```bash
local-codebase-indexer --root <project> index                       # build / incrementally refresh the index (--force rebuilds all)
local-codebase-indexer search "database mapping for LotNo" --limit 5
local-codebase-indexer relations IExistOpenHandleService --direction both   # upstream | downstream | both
local-codebase-indexer context Services/LotService.cs --lines 120 --padding 15   # alias: slice
local-codebase-indexer data-access MES_Lot                           # who reads / writes / executes a table or procedure; what a procedure touches
local-codebase-indexer stats
local-codebase-indexer status                                       # is the index fresh? failed files, git commit, consistency checks
```

- Without `--root` the current directory is used; `INDEXER_ROOT` also works.
- Put `--json` before the subcommand to get JSON output.
- `search`, `relations` and `context` run an incremental refresh first (mtime check, then md5 only when mtime changed).
- The tool names from the spec also work as subcommands: `codebase_semantic_search`, `codebase_get_symbol_relations`, `codebase_get_optimized_context` (options `--query`, `--symbol_name --relation_direction`, `--file_path --focus_lines --padding_lines`).

## Usage (Claude Code / MCP)

```bash
claude mcp add local-indexer -- local-codebase-indexer --root /path/to/project serve
# without installing: claude mcp add local-indexer -- uvx local-codebase-indexer --root /path/to/project serve
```

`serve` speaks JSON-RPC (MCP) over stdio and exposes five tools: `codebase_semantic_search`, `codebase_get_symbol_relations`, `codebase_get_optimized_context`, `codebase_get_data_access`, `codebase_index_status`.

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

`data-access <table-or-procedure>` answers "what writes / reads / executes X". It is built from T-SQL (INSERT / UPDATE / DELETE / MERGE / TRUNCATE / SELECT INTO / FROM / JOIN / EXEC, with alias resolution; CTEs, temp tables and system objects excluded), from C# SQL strings (literals, `+`, interpolation, `string.Format`, consts, locals, `StringBuilder.Append` / `AppendLine` / `AppendFormat`) passed to Dapper, `SqlCommand` or EF `FromSql`, from stored-procedure names used with `CommandType.StoredProcedure`, from EF LINQ on `DbSet<T>` members (`ctx.Orders.Where(..)` / `.Add(..)` / `.Remove(..)`, `from o in ctx.Orders`; `probable`, since only the member name is matched) and from EF `ToTable` / `[Table]` mappings. A procedure's callers are indirect writers of the tables it writes (followed up to 4 hops). `programs` (USED BY PROGRAMS) follows confirmed / probable C# calls upward from those accessors to `*Controller` methods (up to 8 hops) (including from an implementation to the callers of the interface method it implements, for `foreach (var step in steps) step.Run()` pipelines) and groups them per controller file, with the Razor pages (`.cshtml`) whose `Url.Action("Action"[, "Controller"])` or AngularJS `ControllerPath + 'Action'` (controller from `$scope.Controller = 'X'`) targets those actions, so a table maps to the screens that touch it. A method that reads a DbSet and calls `SaveChanges` without an explicit `Add` / `Remove` may update tracked entities; it is listed as a possible (`uncertain`) writer. Each entry has a confidence; **dynamic SQL (`EXEC(@sql)`, `sp_executesql`, SQL built from unknown values) cannot be resolved and is listed separately as `uncertain`**, never mixed into the confirmed lists. Known gaps: `FROM a JOIN b ON ..., c` loses `c`, and a stored-procedure name passed to a non-database method is not seen.

Stored procedures that live only in the database can be exported to a folder outside the repo and listed in `.ai_context/extra_roots` (one folder per line, absolute or relative to the root); their files are indexed under their absolute paths.

Natural-language search routes questions such as "who modifies MES_ScrapRecord" or "哪些程式會寫入 MES_Lot" to this index first.

## Query expansion

Queries are expanded with a small synonym dictionary (`SP` / `預存程序` / `stored procedure` / `usp`, table, lot, scrap, ...; `local_codebase_indexer/synonyms.py`). Add or disable groups per project in `.ai_context/synonyms.json`: `{"groups": [["wpnlot", "在製批"]], "disable": ["sp"]}`. This helps abbreviations and Chinese/English terms, not free paraphrase.

## Index health

`status` (also the MCP tool `codebase_index_status`) reports whether the index can be trusted without refreshing it: `fresh`, `stale` (files changed or git HEAD moved since the last update), `degraded` (failed files or an inconsistent database) or `not_indexed`. A file that fails to parse is removed from the index, listed with its error, and retried on every update, so search never returns stale lines for it. `index --force` rebuilds everything.

## Benchmark

`python benchmarks/run_benchmark.py` measures Recall@5, Recall@10 and MRR on fixed queries. See [`benchmarks/README.md`](benchmarks/README.md).

## Tests

```bash
python -m pytest -q
```

<!-- mcp-name: io.github.sfy257000/local-codebase-indexer -->
