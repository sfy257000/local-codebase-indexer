# Community post draft — not yet posted

Suggested audience: a C# / SQL Server maintenance discussion that permits self-promotion. Disclose authorship and check each community's rules before posting.

## Title

Tracing a SQL Server table to C# callers and Razor pages with a local CLI / MCP server

## Post

I maintain Local Codebase Indexer, an MIT-licensed Python tool for inspecting source trees locally. The specific maintenance question I wanted to make easier is: “Which code writes this table, and which controller actions and pages reach it?”

For C# / SQL Server applications, it combines T-SQL access, C# SQL/procedure calls, a typed call graph, and controller/page links. The CLI shows file paths and confidence labels; the same evidence is available to an MCP client.

A bundled synthetic example lets you try this without SQL Server or a .NET runtime:

```bash
python -m pip install --upgrade local-codebase-indexer
local-codebase-indexer demo
```

The example finds an indirect writer through `usp_ApproveOrder`, then rolls it up to `OrdersController` and `Views/Orders/Details.cshtml`. The controller relation is labeled `probable` because it goes through an interface. Four checks pass on this small fixture; that is not a production accuracy claim or a measured time-savings result. Dynamic SQL and C# overload/delegate resolution still have limitations.

Code and 32-second walkthrough: https://github.com/sfy257000/local-codebase-indexer

Source-by-source case study and raw report: https://github.com/sfy257000/local-codebase-indexer/blob/main/docs/case-study.md

The index stays in the project directory. If you connect an AI assistant, its model-provider settings determine where the returned context is sent.
