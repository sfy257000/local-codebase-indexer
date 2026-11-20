# Changelog

## 1.1.0 — 2026-11-20

- Chinese (CJK) queries rank much better in hybrid search. The symbol-name and vector layers are scaled by the share of non-CJK characters in the query, so a Chinese query is ranked by the keyword layer with synonym expansion. On 20 paraphrased Chinese navigation questions in a private 3.4k-file C# project, hybrid MRR went from 0.48 to 0.84 and first-rank hits from 6 to 16; the synthetic, noisy and stdlib benchmark suites are unchanged.
- Data-access `programs` and impact mark paths that reach a method only through an interface or base method with several implementations as `uncertain` (`interface dispatch, one of N implementations`). A single implementation stays `probable`; abstract declarations are not counted. On the same project, the programs left at `probable` for two tables were exactly the screens that write them, with shared-pipeline fan-out moved to `uncertain`.
- Historical impact snapshots start from a copy of the current index and use the project's `.ai_context/ignore`, so only files whose content differs are parsed and the base revision honours the same exclusions (previously it also indexed files the project excludes). First runs on the same project: the latest commit (2 files changed) 3 min 38 s to 1 min 29 s; a commit on another branch whose snapshots differ from the working tree in 1,336 files 7 min 9 s to 4 min 8 s. Extracting the snapshot to disk is now most of the remaining time; later runs reuse the cache.
- When a one-line class and its method share a search result span, the result is labelled with the innermost symbol (the method).
- No index format change.

## 1.0.4 — 2026-11-20

- The MCP server answers `initialize`, `ping` and `tools/list` without opening the index; the first `tools/call` opens it. Opening can take longer than a client's connection timeout when an upgrade adds indexes to a large existing database on a cold cache (Claude Code gives up after 30 s). A failure to open the index is reported as a tool error and the next call retries.
- A missing project root still fails at startup.

## 1.0.3 — 2026-11-20

- Faster incremental updates. Resolving EF context types looked up method text and qualified names without an index, once per EF access in the whole project, on every update with changes. New `idx_chunk_symbol` and `idx_symbols_qualified` indexes make those lookups direct. File dependencies now read `raw_refs` with a table scan instead of an index plus a table lookup per row. On a 3.4k-file project, an update of 28 changed files went from 73 s to 23 s (EF resolution 49 s to 0.4 s, dependencies 6.9 s to 2.1 s); the dependency rows are identical.
- `doctor` no longer reopens indexed files whose modification time is unchanged.
- No index format change. The two indexes are created automatically the first time an existing index is opened.

## 1.0.2 — 2026-11-20

- Scanning no longer opens every indexed file to check for binary content. A file already indexed with an unchanged modification time is known to be text; new and modified files are still checked. On a 3.4k-file project this removed about 3,400 file opens from every status call and pre-query refresh (about 6 s each with a warm cache, much longer while antivirus scans cold files).
- `status` consistency checks read indexes instead of whole tables: rowid sets are compared with `EXCEPT` over covering indexes, the full-text side uses the FTS5 `docsize` shadow table, and orphaned reference paths are found by index seeks over distinct paths. A new `idx_vss_rowid` index covers vector rowids. On a 420 MB index this cut the data a status call reads from about 460 MB to about 65 MB, which dominated a cold first call. Corruption outside an indexing run is still detected.
- No index format change. The new vector index is created automatically the first time an existing index is opened (about 15 s on a 42k-chunk index).

## 1.0.1 — 2026-11-20

- Program tracing follows interface methods implemented by abstract base classes: an override in a subclass is reached from callers of the interface or base method (`override dispatch`), so data-access `programs` and impact no longer report such pipeline steps as having no known caller.
- Impact `tables` include accesses made by methods the changed code calls (confirmed / probable calls, up to 4 hops). Each entry carries `depth` and `via_chain`; a `callee_depth_limit` coverage warning marks a cutoff.
- No index format change; existing indexes keep working.

## 1.0.0 — 2026-11-20

- Simplify all three READMEs: remove development-stage notes, repeated benchmarks, and maintainer-specific acceptance narratives; retain practical setup/report commands and source-backed tool comparisons.

- Keep BOM-tagged UTF-16/UTF-32 SQL eligible in doctor, matching indexing/status; add separate maintainer-supplied Python workflow/retrieval acceptance.

