# v1 readiness: implementation and evidence

These Unreleased changes extend 0.9.1. They do not relabel an existing benchmark or publish a new package version.

## Implemented scope

| Area | Behavior |
|---|---|
| SQL identity | Preserve separate linked-server/database/schema/name components, including delimited dots, escaped brackets, and explicit `USE` context. Qualified queries isolate objects; ambiguous short names return candidates instead of a combined confirmed list. |
| Evidence | Accessor-to-object `path` and action-specific program/page paths carry file locations, confidence, and source evidence. `coverage.warnings` identifies depth cutoffs and unavailable SQL definitions. |
| MCP/API | Negotiate supported protocol versions; expose structured results and output schemas alongside legacy human text. Seven committed JSON schemas and the full input/output tool manifest are checked in CI. |
| Search/scale | Soft file diversity preserves strong matches in large single-file libraries. Optional lexical mode skips vectors and SQL routing. A bounded vector cache follows index revisions and embedder identity. |
| Onboarding/reporting | Project-local Claude/Codex configuration, indexing progress on stderr, and offline HTML/SVG, Mermaid, and JSON reports. |
| Evaluation | 100 frozen source-reviewed questions over three pinned public C# projects; isolated 10k/50k/100k scaling, incremental changes, and actual Git switches; SDK and real-client transport evidence. |

## Use the additions

```bash
local-codebase-indexer --root /path/to/project init --client claude
local-codebase-indexer --root /path/to/project init --client codex --no-index
local-codebase-indexer --root /path/to/project index --progress
local-codebase-indexer --root /path/to/project data-access dbo.YourTable --sql-depth 12 --call-depth 16 --json
local-codebase-indexer --root /path/to/project search "exact project terminology" --mode lexical
local-codebase-indexer --root /path/to/project report dbo.YourTable --output .ai_context/reports/table.html
local-codebase-indexer --root /path/to/project report dbo.YourTable --format mermaid --output .ai_context/reports/table.mmd
```

`init --client generic` prints the server command/arguments without changing client configuration. `init` writes only project-local `.mcp.json` or `.codex/config.toml`, preserves unrelated settings, and rejects conflicting existing `local-indexer` settings or escaping file symlinks. It does not approve client trust prompts or change account settings. Approve the project/server in your client when prompted. Use `--no-index` to configure without building; an empty/failed first index returns exit 3 and coverage details.

Reports include source paths and excerpts, confidence, index status/revision, representative evidence paths, and the complete structured result. HTML has no script or remote resource dependency. Its SVG is limited to 250 nodes with an explicit warning; the complete JSON evidence remains available. Existing files require `--force` before replacement. Prefer `.ai_context/reports/` so generated output is excluded from indexing.

## Identity and completeness semantics

- Matching remains case-insensitive. Case-sensitive database collations require source verification; no live SQL Server connection is made.
- `USE` is respected where explicit in SQL files. No default database/schema or connection-string binding is invented for unqualified C#/SQL references.
- A short reference with one indexed qualified candidate can be `probable`; partial database/schema constraints are preserved and never replaced by a less informative qualifier. Conflicting EF entity mappings produce uncertain candidates rather than selecting the last mapping; conflicting scopes are `uncertain` under `possible`. Explicit conflicting qualifiers never become confirmed matches.
- The default bounds remain 4 SQL hops and 8 C# call hops. Override with `sql_depth` (1–32) and `call_depth` (1–64), in CLI or MCP. A cutoff is reported, rather than interpreted as no dependency.
- `complete_within_limits` only describes traversal within the indexed supported patterns. It is not runtime completeness. Missing SQL definitions, unsupported dispatch, dynamic SQL, and external source remain limitations.
- Paths are representative paths through the resolved graph, not an enumeration of every control-flow path. Visited states terminate cycles. Page evidence is attached to the specific action it targets.
- C# source owners use AST byte spans, so several methods/overloads on one line retain distinct identities and execution attribution.

Index format **11** rebuilds older parsed data automatically on the first update. Existing databases receive additive columns before the rebuild. Do not manually delete active index locks.

## Public contract

