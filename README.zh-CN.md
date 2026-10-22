# Local Codebase Indexer（本地代码索引器）

[English](README.md) | [繁體中文](README.zh-TW.md) | **简体中文**

为 AI 编程助手（Claude Code 通过 MCP，GitHub Copilot / Codex 通过 CLI）打造的本地代码索引与上下文提供工具，让它们在大型遗留项目中用更少的 token 找到代码。索引全部存放在项目下的 `.ai_context/indexer.db`，不连接任何云服务。设计依据见 [`index.md`](index.md)。

## 安装

```bash
pip install local-codebase-indexer              # Python 3.10+
# 或与其他包隔离安装：
pipx install local-codebase-indexer
# 可选：真正的嵌入模型（见下文）pip install "local-codebase-indexer[onnx]"
```

从源码：`pip install -e .`（在 clone 下 `bin/indexer` 仍可使用）。

## 使用（CLI）

```bash
local-codebase-indexer --root <项目路径> index                      # 建立 / 增量更新索引（--force 全部重建）
local-codebase-indexer search "database mapping for LotNo" --limit 5
local-codebase-indexer relations IExistOpenHandleService --direction both   # upstream | downstream | both
local-codebase-indexer context Services/LotService.cs --lines 120 --padding 15   # 别名：slice
local-codebase-indexer data-access MES_Lot                           # 谁读 / 写 / 执行某数据表或存储过程；存储过程碰了哪些数据
local-codebase-indexer stats
local-codebase-indexer status                                       # 索引是否新鲜、失败文件、git commit、一致性检查
```

- 不指定 `--root` 时使用当前目录，也可用环境变量 `INDEXER_ROOT`。
- 加 `--json`（放在子命令前）输出 JSON。
- `search`、`relations`、`context` 执行前会自动做一次增量更新（先比较 mtime，mtime 变了才计算 md5）。
- 规格中的工具名称也可以直接作为子命令：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`（参数 `--query`、`--symbol_name --relation_direction`、`--file_path --focus_lines --padding_lines`）。

## 使用（Claude Code / MCP）

```bash
claude mcp add local-indexer -- local-codebase-indexer --root /path/to/project serve
# 不安装直接运行：claude mcp add local-indexer -- uvx local-codebase-indexer --root /path/to/project serve
```

`serve` 通过 stdio 提供 JSON-RPC（MCP），暴露五个工具：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`、`codebase_get_data_access`、`codebase_index_status`。

## 三层索引

| 层 | 实现 |
|---|---|
| Syntactic | tree-sitter 解析 C#、Python、JavaScript、TypeScript/TSX；SQL 用正则提取 PROCEDURE / VIEW / FUNCTION / TABLE；其余文本文件（VB6 `.frm`、`.config`、`.md` 等）切成窗口块 |
| Topology | `using` / `import` 解析为文件依赖；继承（base list）；类型使用（`invocation`）。存放在 `dependencies` 表 |
| Semantic | 代码块嵌入 384 维，存入 `vss_chunks`；查询时用 FTS5 关键字、符号名称、向量三路做 Reciprocal Rank Fusion，再展开前 3 名文件的依赖邻居 |

编码：先尝试 UTF-8，再尝试 Big5、cp1252。单个文件超过 5MB 不索引；遵守根目录与子目录的 `.gitignore`；固定跳过 `.git`、`bin`、`obj`、`node_modules` 等。

## 与规格的差异

- **向量存储**：规格中的 `sqlite-vss` 需要原生扩展且已停止维护，这里 `vss_chunks` 改为存 float32 BLOB，用 numpy 暴力计算 cosine，不需要额外安装扩展。
- **嵌入模型**：默认是内置的 hashing 嵌入（标识符拆词、双词组、中文双字），快速、可复现，但**不理解同义词**。要使用真正的 transformer，把 `all-MiniLM-L6-v2` 或 `bge-small-en-v1.5` 导出为 ONNX，安装 `onnxruntime`、`tokenizers`，并设置 `INDEXER_ONNX_MODEL=<包含 model.onnx 与 tokenizer.json 的文件夹>`（加载、补齐、池化与重建索引的逻辑已用一个小型合成模型测试过；真实模型的检索质量尚未度量，设置好变量后请运行 `python benchmarks/run_benchmark.py --compare benchmarks/baseline.json` 验证；目前使用平均池化，适合 MiniLM，bge 系列建议使用 CLS 池化）。切换嵌入后会自动重建索引。
- **C# 调用关系**以有限度的类型推断解析（using / namespace、字段与构造函数注入的类型、局部变量、接口对应实现），每条关系都标置信等级：`confirmed`（接收者类型已知）、`probable`（经由接口或唯一候选）、`candidate`（仅名称相符）。没有 Roslyn，所以不做按参数类型的重载解析、不处理委托 / lambda / 扩展方法，调用链只追一层。继承关系来自 AST，可靠。
- 额外增加了 `symbols.qualified_name`、`chunk_metadata.symbol_id`、`raw_refs`、`chunks_fts` 与 `meta` 表。

## 数据访问（SQL）

`data-access <数据表或存储过程>` 回答“谁写入 / 读取 / 执行 X”。数据来源：T-SQL（INSERT / UPDATE / DELETE / MERGE / TRUNCATE / SELECT INTO / FROM / JOIN / EXEC，含别名解析，排除 CTE、临时表、系统对象）、C# 的 SQL 字符串（字面值、`+`、字符串插值、`string.Format`、常量、局部变量）经 Dapper、`SqlCommand` 或 EF `FromSql` 发出、配合 `CommandType.StoredProcedure` 的存储过程名称，以及 EF 的 `ToTable` / `[Table]` 映射。调用某存储过程的代码视为该过程所写数据表的间接写入者（最多追 4 层）。每条都有置信等级；**动态 SQL（`EXEC(@sql)`、`sp_executesql`、由未知值组成的 SQL）无法解析，会单独列为 `uncertain`**，不会混进确定的清单。已知缺口：`FROM a JOIN b ON ..., c` 会漏掉 `c`；把存储过程名称传给非数据库方法时看不到。

自然语言搜索遇到“谁修改 MES_ScrapRecord”“哪些程序会写入 MES_Lot”这类问题，会先走这个索引。

## 查询扩展

查询会用小型同义词字典扩展（`SP` / `预存程序` / `stored procedure` / `usp`、数据表、批号、报废等；见 `local_codebase_indexer/synonyms.py`）。可在 `.ai_context/synonyms.json` 为项目新增或停用分组：`{"groups": [["wpnlot", "在制批"]], "disable": ["sp"]}`。这对缩写与中英对照有帮助，对自由改写的同义说法没用。

## 索引健康状态

`status`（也是 MCP 工具 `codebase_index_status`）不更新索引，只报告索引是否可信：`fresh`（新鲜）、`stale`（文件有变更或 git HEAD 已移动）、`degraded`（有解析失败的文件或数据库不一致）、`not_indexed`（尚未建立）。解析失败的文件会从索引移除、连同错误原因列出，并在每次更新时重试，所以搜索不会返回它的过期内容。`index --force` 可全部重建。

## 基准测试

`python benchmarks/run_benchmark.py` 用固定问题度量 Recall@5、Recall@10 与 MRR，详见 [`benchmarks/README.md`](benchmarks/README.md)（英文）。

## 测试

```bash
python -m pytest -q
```
