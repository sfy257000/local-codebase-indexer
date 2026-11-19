# Search quality benchmark

Fixed queries with known answers, so a change to chunking, embeddings or ranking can be judged by numbers.

```bash
python benchmarks/run_benchmark.py                       # all suites
python benchmarks/run_benchmark.py --misses              # also list queries whose answer is not in the top 5
python benchmarks/run_benchmark.py --compare benchmarks/baseline.json   # show deltas against the saved baseline
python benchmarks/run_benchmark.py --save benchmarks/baseline.json      # replace the baseline after an accepted change
```

## Suites

| Suite | Corpus | Purpose |
|---|---|---|
| `synthetic` | `corpus/`: 24 small C#, SQL, VB6, TypeScript, Python and config files modelled on an MES code base | Controlled vocabulary tests: exact names, synonyms, abbreviations, Chinese, same-name methods, cross-file, SQL data flow |
| `synthetic-noisy` | the same corpus buried among ~2,600 unrelated Python chunks | The same queries, but the answers must compete with distractors, closer to a real project |
| `stdlib` | email, json, http, asyncio, unittest from the Python standard library | Real code and queries not written around the corpus (depends on the Python version) |

Expected answers are `file` plus an optional `symbol` suffix. A result is relevant when both match.

- **Recall@k** = expected answers found in the top k / expected answers.
- **MRR** = mean of 1 / rank of the first relevant result.
- **tokens returned / whole files** estimates (characters / 4) what the default 5 results cost compared with reading the files they point at. It is only meaningful on the larger corpora; the synthetic files are tiny.

## Caveats

- The synthetic corpus, its queries and the synonym dictionary were written by the same author (the `data_access` queries were written before the routing existed, to limit tuning), so absolute numbers are optimistic. `synonym` (free paraphrase) is still 0.30: a dictionary cannot fix it.
- The synthetic corpus and its queries were written by the same author, so absolute numbers are optimistic. Use them to compare versions, not to claim a quality level.
- `synonym` and `cross_lingual` queries must share no content word with their answers. The runner lints this (`LINT:` lines) and a test fails if a query leaks.
- Some `stdlib` answers are debatable (for example `email.headerregistry.Address` is also a reasonable answer for "format an address"), so treat that suite as a relative measure.

## History

- Chinese queries in the noisy corpus: Recall@5 0.50 -> 1.00 after Chinese runs were indexed as character bigrams (SQLite FTS5 keeps a whole run of Chinese characters as one token).

## Reproducible comparison with ripgrep + BM25

The optional `compare_tools.py` runner uses the same frozen questions/corpora and an actual ripgrep candidate search with generic tokenization and BM25 ranking. Install `rg` separately; it is not a product dependency.

```bash
python -m pip install -e ".[dev]"
python benchmarks/compare_tools.py --repeats 3 --navigation \
  --save /tmp/tool-comparison.json --details /tmp/tool-comparison-queries.jsonl
```

Use output paths suitable for your OS. Add `--suite synthetic` for a smaller run. The full run executes 42 synthetic + the same 42 noisy + 12 stdlib questions (54 unique, 96 executions). One warm-up and three measured repetitions per method are the default. `--navigation` also runs a five-evidence-file task through the real CLI, with fresh-index and warm timings. CI runs the synthetic/navigation subset and uploads raw outputs as an artifact.

Unlike the existing symbol-aware benchmark, this comparison scores **file-level** Recall@5 and MRR@5 for both methods, ignoring gold symbol suffixes. The indexer retains its default 5 chunks; the baseline returns 5 files. Output reductions use **exact Unicode characters** against whole reads of the **same indexer-selected files**, not model tokens or Grep output. In-process indexer timings and subprocess baseline timings have different boundaries; only the separate navigation task measures both CLI processes. This is not a Claude agent, Serena, or Claude Context head-to-head test.

[Recorded summary](tool-comparison.json) · [96 per-query records](tool-comparison-queries.jsonl) · [complete protocol, results, losses, and limitations](../docs/tool-comparison.md). The sample shows +10.7 / +9.5 percentage points of file recall on MES fixtures, and −20.8 points on stdlib. Corpus/source/dataset hashes and runtime versions are recorded. Keep gold answers fixed when comparing changes; do not rerun only until a favorable timing appears.

## v1 readiness evidence

- `real-projects.json`: 100 frozen source-reviewed development questions, 34 Dapper / 34 PetaPoco / 32 SimpleCRUD, each on a pinned real public source commit. Run `python benchmarks/real_projects.py --baseline-source /path/to/0.9.1 --save /tmp/real.json`. Each variant uses a fresh identical archive in a separate process; gold is not inferred from retrieval. Default per-project recall-drop budget is zero. MRR/timing losses remain visible.
- `real-project-results.json`: raw baseline/candidate answers and fingerprints. Overall symbol-aware Recall@5 remains 0.48; this does not establish SQL impact precision or independent user accuracy. The development set informed the diversity change; it is not held out.
- `performance.py`: generated 10k/50k/100k-function corpus, exact chunk counts, cache on/off isolated query workers, p50/p95, peak RSS, DB size, one/ten-file updates and a real Git switch. Raw records are in `performance-results.json`. Ordinary CI runs 1k; manual full-benchmarks runs all sizes. Five-run p95 is only a sample statistic.
- `client-compatibility.json` / `codex-tool-events.json`: native client transport evidence with a local deterministic model, not hosted reasoning or model-cost measurements. See limitations concerning the prior Codex run and automatic-review rejection in the [full protocol](../docs/v1-readiness.md).
- `v1-tool-comparison.json` / JSONL: separate candidate capture of the old lexical comparison; original 0.9.1 records are preserved.

Maintainer-supplied Python acceptance is separate: `maintainer-project.json` freezes 12 navigation questions at the supplied repository commit; `maintainer-project-results.json` records baseline/hybrid/lexical results. Reproduce with `python benchmarks/real_projects.py --manifest benchmarks/maintainer-project.json --baseline-source /path/to/0.9.1 --lexical --save /tmp/maintainer.json`. `maintainer-acceptance.json` records a source-only disposable-copy smoke test of index/context/symbol navigation, stale detection and one-file update/restore. No upstream application code was run.
