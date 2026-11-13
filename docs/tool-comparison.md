# Choosing a navigation tool

Local Codebase Indexer is useful when the question crosses SQL Server definitions, C# calls, controller actions, and Razor links. Its advantage is a focused, reusable relation report with source locations and confidence labels. It complements the agent's text search and an IDE's semantic tools.

## Measured differences

Recorded on 2026-11-13 with Python 3.12.14, SQLite 3.53.1, ripgrep 15.2.0, Linux x86_64, default `HashingEmbedder`, pre-0.9.1 source (then labeled package version 0.9.0) and index format 10. The recorded version and fingerprint are preserved rather than relabeled after the release version bump. [Summary and source/corpus fingerprints](../benchmarks/tool-comparison.json) and [all query-level records](../benchmarks/tool-comparison-queries.jsonl) are committed alongside the runner. Timings describe this machine, not a performance guarantee.

| Retrieval suite | Questions / files | `rg + BM25` file Recall@5 | Indexer file Recall@5 | Difference | File MRR@5: baseline → indexer |
|---|---:|---:|---:|---:|---:|
| MES-style synthetic | 42 / 24 | 81.0% | 91.7% | **+10.7 pp** | 0.669 → 0.833 |
| Same MES questions + distractors | 42 / 109 | 81.0% | 90.5% | **+9.5 pp** | 0.696 → 0.813 |
| Python stdlib | 12 / 85 | 100.0% | 79.2% | **−20.8 pp** | 0.819 → 0.722 |

This supports a narrower claim: the default indexer retrieves more known relevant files on these MES fixtures. It loses to this lexical baseline on these stdlib questions. It does not establish better general code search, independent production accuracy, or an advantage over a Claude agent, Serena, or Claude Context.

### Retrieval protocol

- The existing `queries_synthetic.json` (42 questions) and `queries_stdlib.json` (12 questions), gold answers, and corpus are unchanged. The noisy suite reuses the same 42 questions among Python distractors: **54 unique questions, 96 query executions**, not 96 independent questions. The synthetic corpus, questions, and product domain synonyms were authored by this project; absolute results are optimistic. The stdlib corpus is real Python source, but its 12 questions/gold answers are also project-authored, not an independent benchmark.
- Both methods receive the same indexed file set and original question. The baseline splits identifiers into whole/CamelCase/snake_case terms and Chinese bigrams, removes generic stop words, invokes real `rg --no-config --files-with-matches --text --ignore-case` with OR terms, then ranks candidate files using standard BM25 (`k1=1.2`, `b=0.75`). Ranking precomputes token counts and document lengths. No domain aliases, gold-derived terms, manual query rewriting, or best-of-several baselines are added. Native ripgrep does not rank: **`rg + BM25` is our documented wrapper**, not Claude Grep's implementation.
- The indexer uses its normal 5-result-chunk budget, including routing, domain synonyms, and default hashing. The baseline returns 5 files. Both are scored at **file** level: repeated paths are deduplicated; symbol suffixes in gold are ignored for both. Chunk results can therefore cover fewer than 5 unique files. These are file Recall@5 / MRR@5, not the symbol-aware metrics of `run_benchmark.py` and not measured answer correctness.
- For each question: one warm-up per method, then three measured calls per method with alternating order. Query-level files, terms, gold, scores, durations, and output character counts are saved in JSONL. Index construction and BM25 setup are reported separately. Command failures fail the experiment rather than silently count as no matches.

### Context size has a separate denominator

| Suite | Rendered indexer result characters | Whole reads of the same selected files | Reduction |
|---|---:|---:|---:|
| MES-style synthetic | 83,044 | 81,175 | **−2.3%** (more output) |
| MES + distractors | 86,339 | 1,700,971 | **94.9%** |
| Python stdlib | 31,984 | 1,039,878 | **96.9%** |

Counts are exact Unicode characters of `render_search(response)` versus full decoded source plus path headers for the same unique indexer-selected files, summed over all questions. Reduction is `1 − total_output / total_full_reads`, not the average of per-question ratios. This compares chunked output with whole-file reading. It does **not** compare output size against `rg -l`, contextual Grep snippets, Serena, or Claude Context; it does **not** count model tokens, verify sufficient answering context, or measure cost savings. Tiny files can produce more output because result headers add overhead.

### Evidence coverage and actual CLI cost

The separate [navigation task](../benchmarks/navigation_task.json) requires five evidence files for the bundled synthetic order-approval scenario: table definition, SQL writer, C# accessor, controller, and Razor page. Intermediate services are not required by this report task; this is not an inventory of all affected files.

| Single invocation | Required evidence files found | Coverage | Warm CLI median |
|---|---:|---:|---:|
| Literal table-name `rg -l -i -w -F` | 3/5 | 60% | 8.3 ms |
| `data-access Sales_OrderHeader --json` | 5/5 | 100% | 244.5 ms |

Coverage improves by **40 percentage points** for this specified task; the extra controller/page path is `probable` due to interface dispatch. CLI medians include process startup and three runs; the first indexer call, including indexing, takes **266.1 ms**. The richer report is slower than one literal search. A multi-step agent or an expert using several Grep searches can investigate further; neither was timed or scored here.

Retrieval-suite backend timings are deliberately separate: indexer warm in-process method medians are 3.1 / 94.5 / 74.8 ms (synthetic / noisy / stdlib), versus 5.7 / 11.8 / 11.9 ms for `rg` subprocess plus BM25 ranking. Their boundaries differ: indexer timings exclude process startup and MCP transport. They cannot be presented as CLI speedups. Initial indexes take 62 / 1,478 / 1,390 ms; baseline setup takes 4 / 316 / 301 ms. See raw records for every repetition.

