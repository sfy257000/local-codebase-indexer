# Local Codebase Indexer

**English** | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

**Which C# code and pages reach this SQL Server table?**

Trace tables and stored procedures to repositories, controller actions, and Razor pages in a local source tree. Use the CLI yourself or give your AI assistant the same evidence through MCP. Especially useful when maintaining C# / SQL Server applications with Dapper, ADO.NET, or Entity Framework.

![A 32-second walkthrough tracing SQL Server access to C# and a Razor page](https://raw.githubusercontent.com/sfy257000/local-codebase-indexer/main/assets/demo.gif)

[Case study and raw results](https://github.com/sfy257000/local-codebase-indexer/blob/main/docs/case-study.md) · [CLI reference](https://github.com/sfy257000/local-codebase-indexer/blob/main/docs/reference.md) · [PyPI](https://pypi.org/project/local-codebase-indexer/) · [MIT license](https://github.com/sfy257000/local-codebase-indexer/blob/main/LICENSE)

## How it compares with Claude's Grep and similar tools

**Measured on our MES fixtures: +9.5–13.1 percentage points in relevant-file recall versus `rg + BM25`.** For the bundled SQL → C# → page investigation, the relation report covers **5/5 required evidence files**, versus **3/5** from one literal table-name search (**+40 points**).

| Fixed retrieval suite | `rg + BM25` file Recall@5 | This project | Difference |
|---|---:|---:|---:|
| MES-style synthetic: 42 questions, 24 files | 81.0% | **94.0%** | **+13.1 pp** |
| Same 42 questions with distractors: 109 files | 81.0% | **90.5%** | **+9.5 pp** |
| Python stdlib: 12 questions, 85 real source files | **100.0%** | 79.2% | −20.8 pp |

Recall is averaged over 54 project-authored questions (96 executions including the noisy rerun). The indexer returns 5 chunks; `rg + BM25` returns 5 files, and both are scored by file. This is a fixed-fixture comparison, not a complete Claude agent benchmark.

The richer relation report is slower than a single literal search: **228.5 ms versus 18.8 ms** on the bundled scenario (three-run medians). Controller reachability is `probable`, not runtime proof. [Protocol](docs/v1-readiness.md#candidate-versus-the-literal-search-baseline) · [Raw results](benchmarks/v1-tool-comparison.json) · [Per-query records](benchmarks/v1-tool-comparison-queries.jsonl).

**The extra value is an indexed SQL → C# → page relation report.** For supported patterns, one `data-access` query returns direct/procedure-mediated access, controller actions, page links, and confidence labels. Claude can also follow these links through multiple Grep searches and file reads; this tool packages that investigation into a repeatable CLI/MCP result.

| Tool / workflow | Strong fit | Trade-off and where this project helps |
|---|---|---|
| Claude + Grep / ripgrep | Exact identifiers, strings, and regex; no index setup | Keep it for direct text checks. This project's SQL/call/page relations go beyond the literal table-name matches. |
| [Serena](https://github.com/oraios/serena) with LSP / JetBrains | Backend-driven symbols, references, and editing/refactoring | Prefer compiler/IDE semantics for accurate renames and overloads. This project provides a focused SQL maintenance report without a language-server backend. |
| [Claude Context](https://github.com/zilliztech/claude-context) | Natural-language hybrid BM25 + dense-vector retrieval | Its documented setup uses an embedding provider and Milvus/Zilliz configuration; local providers are supported. This project's default needs no embedding API or vector server, but identifier-oriented hashing can miss paraphrases. |
| Local Codebase Indexer | SQL Server table/procedure → C# accessors → actions/pages; schema and Git impact | Approximate static analysis with visible confidence; no Roslyn overload resolution or semantic rename. |

The controller and Razor page in the demo contain no literal table name; the relation report reaches them through the stored procedure and C# calls. All **4 built-in relationship checks pass**. See [source evidence and upstream feature references](docs/tool-comparison.md).

## Try it in three steps

Python **3.10+**. Run these in a virtual environment; no SQL Server, .NET runtime, model download, or API key is needed for the demo.

**1. Install**

```bash
python -m pip install --upgrade local-codebase-indexer
```

If Python reports an externally managed environment, create and activate a [virtual environment](https://docs.python.org/3/library/venv.html) first. See the [reference](https://github.com/sfy257000/local-codebase-indexer/blob/main/docs/reference.md#install) for isolated installation alternatives.

**2. Run the bundled example**

```bash
local-codebase-indexer demo
```

This creates a temporary **synthetic** source tree, indexes it, and checks four expected relationships. It does not index your current directory. The animation uses this command's actual output, with reading pauses added. An excerpt:

```text
table Sales_OrderHeader
  WRITTEN BY (2)
    [confirmed] procedure dbo.usp_ApproveOrder
    [confirmed] method Shop.Data.OrderRepository.Approve ... via usp_ApproveOrder
  READ BY (1)
    [confirmed] method Shop.Data.OrderRepository.Get
  USED BY PROGRAMS (1)
    [probable] Orders ... (Approve, Details)
        pages: Views/Orders/Details.cshtml
Verified 4/4 expected relationships: PASS
```

Use `local-codebase-indexer demo --output demo-orders` to keep a copy in a new or empty folder, or `demo --json` for the complete report.

**3. Point it at your source tree**

```bash
local-codebase-indexer --root "/path/to/project" data-access YourTable
```

Replace the path and table name. The first query builds the index; subsequent queries refresh it incrementally. SQL definitions must be present in source files for procedure bodies to be analyzed. There is no connection to a live database.

## Give the same evidence to your assistant

After installation, choose your client to create project-local MCP configuration:

```bash
local-codebase-indexer --root "/absolute/path/to/project" init --client claude
local-codebase-indexer --root "/absolute/path/to/project" init --client codex
```

Ask: “Which code writes `YourTable`, and which controller actions and pages reach it? Show paths and confidence levels.” Restart/reconnect the MCP client if needed. For other stdio MCP clients, use command `local-codebase-indexer` and arguments `--root`, your absolute project path, `serve`.

Codex / Copilot can use the CLI. [AGENTS.md](https://github.com/sfy257000/local-codebase-indexer/blob/main/AGENTS.md) contains assistant instructions. The [reference](https://github.com/sfy257000/local-codebase-indexer/blob/main/docs/reference.md) also covers the Claude Code plugin, seven MCP tools, ONNX models, aliases, and configuration.

## More maintenance questions

```bash
local-codebase-indexer --root "/path/to/project" relations OrderRepository.Approve --direction upstream
local-codebase-indexer --root "/path/to/project" impact main...HEAD
local-codebase-indexer --root "/path/to/project" search "order approval" --explain --limit 5
local-codebase-indexer --root "/path/to/project" doctor
local-codebase-indexer --root "/path/to/project" report dbo.YourTable --output table-impact.html
```

Relations show callers; impact compares Git snapshots; search combines identifiers, text, and local embeddings. C#, Python, JavaScript, and TypeScript have symbol parsing; T-SQL and C# have additional data-access analysis. Add `--json` for structured output.

## What to verify

- This is **static analysis**, with confidence labels. Interface dispatch can be `probable`; name-only candidates and dynamic SQL require source verification. There is no Roslyn overload resolution or runtime execution proof.
- The default hashing embedder is best at identifiers and related terms. Natural-language paraphrases can miss results. [Benchmarks](https://github.com/sfy257000/local-codebase-indexer/blob/main/benchmarks/README.md) and the [case study](https://github.com/sfy257000/local-codebase-indexer/blob/main/docs/case-study.md) explain what was measured; four demo checks do not establish general accuracy or time savings.
- The index is local at `<project>/.ai_context/indexer.db`. Indexing needs no remote service after dependencies are installed. Your MCP client's model provider controls where returned context is sent.
- Start with a table or procedure that you know. If files are missing, run `doctor --path path/to/file.cs`; check `.gitignore` and `.ai_context/ignore`. See [known limitations](https://github.com/sfy257000/local-codebase-indexer/blob/main/docs/reference.md#differences-from-the-spec) and [planned work](https://github.com/sfy257000/local-codebase-indexer/blob/main/TODO.md).

Found a wrong edge or missing result? Open a [reproducible issue](https://github.com/sfy257000/local-codebase-indexer/issues/new/choose). [Contributions](https://github.com/sfy257000/local-codebase-indexer/blob/main/CONTRIBUTING.md) are welcome. Licensed under [MIT](https://github.com/sfy257000/local-codebase-indexer/blob/main/LICENSE).

[SQL / Unicode / VB6 support and upgrade notes](https://github.com/sfy257000/local-codebase-indexer/blob/main/docs/sql-legacy-support.md)

<!-- mcp-name: io.github.sfy257000/local-codebase-indexer -->