The seven MCP results and corresponding CLI JSON commands add `schema_version: 1` and `result_type`. See [committed output schemas](contracts/) and the [complete input/output tool manifest](mcp-tools.json). Success includes `structuredContent`; the original text content remains available. A missing/ambiguous SQL object is a successful query with `found: false`, not a transport error. Tool failures retain `isError`; a busy index additionally reports `error.code: index_busy` and `retryable: true`. Malformed JSON-RPC and invalid arguments retain protocol errors.

MCP data-access defaults to **5 entries per section**, with exact section totals/next offsets. Nested program paths/pages and other tools' summary collections are bounded; `truncation` records their paths and original/returned counts. Complete individual evidence chains are never cut in half. `detail=true` requests full structured nested collections, while explicit result/lookup limits still apply. CLI JSON keeps its existing full-result default unless `--limit` is supplied, and report export retains complete structured evidence.

Supported protocol versions: `2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05`. A supported requested version is echoed; an unsupported version receives the server's preferred supported version so the client can decide whether to continue.

The 1.x contract is additive: preserve existing field types/meanings, tool names, CLI aliases, confidence labels, and exit codes. Optional fields may be added. Consumers should tolerate unknown fields and inspect `found`, pagination, coverage, and index status. A breaking contract requires a schema/major-version change. Symbol IDs and search cursors are revision-bound; they are not permanent cross-index identifiers.

Search scores remain raw fused RRF scores, not probabilities; final ordering can also apply identifier priority and soft file diversity.

`search --mode lexical` uses the existing keyword/symbol index without vectors, domain expansion, or SQL routing. It searches indexed sources and does not replace complete literal `rg` checks. Cursor mode/query/filter/revision must remain unchanged. `INDEXER_VECTOR_CACHE_MB` defaults to 128; 0 disables caching. The cache is in-process, so a single CLI invocation does not benefit from previous CLI processes. Changed index revisions, embedder identities, filters, and interrupted indexing are handled explicitly.

## Real-project retrieval evaluation

[Questions and pinned repositories](../benchmarks/real-projects.json) · [baseline/candidate per-query results](../benchmarks/real-project-results.json).

The 100 English/Chinese questions were written and source-reviewed for this project before the first run. The source commits are fixed; neither questions nor gold answers were changed during ranking work. They are development evaluation questions over real code, not independently authored user tasks or a held-out benchmark. The first hard per-file diversity experiment regressed on SimpleCRUD and was replaced with a soft penalty; this evaluation therefore informed development.

| Project | Questions | 0.9.1 symbol-aware Recall@5 | Candidate |
|---|---:|---:|---:|
| Dapper | 34 | 35.3% | 35.3% |
| PetaPoco | 34 | 44.1% | 44.1% |
| SimpleCRUD | 32 | 65.6% | 65.6% |
| All questions | 100 | 48.0% | 48.0% |

Overall MRR changes from **0.3345 to 0.3283**; ranking is slightly worse by this measure. File+symbol matching is stricter than the earlier file-only tool comparison. These results expose the limits of default hashing for broad natural-language questions. They do not measure SQL impact precision/recall, independent production accuracy, model-token cost, or human investigation-time savings. Those claims require their own verified task data and observations; this work does not invent them.

CI compares each project's Recall@5 to the same frozen 0.9.1 source snapshot without allowing a recall drop by default. MRR and timings remain visible in the artifact. Absolute semantic-search accuracy remains a product limitation, rather than an implied v1 quality guarantee.

```bash
python benchmarks/real_projects.py --baseline-source /path/to/0.9.1-checkout --save /tmp/real-projects.json
python benchmarks/compare_tools.py --repeats 3 --navigation --save /tmp/comparison.json --details /tmp/queries.jsonl
```

Use output paths suitable for your OS. The runner fetches pinned public sources and analyzes them as text; it does not execute their code or database tests. Source/corpus/dataset fingerprints and raw query results are recorded. The original 0.9.1 [comparison](tool-comparison.md) is preserved; [candidate file-level measurements](../benchmarks/v1-tool-comparison.json) are a separate capture.