### Run the recorded protocol

Use the repository checkout, Python 3.12 for the closest corpus match, and install ripgrep on PATH. Ripgrep is only needed by this optional benchmark, not by the product.

```bash
python -m pip install -e ".[dev]"
python benchmarks/compare_tools.py --repeats 3 --navigation \
  --save /tmp/tool-comparison.json --details /tmp/tool-comparison-queries.jsonl
```

Use paths suitable for your OS. `--suite synthetic --repeats 1 --navigation` is the CI smoke run; its JSON/JSONL are uploaded as a `tool-comparison` artifact. CI checks that the experiment runs; it does not rewrite the committed sample, enforce these machine-specific timings, or replace the existing quality regression benchmark. Different Python versions change stdlib source and can change results; corpus hashes are included to detect that difference.

## Claude with Grep / ripgrep

Use text search first for a known identifier, an exact string, a regex, or checking whether a reference exists in the files being searched. It has no index to build. A Claude agent can follow the matches, read more files, and infer the same chain manually.

For supported patterns, this indexer joins SQL access, procedure execution, C# calls, and page links into one `data-access` result. That makes repeated maintenance questions and machine-readable follow-up easier. The result is approximate static analysis; a missing edge is not proof there is no dependency. Grep remains useful for checking the underlying text and patterns the analyzer does not support.

No end-to-end Claude agent benchmark was performed. A comparison of one literal search and one relation report does not measure Claude's reasoning ability, human investigation time, model tokens, or cost.

## Serena and compiler / IDE tools

[Serena](https://github.com/oraios/serena) offers symbol navigation, references, and editing/refactoring through language-server or JetBrains backends. These backends are a strong fit for language-aware navigation and supported renames. Availability and precision depend on the selected backend and language.

Choose this project for its dedicated SQL table/procedure-to-C#-to-page report, separate schema operations, and historical Git impact, with Python/SQLite and no language server to configure. Choose an appropriate compiler/IDE backend, such as Roslyn for C#, when overload resolution, semantic rename, or compilation validation matters. This project's heuristic C# call graph does not replace those guarantees. We have not benchmarked Serena's coverage of this synthetic SQL scenario, nor do we claim it cannot be extended to handle it.

## Claude Context and vector code search

[Claude Context](https://github.com/zilliztech/claude-context) documents BM25 plus dense-vector retrieval, configurable embedding providers (including Ollama), and Milvus/Zilliz configuration. It is a strong fit for retrieving code from natural-language questions when a learned embedding index is appropriate. Provider choice determines whether that setup uses local or remote services.

This project's default hashing/text/symbol index needs no embedding API key, model download, or vector-database server after installation. Its SQL relation report also distinguishes direct writes, procedure-mediated writes, schema changes, and confidence. Those are reasons to choose it for maintenance tracing; they do not establish better natural-language retrieval. The default hashing embedder often needs identifiers or domain aliases; paraphrases can miss results. Optional local ONNX embeddings are available but do not automatically improve every project.

## Reproduce the narrow comparison

Requires Python 3.10+ and ripgrep (`rg`) on PATH for the text-search step. Ripgrep is not required by the indexer itself. Use a new or empty `demo-orders` folder:

```bash
python -m pip install local-codebase-indexer
local-codebase-indexer demo --output demo-orders
rg -l -i -w Sales_OrderHeader demo-orders -g '*.cs' -g '*.sql' -g '*.cshtml'
local-codebase-indexer --root demo-orders data-access Sales_OrderHeader --full
```

On the bundled **synthetic** fixture, the literal search returns three files:

```text
Data/OrderRepository.cs
Sql/Sales_OrderHeader.sql
Sql/usp_ApproveOrder.sql
```

The relation report additionally connects `OrderRepository.Approve` through `usp_ApproveOrder` to the table, and rolls the C# service calls up to `OrdersController` actions `Approve` / `Details` and `Views/Orders/Details.cshtml`. The controller and page do not contain the literal table name. Controller reachability is **probable** because the path includes interface dispatch. The four built-in scenario checks pass.

[Recorded evidence](comparison-evidence.json) contains the exact reference files, writer chains, actions, page, and confidence labels. It was regenerated against this checkout (index format 10). The [source-by-source case study](case-study.md) explains the fixture and broader limitations. The five-file coverage score above uses a separate task manifest; the demo's four relationship checks are a different measure. Neither establishes production accuracy or an end-to-end competing-agent score.

## Upstream sources

Feature descriptions were checked on 2026-11-13 against these upstream READMEs; these are documentation comparisons, not executed head-to-head tests:

- [Serena README snapshot](https://github.com/oraios/serena/blob/3ecf6eb3f65ac26d25d7d86cd8926d1eb4f6ab3e/README.md): language-server / JetBrains backends, retrieval, and editing/refactoring tools.
- [Claude Context README snapshot](https://github.com/zilliztech/claude-context/blob/0d558ff7ac294ecb207f8264437f8ac7389459c4/README.md): hybrid search, embedding providers, and Milvus/Zilliz setup.
- [ripgrep](https://github.com/BurntSushi/ripgrep): text-search behavior. Our commands invoke `rg` directly; they do not exercise Claude's Grep integration or its multi-step agent workflow.
