# Local Codebase Indexer

**English** | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

A local codebase indexer and context provider for AI coding assistants (Claude Code via MCP, GitHub Copilot / Codex via CLI). It helps them navigate large legacy codebases with far fewer tokens. Everything is stored in `.ai_context/indexer.db` inside your project, with no cloud services involved. The design follows [`index.md`](index.md).

## Install

```bash
pip install -r requirements.txt   # Python 3.10+
```

## Usage (CLI)

```bash
bin/indexer --root <project> index                       # build / incrementally refresh the index (--force rebuilds all)
bin/indexer search "database mapping for LotNo" --limit 5
bin/indexer relations IExistOpenHandleService --direction both   # upstream | downstream | both
bin/indexer context Services/LotService.cs --lines 120 --padding 15   # alias: slice
bin/indexer data-access MES_Lot                           # who reads / writes / executes a table or procedure; what a procedure touches
bin/indexer stats
bin/indexer status                                       # is the index fresh? failed files, git commit, consistency checks
```

- Without `--root` the current directory is used; `INDEXER_ROOT` also works.
- Put `--json` before the subcommand to get JSON output.
- `search`, `relations` and `context` run an incremental refresh first (mtime check, then md5 only when mtime changed).
- The tool names from the spec also work as subcommands: `codebase_semantic_search`, `codebase_get_symbol_relations`, `codebase_get_optimized_context` (options `--query`, `--symbol_name --relation_direction`, `--file_path --focus_lines --padding_lines`).

## Usage (Claude Code / MCP)

```bash
claude mcp add local-indexer -- python /path/to/local-codebase-indexer/bin/indexer --root /path/to/project serve
```

`serve` speaks JSON-RPC (MCP) over stdio and exposes five tools: `codebase_semantic_search`, `codebase_get_symbol_relations`, `codebase_get_optimized_context`, `codebase_get_data_access`, `codebase_index_status`.

## Three-layer index

| Layer | Implementation |
|---|---|
| Syntactic | tree-sitter for C#, Python, JavaScript, TypeScript/TSX; regex for SQL (PROCEDURE / VIEW / FUNCTION / TABLE); other text files (VB6 `.frm`, `.config`, `.md`, ...) are split into windowed chunks |
| Topology | `using` / `import` resolved to file dependencies, inheritance (base lists) and type usage (`invocation`), stored in the `dependencies` table |
| Semantic | 384-dim chunk embeddings in `vss_chunks`; queries fuse FTS5 keyword, symbol-name and vector rankings with Reciprocal Rank Fusion, then expand to the dependency neighbours of the top 3 files |

Encodings: UTF-8 first, then Big5, then cp1252. Files over 5MB are skipped. Root and nested `.gitignore` files are respected, and `.git`, `bin`, `obj`, `node_modules` and similar directories are always skipped.

## Differences from the spec

- **Vector storage**: `sqlite-vss` needs a native extension and is no longer maintained, so `vss_chunks` stores float32 BLOBs and cosine similarity is computed with numpy. No extension to install.
- **Embedding model**: the default is a built-in hashing embedder (identifier sub-words, bigrams, CJK character bigrams). It is fast and reproducible but **does not understand synonyms**. For real transformer embeddings, export `all-MiniLM-L6-v2` or `bge-small-en-v1.5` to ONNX, install `onnxruntime` and `tokenizers`, and set `INDEXER_ONNX_MODEL=<folder with model.onnx and tokenizer.json>` (the loading, padding, pooling and re-index logic is covered by a test with a tiny synthetic model; retrieval quality with a real model has not been measured, so run `python benchmarks/run_benchmark.py --compare benchmarks/baseline.json` with the variable set to check it; mean pooling suits MiniLM, bge models recommend CLS pooling). Switching embedders triggers a full re-index.
- **Call relations** for C# are resolved with limited type inference (using / namespaces, field and constructor-injected types, locals, interface to implementation), and every edge carries a confidence: `confirmed` (the receiver type is known), `probable` (reached through an interface or a single candidate) or `candidate` (name match only). There is no Roslyn: no overload resolution by argument type, no delegates / lambdas / extension methods, and call chains are followed one level. Inheritance edges come from the AST and are reliable.
- Extra schema additions: `symbols.qualified_name`, `chunk_metadata.symbol_id`, and the `raw_refs`, `chunks_fts` and `meta` tables.

## Data access (SQL)

`data-access <table-or-procedure>` answers "what writes / reads / executes X". It is built from T-SQL (INSERT / UPDATE / DELETE / MERGE / TRUNCATE / SELECT INTO / FROM / JOIN / EXEC, with alias resolution; CTEs, temp tables and system objects excluded), from C# SQL strings (literals, `+`, interpolation, `string.Format`, consts, locals) passed to Dapper, `SqlCommand` or EF `FromSql`, from stored-procedure names used with `CommandType.StoredProcedure`, and from EF `ToTable` / `[Table]` mappings. A procedure's callers are indirect writers of the tables it writes (followed up to 4 hops). Each entry has a confidence; **dynamic SQL (`EXEC(@sql)`, `sp_executesql`, SQL built from unknown values) cannot be resolved and is listed separately as `uncertain`**, never mixed into the confirmed lists. Known gaps: `FROM a JOIN b ON ..., c` loses `c`, and a stored-procedure name passed to a non-database method is not seen.

Natural-language search routes questions such as "who modifies MES_ScrapRecord" or "哪些程式會寫入 MES_Lot" to this index first.

## Query expansion

Queries are expanded with a small synonym dictionary (`SP` / `預存程序` / `stored procedure` / `usp`, table, lot, scrap, ...; `indexer/synonyms.py`). Add or disable groups per project in `.ai_context/synonyms.json`: `{"groups": [["wpnlot", "在製批"]], "disable": ["sp"]}`. This helps abbreviations and Chinese/English terms, not free paraphrase.

## Index health

`status` (also the MCP tool `codebase_index_status`) reports whether the index can be trusted without refreshing it: `fresh`, `stale` (files changed or git HEAD moved since the last update), `degraded` (failed files or an inconsistent database) or `not_indexed`. A file that fails to parse is removed from the index, listed with its error, and retried on every update, so search never returns stale lines for it. `index --force` rebuilds everything.

## Benchmark

`python benchmarks/run_benchmark.py` measures Recall@5, Recall@10 and MRR on fixed queries. See [`benchmarks/README.md`](benchmarks/README.md).

## Tests

```bash
python -m pytest -q
```
