# Choosing a navigation tool

Local Codebase Indexer is useful when the question crosses SQL Server definitions, C# calls, controller actions, and Razor links. Its advantage is a focused, reusable relation report with source locations and confidence labels. It complements the agent's text search and an IDE's semantic tools.

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

[Recorded evidence](comparison-evidence.json) contains the exact reference files, writer chains, actions, page, and confidence labels. It was regenerated against this checkout (index format 10). The [source-by-source case study](case-study.md) explains the fixture and broader limitations. The result demonstrates the extra relation report, not a speed, token-efficiency, production accuracy, or competing-tool superiority score.

## Upstream sources

Feature descriptions were checked on 2026-11-13 against these upstream READMEs; these are documentation comparisons, not executed head-to-head tests:

- [Serena README snapshot](https://github.com/oraios/serena/blob/3ecf6eb3f65ac26d25d7d86cd8926d1eb4f6ab3e/README.md): language-server / JetBrains backends, retrieval, and editing/refactoring tools.
- [Claude Context README snapshot](https://github.com/zilliztech/claude-context/blob/0d558ff7ac294ecb207f8264437f8ac7389459c4/README.md): hybrid search, embedding providers, and Milvus/Zilliz setup.
- [ripgrep](https://github.com/BurntSushi/ripgrep): text-search behavior. Our commands invoke `rg` directly; they do not exercise Claude's Grep integration or its multi-step agent workflow.
