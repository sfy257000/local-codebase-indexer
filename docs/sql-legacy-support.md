# SQL correctness and legacy source support (0.9.0)

The seven MCP tools and existing CLI commands remain available. The index format changes to version 9: the first update after upgrading rebuilds the index to replace previously missing or incorrect edges. On a large project this first run can take longer; subsequent updates are incremental.

## SQL Server references

The parser handles aliases such as `a`, `an`, and `the`; parameterized `TOP (@n)` and simple parenthesized expressions; `UPDATE ... WITH (ROWLOCK) SET`; and old table hints such as `FROM dbo.Lot (NOLOCK) t`. A comma source after `JOIN ... ON` is included, while commas in nested expressions are not treated as table lists.

Bracketed and double-quoted names preserve escaped delimiters, spaces, punctuation, and linked-server qualifiers. For example:

```sql
UPDATE a SET Status = 'Approved'
FROM [ntemes\mes].MESDB.dbo.[Lot Master] a;
```

```bash
local-codebase-indexer --root /path/to/project data-access "Lot Master" --full
```

Names are still queried by their final object-name segment. Inspect qualifiers/source evidence when different databases use the same table name; this is not a SQL Server catalog resolver.

Cursor `FETCH ... FROM` and scalar `TRIM(... FROM ...)` do not create table reads. Nested block comments are masked as a whole. Literal SQL inside `OPENQUERY` is surfaced as **uncertain**, like dynamic SQL, because the remote catalog and execution are not validated. Existing dynamic SQL uncertainty remains visible.

C# file-level SQL strings passed to database APIs are attributed to each executing method at its use site. Constants of the same name in different classes do not share their values. A SQL-looking field declaration can still appear as a probable, unproven lead; it is not proof the string executes.

## Unicode source

UTF-8, Big5, and cp1252 behavior is retained. BOM-tagged UTF-16 LE/BE and UTF-32 LE/BE are recognized before binary detection and decoded consistently by indexing, context extraction, and diagnostics. BOM-less UTF-16 is not guessed. True binary files are excluded from both scanning and freshness checks, so they do not remain permanently pending as new files.

## Schema changes

`CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`, `CREATE INDEX`, and `DROP INDEX ... ON` use `access=schema`. `data-access` returns them in an additive `schema_changes` section, with the same confidence filters and pagination as other sections. Procedures' `touches` results can also include `schema_changes`.

Schema operations are separate from row readers/writers. `impact` includes file-level SQL even when it has no containing symbol, with source evidence and historical snapshot labels. This identifies source-level schema operations; it does not calculate column-level migration safety or validate a live database.

## VB6 sources

`.frm`, `.bas`, and `.cls` retain text-search support and now expose SQL strings under their enclosing Sub/Function/Property. Analysis includes literals, doubled quotes, constants/variables, `&`/`+` concatenation, line continuations, and common `.Execute`, `.Open`, and `.CommandText` sites. Apostrophe and `Rem` comments are excluded.

Complete SQL at an explicitly declared ADO/DAO receiver can be `confirmed` as a static call site. An unknown receiver or incomplete expression remains `probable`; an unknown table or unresolved command is `uncertain`. This does not prove the receiver's runtime state. VB6 symbols use `vb6_method` so they cannot be mistaken for SQL functions or C# call targets. There is no VB6 compiler, full VB6 call graph, general `With`-block resolution, or interpretation of arbitrary functions constructing SQL.

## Include a source directory that has a conventional build name

Defaults still exclude generated/dependency directories. If your application keeps real source in `bin`, `packages`, `build`, or `dist`, put explicit negations in `.ai_context/ignore`:

```gitignore
!bin/
!bin/**
!packages/
!packages/**
!build/
!build/**
!dist/
!dist/**
```

These project rules have final precedence over `.gitignore`. Include excluded parents as well when the directory is nested. `.git`, `.svn`, `.hg`, and `.ai_context` remain excluded to prevent indexing repository/index state. Symlinks and size limits retain their existing behavior. Check an individual result with `doctor --path bin/RealSource.cs`.

## Verification and development

`tests/test_sql_regressions.py` contains the reported syntax cases, negative examples, all four Unicode BOM encodings, constant ownership, schema impact, VB6, and directory opt-in tests. The rest of the test suite covers search, call navigation, ONNX, MCP recovery, concurrency, pagination, and package/release validation.

CI runs the suite on Linux and Windows with Python 3.10/3.12/3.13, plus installed-package checks. Ruff checks correctness/imports and mypy checks the package with incremental typing (untyped third-party imports are allowed). They are also release validation gates. Dependencies are declared only in `pyproject.toml`.

`query.py` remains the facade and hybrid-search implementation. `relations.py`, `context.py`, and `rendering.py` separate navigation, source extraction, and presentation. Existing `from local_codebase_indexer.query import CodebaseTools, render_*` imports continue to work.