## Maintainer-supplied project acceptance

The maintainer supplied [island-inline-watcher](https://github.com/sfy257000/island-inline-watcher/tree/1534a8378e5bd21acb8c9e61d2d84eba2ae2c435), a Python desktop application, not a C#/SQL Server project. A separate [frozen 12-question set](../benchmarks/maintainer-project.json) was source-reviewed before its first retrieval run; it does not alter the original 100 questions. [Raw retrieval results](../benchmarks/maintainer-project-results.json) retain all misses.

Symbol-aware Recall@5 improves from **7/12 (58.3%) to 8/12 (66.7%)**, or **+8.3 percentage points**, with MRR **0.4750 → 0.4819**. Lexical mode also finds 8/12, MRR 0.4861. These are project-authored development questions, including two Traditional Chinese questions, rather than tasks independently written by the maintainer. They were not used to tune ranking.

[Source-only workflow acceptance](../benchmarks/maintainer-acceptance.json) indexed 24 files / 267 chunks / 220 symbols with no failures, found the `parse_reply` definition/context, detected an edit as stale, identified that function in Git impact, updated exactly one file (23 unchanged), and returned to fresh after restoration. All edits were in a disposable copy. No application, booking, browser or email operations were executed. This adds Python navigation/update evidence; it cannot validate C# caller resolution, SQL writes, or Razor page impact, which still lack an independent production-project evaluation.

```bash
python benchmarks/real_projects.py --manifest benchmarks/maintainer-project.json --baseline-source /path/to/0.9.1-checkout --lexical --save /tmp/maintainer-project.json
```

## Scaling evidence

[Raw scaling records](../benchmarks/performance-results.json). Generated Python functions, not real C# production workloads. Five warm repetitions per variant, separate worker processes, one BLAS thread, identical cached/uncached result spans.

| Actual chunks | Initial index | First CLI query | Warm p95, cache off → on | Query peak RSS, cache on |
|---|---:|---:|---:|---:|
| 10,000 | 1.53 s | 0.37 s | 168 → 143 ms | 65.8 MiB |
| 50,000 | 7.69 s | 1.09 s | 787 → 730 ms | 211.3 MiB |
| 100,000 | 13.83 s | 2.19 s | 2,202 → 2,010 ms | 401.7 MiB |

At 100k chunks, updating 1/10 files takes about 0.98/1.05 s; refreshing after a Git checkout rewriting 100 source files takes 2.34 s, excluding checkout time. These are machine-specific sample timings; p95 from five repeats is not a population latency guarantee. Raw records include all timings, database sizes, index/query memory separately, and actual update counts.

```bash
python benchmarks/performance.py --sizes 10000 50000 100000 --repeats 5 --save /tmp/scaling.json
```

Ordinary CI runs the 1,000-function smoke case. The manual `full-benchmarks` workflow runs the full sizes. Timing budgets are not adjusted to make a particular machine pass.

## Client evidence and limitations

[Client results](../benchmarks/client-compatibility.json) · [native Codex MCP events](../benchmarks/codex-tool-events.json).

The official Python MCP SDK connects over stdio and validates/calls all seven tools. Real Claude Code 2.1.328 and Codex CLI 0.170.0 also called the MCP data-access tool with a deterministic local model stub and received the expected source evidence. This verifies client transport/configuration, not hosted model reasoning, billing, or end-to-end answer quality. Claude consumes the structured JSON; native Codex events retain both text and structured output.

The captured Codex run also attempted an unrelated inherited connector, which failed. Automatic approval review rejected another outside-sandbox run because of possible external-service traffic. Its already captured successful native tool events were verified offline. The harness now disables inherited MCP servers for an explicitly authorized future run; automated CI runs the SDK and Claude checks. No further Codex run was made after that rejection.

```bash
python scripts/export_contracts.py --check
python scripts/check_mcp_clients.py --claude /path/to/claude --save /tmp/claude-result.json
```

The optional `--codex /path/to/codex` starts the real CLI and may require normal client state-directory access. Review its effective configuration and permit that execution explicitly in a restricted environment. No real model API key is required by the local stub.
