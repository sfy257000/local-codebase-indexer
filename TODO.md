# TODO

Priorities come from the benchmark (`benchmarks/run_benchmark.py`, baseline in `benchmarks/baseline.json`) and the measurements below, not from guesses.

## Next

1. **SQL / data-access relations.** Parse EXEC / INSERT / UPDATE / DELETE / FROM / JOIN in SQL and in C# SQL strings and Dapper calls, to answer "what writes / reads table X".
   Evidence: `sql_dataflow` queries score Recall@5 0.72 and MRR 0.46; "what writes to MES_Lot" does not return the writing stored procedure in its top 6. Search returns chunks and cannot enumerate every writer. Mark dynamic SQL as uncertain.
2. **C# symbol-level relations.** Resolve calls through using / field types / constructor injection, with a confidence level.
   Evidence: `relations Save` returns the same 4 references for 4 different `Save` methods (about 25% precision even in a 24-file corpus).
3. **Query expansion for abbreviations** (SP -> stored procedure / usp_, 預存程序). Cheap, do together with 1.
   Evidence: `abbreviation` MRR 0.25.
4. **Check a real embedding model on your own machine** (this environment cannot download one). Export `all-MiniLM-L6-v2` to ONNX, set `INDEXER_ONNX_MODEL`, run the benchmark with `--compare`.
   Evidence: `synonym` Recall@5 is 0.00 with the hashing embedder, and `cross_lingual` (Chinese query, English-only code) is 0.00 in the noisy corpus. Whether a real model fixes this is not measured yet.

## Deferred (not worth doing yet)

| Item | Why deferred | Revisit when |
|---|---|---|
| ANN index | Exact search is not the bottleneck. Measured (Python stdlib copies, linear growth): 17k chunks cold CLI search 0.35 s / warm 0.13 s / index 10 s; 52k chunks 0.75 s / 0.36 s / 28 s; 104k chunks 1.09 s / 0.73 s / 54 s, 372 MB RSS, 729 MB database. At 104k chunks the cosine product plus top-k takes about 12 ms; loading the vectors out of SQLite (fetch 0.31 s, join 0.12 s) is about half of a warm query | Exact cosine itself becomes slow, which is far beyond 1M chunks |
| Vector matrix cache | Cheap and measurable: it removes about half of a warm query at 104k chunks (about 0.8 s -> 0.3 s). Not needed below ~50k chunks | A real project passes ~50k chunks. Keep the matrix in the MCP server process, and/or store it as a memory-mapped `.npy` next to the database so the CLI benefits too (invalidate on any index change) |
| `codebase_trace_execution` | Needs reliable call / data-flow relations; today's usage edges are name matches | Items 1 and 2 are done |
| `codebase_impact_analysis` | Same dependency; must separate confirmed from guessed edges | Items 1 and 2 are done |
| `codebase_architecture_overview` | Low value for the intended use cases | A concrete need appears |
| `codebase_find_references` (upgrade) | `relations` already returns name-matched references | Item 2 can tell confirmed from candidate references |

## Done

- Index status tool, failure log (failed files are removed from the index and retried), git commit metadata, consistency checks
- Search quality benchmark with a lint for leaky synonym queries
- Chinese text indexed as character bigrams in full-text search
