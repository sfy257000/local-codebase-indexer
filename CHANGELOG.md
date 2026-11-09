# Changelog

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