- Preserve four-part SQL identities, delimited dots, explicit USE context, EF schema mappings, and quoted C# stored-procedure names. Return qualified candidates for ambiguous short names; keep unresolved scope under possible instead of confirmed.
- Use AST byte ranges for compact C# source ownership and distinct same-line overload IDs; preserve differently qualified SQL accesses on the same source line.
- Add representative source-evidence paths, action-specific page locations, bounded SQL/C# depth controls, and visible cutoff/unavailable-definition warnings, including Git impact metadata.
- Negotiate supported MCP protocol versions; provide structured results/output schemas and matching CLI JSON metadata while retaining legacy text. Add seven checked-in contracts, a full tool manifest, bounded structured defaults with exact truncation/pagination, retryable busy errors, and official-SDK tests.
- Add project-local init/configuration, stderr indexing progress, and offline escaped HTML/SVG, Mermaid and JSON impact reports with explicit overwrite handling.
- Add optional lexical search, soft file diversity, and a bounded revision-aware in-process vector cache; bind cursors to retrieval mode/algorithm.
- Add 100 frozen navigation questions on three pinned public C# projects, baseline/candidate records, real-client transport evidence using a local mock model, and 10k/50k/100k scaling/update/Git-switch measurements. Preserve retrieval losses and limits on interpretation.
- Extend Linux/Windows CI to Python 3.11, package the new tests/dependencies, check contracts, run public-code/performance smoke evaluations, and test Claude transport automatically. Codex native tool events are recorded from a manual transport run.
- Synchronize English/Traditional Chinese/Simplified Chinese README and reference additions; correct obsolete JOIN and index-format guidance.
- Index format 11 rebuilds older parsed data on the first update. Package and MCP Registry versions are synchronized at 1.0.0.

## 0.9.1

- Bound dynamic `sp_executesql` analysis to its SQL argument and statement; preserve uncertainty for quoted procedure names and exclude unrelated literals/column names.
- Scope CTE exclusions to one consuming statement/batch, including omitted semicolons and UNION/INSERT SELECT and stop UPDATE/DELETE alias lookup at the next command, including commands without semicolons.
- Preserve comma sources following JOIN predicates that use qualified `go` columns; recognize GO only as a batch separator line.
- Restrict SQL object definitions/suggestions to SQL source files so application functions do not become tables/procedures or redirect SQL search routing.
- Respect C#/VB6 parameter and local shadowing, invalidate overwritten SQL, and avoid interpreting query parameters as SQL. Preserve earlier valid execution evidence and known ADO declarations.
- Add English/Traditional Chinese/Simplified Chinese tool comparisons with documented trade-offs, pinned upstream sources, and a reproducible literal-search/relation-report example.
- Quantify those comparisons with a reproducible ripgrep + BM25 baseline, unchanged retrieval questions, raw per-query records, file-level recall/MRR, exact character denominators, and CLI evidence coverage/latency. Show stdlib losses and tiny-file output overhead alongside MES gains in all three READMEs. Add benchmark protocol regression tests and a CI artifact smoke run.
- Index format 10 rebuilds stale parsing results on the first update. Package and Registry versions remain unchanged until a release is prepared.

## 0.9.0

- Fix a first-run Windows lock-file initialization race found by concurrent-process CI; serialize index ignore-file creation.

- Fix SQL aliases `a` / `an` / `the`, parameterized TOP, UPDATE table hints, old NOLOCK hints, and comma sources after JOIN predicates.
- Preserve bracketed/quoted identifiers and linked-server names, including spaces, backslashes, escaped delimiters, and quoted dots. Ignore keywords inside identifiers and nested block comments.
- Exclude cursor FETCH and scalar TRIM FROM operands from table reads; analyze literal OPENQUERY SQL as uncertain.
- Decode BOM-tagged UTF-16/UTF-32 before binary detection; make source context and freshness diagnostics consistent. True binary files no longer remain pending forever.
- Attribute constant C# SQL to each executing method without sharing same-named constants across unrelated classes.
- Add conservative VB6 SQL analysis for .frm/.bas/.cls, with enclosing method owners and explicit confidence levels.
- Track table/index schema operations separately from row writes; include file-level SQL and before/after source evidence in Git impact.
- Allow explicit project-ignore negations to include default build/dependency-named source directories, preserving hard exclusions for VCS and index state.
- Split relation, context, and rendering code out of the query facade while preserving public imports and commands.
- Clear the existing mypy errors; add Ruff/mypy CI and release gates; test Windows and Linux on Python 3.10, 3.12, and 3.13.
- Canonical assistant instructions live in AGENTS.md; CLAUDE.md points there. Remove duplicate requirements.txt dependency declarations.
- Index format 9 triggers one full rebuild after upgrade. See docs/sql-legacy-support.md for supported patterns and limitations.

