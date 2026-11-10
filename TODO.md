# TODO

Priorities come from the benchmark (`benchmarks/run_benchmark.py`, baseline in `benchmarks/baseline.json`) and the measurements below, not from guesses.

## Next

0. **MCP Registry discovery**: the 0.8.0 release workflow publishes `server.json` after PyPI succeeds, using GitHub OIDC. Verify the live entry after each release; a local manifest alone does not establish publication. See [discovery instructions](docs/discovery.md).
1. **Remaining data-flow gaps**: non-sink SQL proc-name literals (`Run("usp_X")`), EF `this.ToTable(..)` inside `EntityTypeConfiguration<T>` (entity not taken from the base class yet), and EF updates through tracked entities when the entity was loaded in another method than the one calling `SaveChanges` (only same-method read + `SaveChanges` is listed, as uncertain).
2. **C# call graph gaps**: overload resolution by argument type, delegates / lambdas / extension methods, call chains longer than one level. Without Roslyn these stay approximate; edges carry `confirmed` / `probable` / `candidate`.

## Deferred (not worth doing yet)

| Item | Why deferred | Revisit when |
|---|---|---|
| Real embedding model as default | Measured 0.4.0 on a real 30k-chunk C# codebase with Chinese comments, 12 paraphrased queries (6 zh, 6 en): hashing MRR 0.48 / hit@5 6, `all-MiniLM-L6-v2` 0.29 / 6, `paraphrase-multilingual-MiniLM-L12-v2` (int8) 0.41 / 6; full index 128 s vs 35 / 43 min. Benchmark: MiniLM +0.08-0.13 MRR, multilingual -0.01..+0.03. Domain jargon ("split a lot into two") was missed by all three | A model trained on code + Chinese, or embedding becomes incremental enough that branch switches stay under a minute |
| Content-hash cache of parse results across branch switches | Measured: a switch that rewrites 400 files re-indexes in about 10 s; parsing is about 40% of that, so a cache would save a few seconds at the cost of a large table | Branch switches routinely rewrite thousands of files |
| ANN index | Exact search is not the bottleneck. Measured (Python stdlib copies, linear growth): 17k chunks cold CLI search 0.35 s / warm 0.13 s / index 10 s; 52k chunks 0.75 s / 0.36 s / 28 s; 104k chunks 1.09 s / 0.73 s / 54 s, 372 MB RSS, 729 MB database. At 104k chunks the cosine product plus top-k takes about 12 ms; loading the vectors out of SQLite (fetch 0.31 s, join 0.12 s) is about half of a warm query | Exact cosine itself becomes slow, which is far beyond 1M chunks |
| Vector matrix cache | Cheap and measurable: it removes about half of a warm query at 104k chunks (about 0.8 s -> 0.3 s). Not needed below ~50k chunks | A real project passes ~50k chunks. Keep the matrix in the MCP server process, and/or store it as a memory-mapped `.npy` next to the database so the CLI benefits too (invalidate on any index change) |
| `codebase_trace_execution` | Needs reliable call / data-flow relations; today's usage edges are name matches | Call graph gaps (item 3) are narrowed; today edges carry a confidence level |
| `codebase_architecture_overview` | Low value for the intended use cases | A concrete need appears |
| `codebase_find_references` (upgrade) | `relations` already returns name-matched references | Done in part: `relations` already separates confirmed / probable / candidate callers |

## Done

- 0.9.0: SQL alias/hint/TOP and quoted-name fixes; cursor/TRIM false-positive removal; nested comments and uncertain OPENQUERY analysis; comma sources after JOIN; BOM-aware Unicode source/context handling; constant SQL method attribution; VB6 SQL analysis; separate DDL/schema impact; explicit ignore overrides; modular navigation/context/rendering; lint/mypy CI and Windows Python 3.10/3.12/3.13.

- 0.7.0: revision-bound search cursors; historical target snapshots; model content identities; process coordination and crash recovery; focus-preserving context; synonym hot reload; retrieval explanations; project evaluation runner; automatic validated Release assets. Representative user-authored evaluation questions and broader semantic retrieval remain ongoing work.

- 0.6.0: complete before/after impact analysis with cached historical indexes; resilient MCP input validation; nested ignore negations; root validation; doctor diagnostics; bounded and filtered search; confidence-filtered data-access pagination with no hidden full-output caps; general paraphrases; mandatory ONNX dependencies in CI. See CHANGELOG.md for validation and limitations.

- 0.5.0: a long-lived MCP server re-checks the index (was: once per process); full build 155 s -> 36 s on 2.3k files (C# usings no longer scanned as Python modules, per-file savepoints with batched commits); Chinese comment <-> identifier dictionary mined per project (iWAP paraphrase MRR 0.57 -> 0.64); data-access summary by default (`detail` / `--full`), related files only on request; EF confidence from the context variable's type; NO KNOWN CALLER roots; `impact` tool for a git diff; status shows the running version and warns when the code changed after start

- Data access: `StringBuilder` SQL, EF LINQ on `DbSet<T>` members (resolved after indexing against declared DbSets), `programs` roll-up from accessors to controller actions; `relations Type.Method`; call graph resolves `Lazy<T>.Value` and `foreach (var x in ...)` element types; programs roll-up walks from an implementation to its interface method's callers and lists Razor pages (`Url.Action`, AngularJS `ControllerPath + 'Action'`); a typed identifier's scope includes files named after it and ranks names matching the rest of the query first ("WD0096 存檔" -> SetRecord); EF `SaveChanges` on tracked entities listed as possible writes; `.ai_context/extra_roots`; `.ai_context/ignore`, minified files skipped; C# namespace imports only link files whose types are used; piped CLI output is UTF-8

- SQL / data-access relations: `data_access` table and tool (`data-access` CLI, `codebase_get_data_access` MCP); T-SQL read / write / exec, C# SQL strings and Dapper / SqlCommand / EF sinks, EF table mappings, indirect writers through procedures, dynamic SQL reported as `uncertain`; search routes "who writes / reads / calls X" to it. Benchmark `sql_dataflow` Recall@5 0.72 -> 1.00, new `data_access` queries 0.67 -> 1.00
- C# symbol-level calls: using / field / constructor-injection / local type inference, interface dispatch, confidence levels. `relations Save` no longer shares one reference list across four unrelated methods
- Domain-term query expansion (`local_codebase_indexer/synonyms.py`, extendable with `.ai_context/synonyms.json`); `abbreviation` Recall@5 0.67 -> 1.00, `cross_lingual` 0.50 -> 1.00 (small, author-written sets: optimistic). ONNX pooling / query-prefix options
- Index status tool, failure log (failed files are removed from the index and retried), git commit metadata, consistency checks
- Search quality benchmark with a lint for leaky synonym queries
- Chinese text indexed as character bigrams in full-text search
