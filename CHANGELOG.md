# Changelog

## 0.9.0

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