## 0.8.0

- MIT license and SPDX package metadata.
- Bundled `demo` command traces a synthetic SQL Server table through a stored procedure, C# callers, controller actions, and a Razor page without a database or .NET runtime. JSON output includes source evidence, four scenario checks, an exact-reference baseline, and measured timings.
- `demo --output` saves the fixture in a new or empty directory and refuses to overwrite user content.
- English, Traditional Chinese, and Simplified Chinese READMEs focus on the SQL Server / C# workflow with a three-step first run and a reproducible 32-second demonstration.
- Recorded case study, raw evidence, contribution guide, issue forms, and community/directory submission copy.
- Official MCP Registry publication follows successful PyPI publication using GitHub OIDC and a pinned, signature-verified publisher.

## 0.7.0

- Stable search ranking across page sizes/offsets, with query- and revision-bound continuation cursors.
- Historical Git ranges analyze both requested snapshots instead of mixing in the current working tree.
- ONNX identities fingerprint model/tokenizer/external weights and inference settings, triggering rebuilds when contents change.
- Process-safe indexing, consistent query snapshots, bounded busy responses (CLI exit 4), and interrupted-batch recovery.
- Context clipping preserves focus lines and reports omitted/clipped content.
- Project synonyms reload on queries; malformed settings warn once per content version and recover after correction.
- Opt-in search explanations show matched terms, expansion provenance, layer ranks/contributions and SQL evidence.
- Project evaluation datasets, per-query reports, retrieval/latency/token metrics, baseline comparison and enforceable budgets.
- Future Release events test and build once, validate versions, and upload the same artifacts to PyPI/GitHub with SHA256SUMS. Upload retries are idempotent and do not overwrite conflicting assets.


## 0.6.0

### Fixed

- Impact analysis reports all methods in a changed hunk and retains removed methods/files, historical SQL accesses and callers. Current and historical line numbers are labeled separately.
- Malformed JSON-RPC messages and invalid tool arguments return errors without terminating the MCP session.
- Nested `.gitignore` negations correctly override parent file patterns.
- Missing project roots fail before creating directories; empty or partially failed indexing returns exit status 3 with diagnostics.
- Search strictly honors its result limit, including queries routed to SQL data access.
- Generated packaging metadata is no longer tracked; source distributions exclude generated benchmark indexes.
- CI explicitly installs and imports `onnx`, `onnxruntime` and `tokenizers`, so ONNX plumbing tests cannot silently skip for missing dependencies.

### Added

- `doctor --path <file>` and the `codebase_index_doctor` MCP tool explain indexing exclusions and report index health.
- Search file/language filters, offset pagination, similar-name suggestions and actionable no-result hints.
- Data-access confidence filtering, per-section pagination, totals and next offsets. `--full` / `detail=true` no longer silently truncates at 40 displayed or 200 queried entries.
- General English/Chinese paraphrases for common operations and password terminology. Keyword expansion is separated from vector expansion to preserve existing rankings.
- Cached historical impact indexes under `.ai_context/impact/`, reusable per base revision and package/index version.
- `--json` and `--root` also work after the subcommand.

### Validation and limits

- Full tests include ONNX plumbing and regression coverage for deleted code, multi-method hunks, Chinese paths, cached historical indexes, invalid MCP messages, ignore negations, filters and pagination beyond 200 entries.
- Python 3.12 comparison against 0.5.0, with unchanged benchmark queries and baseline: synthetic Recall@5 0.893 → 0.917; noisy synthetic 0.857 → 0.881; synonym Recall@5 0.30 → 0.50 in both synthetic suites. Standard-library retrieval metrics are unchanged. These small author-written suites do not establish general semantic understanding.
- First-time historical impact analysis indexes the Git archive and may take time on large projects. External `extra_roots` cannot be reconstructed historically. Existing call-graph and dynamic-SQL limitations remain.
- Pagination assumes the index remains unchanged between requests.
