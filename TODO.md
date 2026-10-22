# TODO

Priorities come from the benchmark (`benchmarks/run_benchmark.py`, baseline in `benchmarks/baseline.json`) and the measurements below, not from guesses.

## Next

0. **Publish to the MCP registry** (not done yet). PyPI 0.3.0 is out and carries the `mcp-name` marker; `server.json` is ready. Run `mcp-publisher login github` then `mcp-publisher publish` from the repo folder, then verify with `curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=local-codebase-indexer"`. Repeat after each release (bump both versions in `server.json`).
1. **Check a real embedding model on your own machine** (this environment cannot reach Hugging Face, so the ONNX path is only tested with a tiny generated model). Export a model such as `all-MiniLM-L6-v2` (mean pooling) or `bge-small-en-v1.5` (`INDEXER_ONNX_POOLING=cls`, `INDEXER_ONNX_QUERY_PREFIX="Represent this sentence for searching relevant passages: "`), set `INDEXER_ONNX_MODEL`, and run `python benchmarks/run_benchmark.py --compare benchmarks/baseline.json`.
   Evidence: `synonym` Recall@5 is only 0.30 (hashing embedder plus the dictionary, which cannot cover free paraphrase). Whether a real model fixes this is not measured.
2. **Resolve `FROM a JOIN b ON ..., c`** (a comma list after a JOIN loses `c`), non-sink SQL proc-name literals (`Run("usp_X")`), and EF query tracing (only the table mapping is recorded now).
3. **C# call graph gaps**: overload resolution by argument type, delegates / lambdas / extension methods, call chains longer than one level. Without Roslyn these stay approximate; edges carry `confirmed` / `probable` / `candidate`.

## Deferred (not worth doing yet)

| Item | Why deferred | Revisit when |
|---|---|---|
| ANN index | Exact search is not the bottleneck. Measured (Python stdlib copies, linear growth): 17k chunks cold CLI search 0.35 s / warm 0.13 s / index 10 s; 52k chunks 0.75 s / 0.36 s / 28 s; 104k chunks 1.09 s / 0.73 s / 54 s, 372 MB RSS, 729 MB database. At 104k chunks the cosine product plus top-k takes about 12 ms; loading the vectors out of SQLite (fetch 0.31 s, join 0.12 s) is about half of a warm query | Exact cosine itself becomes slow, which is far beyond 1M chunks |
| Vector matrix cache | Cheap and measurable: it removes about half of a warm query at 104k chunks (about 0.8 s -> 0.3 s). Not needed below ~50k chunks | A real project passes ~50k chunks. Keep the matrix in the MCP server process, and/or store it as a memory-mapped `.npy` next to the database so the CLI benefits too (invalidate on any index change) |
| `codebase_trace_execution` | Needs reliable call / data-flow relations; today's usage edges are name matches | Call graph gaps (item 3) are narrowed; today edges carry a confidence level |
| `codebase_impact_analysis` | Same dependency; must separate confirmed from guessed edges | Call graph gaps (item 3) are narrowed; today edges carry a confidence level |
| `codebase_architecture_overview` | Low value for the intended use cases | A concrete need appears |
| `codebase_find_references` (upgrade) | `relations` already returns name-matched references | Done in part: `relations` already separates confirmed / probable / candidate callers |

## Done

- SQL / data-access relations: `data_access` table and tool (`data-access` CLI, `codebase_get_data_access` MCP); T-SQL read / write / exec, C# SQL strings and Dapper / SqlCommand / EF sinks, EF table mappings, indirect writers through procedures, dynamic SQL reported as `uncertain`; search routes "who writes / reads / calls X" to it. Benchmark `sql_dataflow` Recall@5 0.72 -> 1.00, new `data_access` queries 0.67 -> 1.00
- C# symbol-level calls: using / field / constructor-injection / local type inference, interface dispatch, confidence levels. `relations Save` no longer shares one reference list across four unrelated methods
- Domain-term query expansion (`local_codebase_indexer/synonyms.py`, extendable with `.ai_context/synonyms.json`); `abbreviation` Recall@5 0.67 -> 1.00, `cross_lingual` 0.50 -> 1.00 (small, author-written sets: optimistic). ONNX pooling / query-prefix options
- Index status tool, failure log (failed files are removed from the index and retried), git commit metadata, consistency checks
- Search quality benchmark with a lint for leaky synonym queries
- Chinese text indexed as character bigrams in full-text search
