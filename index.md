Feature Specification: Universal Local Codebase Indexer & Context Provider for AI Coding Assistants (Claude Code & GitHub Copilot/Codex)
This specification outlines the architecture, data models, tool definitions, and behavior guardrails required to implement a high-performance, token-efficient Local Codebase Indexer. It enables modern AI assistants (such as Claude Code and GitHub Copilot/Codex) to intelligently navigate, analyze, and refactor massive legacy codebases (e.g., enterprise C#/.NET iMES systems, full-stack monorepos) without exhausting context windows or leaking corporate intellectual property.

1. Architectural Overview
The system runs locally as a background daemon or on-demand CLI companion tool. It exposes a standardized capability interface that can be bound via Model Context Protocol (MCP) for Claude, or invoked via custom CLI/terminal workflows by Copilot/Codex.

It utilizes a Three-Layer Indexing Strategy to map the repository locally:

Syntactic Layer (AST): Uses tree-sitter to parse code files into Abstract Syntax Trees, extracting high-level symbols (Classes, Methods, Interfaces, Fields) and explicit definitions.

Topology Layer (Dependency Graph): Maps using, import, or require directives to create a directed graph of file-to-file and symbol-to-symbol dependencies.

Semantic Layer (Vector Index): Embeds docstrings, function signatures, and core business logic chunks using a lightweight, local embedding model (all-MiniLM-L6-v2 or bge-small-en-v1.5) stored in a local SQLite database (sqlite-vss).

2. Local Storage & Data Schema
All index metadata is stored locally within the project's .ai_context/ directory (or mapped to .claude/) to guarantee absolute privacy, low latency, and zero-cloud-dependency.

Database Layout: .ai_context/indexer.db

-- 1. File Metadata Cache
CREATE TABLE files (
file_path TEXT PRIMARY KEY,
last_modified INTEGER NOT NULL,
md5_hash TEXT NOT NULL,
language TEXT NOT NULL
);

-- 2. Syntactic Symbol Table (AST Extraction)
CREATE TABLE symbols (
id TEXT PRIMARY KEY,
file_path TEXT NOT NULL,
symbol_name TEXT NOT NULL,
symbol_type TEXT NOT NULL, -- 'class', 'method', 'interface', 'property'
parent_id TEXT,
start_line INTEGER NOT NULL,
end_line INTEGER NOT NULL,
signature TEXT,
FOREIGN KEY(file_path) REFERENCES files(file_path)
);

-- 3. Dependency Topology
CREATE TABLE dependencies (
source_path TEXT NOT NULL,
target_path TEXT NOT NULL,
dependency_type TEXT NOT NULL, -- 'import', 'inheritance', 'invocation'
PRIMARY KEY (source_path, target_path, dependency_type)
);

-- 4. Virtual Table for Local Semantic Vector Search (sqlite-vss)
CREATE VIRTUAL TABLE vss_chunks USING vss0(
chunk_embedding(384)
);

-- 5. Chunk Metadata Mapping
CREATE TABLE chunk_metadata (
rowid INTEGER PRIMARY KEY,
file_path TEXT NOT NULL,
start_line INTEGER NOT NULL,
end_line INTEGER NOT NULL,
raw_text TEXT NOT NULL
);

3. Core Implementation Workflow
Step 1: Incremental Background Indexing (On-Save or On-Demand)

Scan project directories while respecting .gitignore.

Compare file hashes with the files table. Skip unmodified files.

For modified files, invoke tree-sitter to extract definitions and overwrite existing entries in the symbols table.

Chunk the file dynamically based on syntax blocks/methods, generate embeddings using onnxruntime + local transformer, and update vss_chunks.

Step 2: Hybrid Query Pipeline
When a semantic search is performed, the host execution layer runs a Reciprocal Rank Fusion (RRF) approach:

Keyword/Symbol Match: Query the symbols and files table via standard SQL LIKE or Full-Text Search (FTS5) for explicit tokens.

Vector Match: Convert the user prompt into an embedding vector locally, and run a cosine similarity query on vss_chunks.

Graph Traversal: Identify top seed components, look up their dependencies, and retrieve adjacent code contexts (e.g., if an API controller is hit, pull its injected services).

4. Universal Tool Specifications (JSON-RPC & CLI Contract)
AI Engines can access this subsystem either via JSON-RPC (MCP) or by invoking the compiled indexing CLI tool via terminal commands (bin/indexer <tool_name> --params).

Tool 1: codebase_semantic_search
Finds code components based on natural language descriptions or abstract bugs.

Parameters:

query (string, required): Semantic explanation of what you are looking for. E.g., "Where do we configure the Entity Framework mapping for manufacturing lots?"

limit (integer, optional): Maximum chunks to return. Default is 5.

Tool 2: codebase_get_symbol_relations
Traces inheritance, method usages, and cross-file dependencies via the AST-generated graph.

Parameters:

symbol_name (string, required): Name of the class, interface, or variable. E.g., "IExistOpenHandleService"

relation_direction (string, required): ["upstream", "downstream", "both"]

Tool 3: codebase_get_optimized_context
Assembles a highly condensed snippet block containing only relevant lines to minimize Token payload.

Parameters:

file_path (string, required): Target source file.

focus_lines (array of integers, required): Target lines identified by search tools.

padding_lines (integer, optional): Surrounding lines to extract for context window. Default is 15.

5. Execution Instructions for AI Assistants
For Claude Code (MCP Engine Mode):
Bind this spec to your active Model Context Protocol (MCP) server.

DO NOT use recursive grep or terminal cat commands on files larger than 5MB.

Prioritize executing codebase_semantic_search to find structural anchors before proposing logic changes.

Before modifying dependency injection patterns or entity contexts, invoke codebase_get_symbol_relations to assess compiler-level blast radius.

For GitHub Copilot / Codex (CLI & Custom Instructions Mode):
When asked to inspect unfamiliar business logic or cross-file classes, utilize your terminal execution capability (@terminal or terminal panel) to run the indexer companion CLI.

Example syntax to gather search insights: bin/indexer search "database mapping for LotNo"

Use the localized outputs returned by the command to feed your internal prompt context window, rather than attempting to read the entire codebase into memory.

Always isolate the exact line numbers needed via the CLI slice utility before initiating a heavy workspace refactoring plan.