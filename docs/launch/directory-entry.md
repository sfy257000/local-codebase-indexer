# Directory submission copy — not yet submitted

Name: Local Codebase Indexer

Repository: https://github.com/sfy257000/local-codebase-indexer

License: MIT

Primary category: Developer tools / code navigation

Tags: MCP, C#, SQL Server, T-SQL, Dapper, code search, static analysis, local

Short description:

> Trace SQL Server tables and stored procedures to C# callers, controller actions, and Razor pages locally. MCP server and CLI with source locations and confidence labels.

Long description:

> A local source index for maintaining C# / SQL Server applications. Combines SQL access analysis, C# call relations, controller/page links, hybrid search, and Git change impact. Stores its index in project-local SQLite and exposes seven stdio MCP tools. Includes a synthetic demo that runs without a database or .NET runtime. Static analysis has gaps around dynamic SQL and runtime dispatch; results include confidence labels for verification.

MCP launch configuration (replace the absolute source path):

```json
{
  "mcpServers": {
    "local-indexer": {
      "command": "uvx",
      "args": ["local-codebase-indexer", "--root", "/absolute/path/to/project", "serve"]
    }
  }
}
```

Requires Python 3.10+ and `uv` for this launch form; pip-installed clients can invoke `local-codebase-indexer` directly.

Registry name: `io.github.sfy257000/local-codebase-indexer`

Registry manifest: https://github.com/sfy257000/local-codebase-indexer/blob/main/server.json

Walkthrough: https://github.com/sfy257000/local-codebase-indexer/blob/main/docs/case-study.md

Screenshot: https://raw.githubusercontent.com/sfy257000/local-codebase-indexer/main/assets/demo-preview.png

Animation: https://raw.githubusercontent.com/sfy257000/local-codebase-indexer/main/assets/demo.gif

Suggested Markdown entry for a compatible code-navigation list:

```markdown
- [Local Codebase Indexer](https://github.com/sfy257000/local-codebase-indexer) - Trace SQL Server tables to C# callers, controller actions, and Razor pages locally; MCP server and CLI with confidence labels. Python 3.10+, MIT.
```

Use the destination's own contribution format. Do not label an entry as accepted or verified until the directory confirms it.
