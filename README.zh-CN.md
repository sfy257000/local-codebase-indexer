# Local Codebase Indexer（本地代码索引器）

[English](README.md) | [繁體中文](README.zh-TW.md) | **简体中文**

为 AI 编程助手（Claude Code 通过 MCP，GitHub Copilot / Codex 通过 CLI）打造的本地代码索引与上下文提供工具，让它们在大型遗留项目中用更少的 token 找到代码。索引全部存放在项目下的 `.ai_context/indexer.db`，不连接任何云服务。设计依据见 [`index.md`](index.md)。

## 安装

```bash
pip install -r requirements.txt   # Python 3.10+
```

## 使用（CLI）

```bash
bin/indexer --root <项目路径> index                      # 建立 / 增量更新索引（--force 全部重建）
bin/indexer search "database mapping for LotNo" --limit 5
bin/indexer relations IExistOpenHandleService --direction both   # upstream | downstream | both
bin/indexer context Services/LotService.cs --lines 120 --padding 15   # 别名：slice
bin/indexer stats
```

- 不指定 `--root` 时使用当前目录，也可用环境变量 `INDEXER_ROOT`。
- 加 `--json`（放在子命令前）输出 JSON。
- `search`、`relations`、`context` 执行前会自动做一次增量更新（先比较 mtime，mtime 变了才计算 md5）。
- 规格中的工具名称也可以直接作为子命令：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`（参数 `--query`、`--symbol_name --relation_direction`、`--file_path --focus_lines --padding_lines`）。

## 使用（Claude Code / MCP）

```bash
claude mcp add local-indexer -- python /path/to/local-codebase-indexer/bin/indexer --root /path/to/project serve
```

`serve` 通过 stdio 提供 JSON-RPC（MCP），暴露三个工具：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`。

## 三层索引

| 层 | 实现 |
|---|---|
| Syntactic | tree-sitter 解析 C#、Python、JavaScript、TypeScript/TSX；SQL 用正则提取 PROCEDURE / VIEW / FUNCTION / TABLE；其余文本文件（VB6 `.frm`、`.config`、`.md` 等）切成窗口块 |
| Topology | `using` / `import` 解析为文件依赖；继承（base list）；类型使用（`invocation`）。存放在 `dependencies` 表 |
| Semantic | 代码块嵌入 384 维，存入 `vss_chunks`；查询时用 FTS5 关键字、符号名称、向量三路做 Reciprocal Rank Fusion，再展开前 3 名文件的依赖邻居 |

编码：先尝试 UTF-8，再尝试 Big5、cp1252。单个文件超过 5MB 不索引；遵守根目录与子目录的 `.gitignore`；固定跳过 `.git`、`bin`、`obj`、`node_modules` 等。

## 与规格的差异

- **向量存储**：规格中的 `sqlite-vss` 需要原生扩展且已停止维护，这里 `vss_chunks` 改为存 float32 BLOB，用 numpy 暴力计算 cosine，不需要额外安装扩展。
- **嵌入模型**：默认是内置的 hashing 嵌入（标识符拆词、双词组、中文双字），快速、可复现，但**不理解同义词**。要使用真正的 transformer，把 `all-MiniLM-L6-v2` 或 `bge-small-en-v1.5` 导出为 ONNX，安装 `onnxruntime`、`tokenizers`，并设置 `INDEXER_ONNX_MODEL=<包含 model.onnx 与 tokenizer.json 的文件夹>`（此路径目前没有实机验证）。切换嵌入后会自动重建索引。
- **使用关系**是标识符名称匹配，不做类型解析；同名类、方法可能混在一起。继承关系则来自 AST。
- 额外增加了 `symbols.qualified_name`、`chunk_metadata.symbol_id`、`raw_refs`、`chunks_fts` 与 `meta` 表。

## 测试

```bash
python -m pytest -q
```
