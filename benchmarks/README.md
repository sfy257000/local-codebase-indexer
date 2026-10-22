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
