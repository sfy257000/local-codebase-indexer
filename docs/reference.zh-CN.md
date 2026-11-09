# Local Codebase Indexer（本地代码索引器）

[English](../README.md) | [繁體中文](../README.zh-TW.md) | **简体中文**

为 AI 编程助手（Claude Code 通过 MCP，GitHub Copilot / Codex 通过 CLI）打造的本地代码索引与上下文提供工具，让它们在大型遗留项目中用更少的 token 找到代码。索引全部存放在项目下的 `.ai_context/indexer.db`，不连接任何云服务。设计依据见 [`index.md`](../index.md)。

## 安装

```bash
pip install local-codebase-indexer              # Python 3.10+
# 或与其他包隔离安装：
pipx install local-codebase-indexer
# 可选：真正的嵌入模型（见下文）pip install "local-codebase-indexer[onnx]"
```

从源码：`pip install -e .`（在 clone 下 `bin/indexer` 仍可使用）。

### 作为 Claude Code plugin 安装

```text
/plugin marketplace add sfy257000/local-codebase-indexer
/plugin install local-codebase-indexer@local-codebase-indexer
```

会为你打开的项目注册 MCP 服务器（实际运行 `uvx local-codebase-indexer --root <项目> serve`，所以要先装 [uv](https://docs.astral.sh/uv/)：`pip install uv`）。安装后重启 Claude Code。

## 使用（CLI）

```bash
local-codebase-indexer --root <项目路径> index                      # 建立 / 增量更新索引（--force 全部重建）
local-codebase-indexer search "database mapping for LotNo" --limit 5
local-codebase-indexer relations IExistOpenHandleService --direction both   # upstream | downstream | both
local-codebase-indexer context Services/LotService.cs --lines 120 --padding 15   # 别名：slice
local-codebase-indexer data-access MES_Lot                           # 谁读 / 写 / 执行某数据表或存储过程；存储过程碰了哪些数据（--full 列出全部）
local-codebase-indexer impact main...HEAD                            # 一次变更动到的符号、数据表、程序与页面（不给参数＝未提交的变更）
local-codebase-indexer stats
local-codebase-indexer status                                       # 索引是否新鲜、失败文件、git commit、一致性检查
```

- 不指定 `--root` 时使用当前目录，也可用环境变量 `INDEXER_ROOT`。
- 加 `--json`（可放在子命令前后）输出 JSON。
- `search`、`relations`、`context` 执行前会自动做一次增量更新（先比较 mtime，mtime 变了才计算 md5）。
- 规格中的工具名称也可以直接作为子命令：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`（参数 `--query`、`--symbol_name --relation_direction`、`--file_path --focus_lines --padding_lines`）。

## 使用（Claude Code / MCP）

```bash
claude mcp add local-indexer -- local-codebase-indexer --root /path/to/project serve
# 不安装直接运行：claude mcp add local-indexer -- uvx local-codebase-indexer --root /path/to/project serve
```

`serve` 通过 stdio 提供 JSON-RPC（MCP），暴露七个工具：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`、`codebase_get_data_access`、`codebase_impact_analysis`、`codebase_index_status`、`codebase_index_doctor`。常驻的 server 在距离上次检查超过 15 秒时，查询前会重新检查索引（几千个文件无变动约 1 秒）。`codebase_get_data_access` 默认返回摘要（每区 5 条、程序全列），加 `detail` 才列全部；`codebase_semantic_search` 加 `related` 才附依赖文件。

## 0.7.0：稳定导航与项目评测

搜索排序不再随页数变化；`pagination.next_cursor` 可用于 `--cursor`／MCP `cursor`，索引或同义词变化时要求重新搜索。`--explain` 显示命中词、扩展来源、排序贡献及 SQL 证据。上下文裁剪优先保留焦点行；同义词自动重载，ONNX 模型和 tokenizer 使用内容指纹。

`impact A..B` 和 `A...B` 使用指定版本的快照；多进程索引使用系统锁和一致性读取，繁忙时 CLI 返回退出码 4。`INDEXER_LOCK_TIMEOUT` 与 `INDEXER_BUSY_TIMEOUT` 控制等待秒数，中断批次会在下次索引时恢复。

`evaluate --queries .ai_context/eval.json --save .ai_context/report.json` 支持项目问题集、Recall@k、MRR、延迟和估计 token，以及基准比较和质量门槛。数据格式为 `{"version":1,"queries":[{"id":"invoice","query":"读取发票","relevant":[{"file":"src/invoices.py","symbol":"load_invoice"}]}]}`。未达到 `--min-recall`／`--max-p95-ms`／`--max-mean-tokens` 门槛返回退出码 3。`benchmarks/queries_project.json` 是本仓库的人工导航示例，不是独立的真实用户评测。

后续 Release workflow 使用同一份经过验证的构建产物发布到 PyPI 和 GitHub，自动生成 SHA256SUMS；使用 Actions 内置令牌，重复运行跳过相同附件，拒绝覆盖不同内容。修改 workflow 不会变更现有 Release。

## 诊断、筛选与分页（0.6.0）

```bash
local-codebase-indexer --root <项目> doctor --path src/Service.cs
local-codebase-indexer --root <项目> search "读取发票" --file '*.py' --language python --limit 5 --offset 0
local-codebase-indexer --root <项目> data-access MES_Lot --confidence confirmed --limit 20 --offset 20
local-codebase-indexer --root <项目> data-access MES_Lot --full
```

`doctor` 解释忽略规则、不支持的扩展名、过大和二进制文件以及索引状态。搜索先筛选再排名；文件 glob 区分大小写，`*` 可以跨 `/`。JSON 提供 `pagination.next_offset`。数据访问支持精确信心等级筛选和每个区段独立分页；`--full`／MCP `detail=true` 不再受旧有 40／200 条上限限制。MCP 搜索使用 `file_pattern`、`language`、`limit`、`offset`，数据访问使用 `confidence`、`limit`、`offset`，新增诊断工具 `codebase_index_doctor`。

`impact` 保留删除代码及历史调用、数据表影响；`snapshot=before` 的行号属于基准版本。历史索引首次建立后缓存在 `.ai_context/impact/`，可安全删除以释放空间；不会重建外部 `extra_roots`，静态分析限制仍适用。不存在的根目录会报错，空索引或部分失败返回退出码 3；MCP 参数错误不会结束服务。新增常见中英文同义词，项目术语仍可配置在 `.ai_context/synonyms.json`。

## 三层索引

| 层 | 实现 |
|---|---|
| Syntactic | tree-sitter 解析 C#、Python、JavaScript、TypeScript/TSX；SQL 用正则提取 PROCEDURE / VIEW / FUNCTION / TABLE；其余文本文件（VB6 `.frm`、`.config`、`.md` 等）切成窗口块 |
| Topology | `using` / `import` 解析为文件依赖；继承（base list）；类型使用（`invocation`）。存放在 `dependencies` 表 |
| Semantic | 代码块嵌入 384 维，存入 `vss_chunks`；查询时用 FTS5 关键字、符号名称、向量三路做 Reciprocal Rank Fusion，再展开前 3 名文件的依赖邻居 |

编码：先尝试 UTF-8，再尝试 Big5、cp1252。单个文件超过 5MB 不索引；遵守根目录与子目录的 `.gitignore`；固定跳过 `.git`、`bin`、`obj`、`node_modules` 等。`*.min.js` / `*.min.css` 也跳过。已提交进仓库的第三方或生成的代码可用 `.ai_context/ignore`（gitignore 语法）排除，例如 `Scripts/*`、`!Scripts/Custom/`、`Web References/`（EF database-first 生成的 model 不要排除，DbSet 声明在它的 DbContext 里）。

## 与规格的差异

- **向量存储**：规格中的 `sqlite-vss` 需要原生扩展且已停止维护，这里 `vss_chunks` 改为存 float32 BLOB，用 numpy 暴力计算 cosine，不需要额外安装扩展。
- **嵌入模型**：默认是内置的 hashing 嵌入（标识符拆词、双词组、中文双字），快速、可复现，但**不理解同义词**。要使用真正的 transformer，把 `all-MiniLM-L6-v2` 或 `bge-small-en-v1.5` 导出为 ONNX，安装 `onnxruntime`、`tokenizers`，并设置 `INDEXER_ONNX_MODEL=<包含 model.onnx 与 tokenizer.json 的文件夹>`（目前使用平均池化，适合 MiniLM，bge 系列建议使用 CLS 池化）。切换嵌入后会自动重建索引。实测（0.4.0）：`all-MiniLM-L6-v2` 让 benchmark MRR 提高 0.08–0.13，但在真实的 3 万 chunk C# / 中文注释项目上，换个说法的查询反而比 hashing 差（MRR 0.29 对 0.48；多语 MiniLM-L12 为 0.41），完整建索引从 2 分钟变成 35–43 分钟。因此默认维持 hashing；领域术语用 `.ai_context/synonyms.json` 比通用模型有效。
- **C# 调用关系**以有限度的类型推断解析（using / namespace、字段与构造函数注入的类型、`Lazy<T>.Value`、局部变量、`foreach (var x in items.Where(..))` 的元素类型、接口对应实现），每条关系都标置信等级：`confirmed`（接收者类型已知）、`probable`（经由接口或唯一候选）、`candidate`（仅名称相符）。没有 Roslyn，所以不做按参数类型的重载解析、不处理委托 / lambda / 扩展方法，调用链只追一层。继承关系来自 AST，可靠。
- 额外增加了 `symbols.qualified_name`、`chunk_metadata.symbol_id`、`raw_refs`、`chunks_fts` 与 `meta` 表。

## 数据访问（SQL）

`data-access <数据表或存储过程>` 回答“谁写入 / 读取 / 执行 X”。数据来源：T-SQL（INSERT / UPDATE / DELETE / MERGE / TRUNCATE / SELECT INTO / FROM / JOIN / EXEC，含别名解析，排除 CTE、临时表、系统对象）、C# 的 SQL 字符串（字面值、`+`、字符串插值、`string.Format`、常量、局部变量、`StringBuilder.Append` / `AppendLine` / `AppendFormat`）经 Dapper、`SqlCommand` 或 EF `FromSql` 发出、配合 `CommandType.StoredProcedure` 的存储过程名称、对 `DbSet<T>` 成员的 EF LINQ（`ctx.Orders.Where(..)` / `.Add(..)` / `.Remove(..)`、`from o in ctx.Orders`；只比对成员名称，所以是 `probable`），以及 EF 的 `ToTable` / `[Table]` 映射。调用某存储过程的代码视为该过程所写数据表的间接写入者（最多追 4 层）。`programs`（USED BY PROGRAMS）沿 confirmed / probable 的 C# 调用往上追到 `*Controller` 方法（最多 8 层），（实现方法也会接到它所实现接口方法的调用者，涵盖 `foreach (var step in steps) step.Run()` 这种管线），按 controller 文件分组，并列出 `Url.Action("Action"[, "Controller"])` 或 AngularJS `ControllerPath + 'Action'`（controller 取自 `$scope.Controller = 'X'`）指向这些 action 的 Razor 页面（`.cshtml`），查一张表就知道哪些画面会动到它。方法读了某 DbSet 又调用 `SaveChanges`、但没有明确 `Add` / `Remove` 时，可能是在更新跟踪中的实体，列为可能（`uncertain`）写入者。每条都有置信等级；**动态 SQL（`EXEC(@sql)`、`sp_executesql`、由未知值组成的 SQL）无法解析，会单独列为 `uncertain`**，不会混进确定的清单。EF 访问在 context 变量（`using (var db = new X())`、字段或属性、`this`）的类型是声明或继承该 DbSet 的类时为 `confirmed`，否则 `probable`。往上追时没有任何 confirmed / probable 调用者的方法列在 NO KNOWN CALLER：可能是 API、计划任务、`Main` 等入口，或是调用图解不出的调用。已知缺口：`FROM a JOIN b ON ..., c` 会漏掉 `c`；把存储过程名称传给非数据库方法时看不到。

只存在数据库里的存储过程，可导出到仓库外的文件夹，再写进 `.ai_context/extra_roots`（一行一个文件夹，绝对路径或相对于项目根目录）；这些文件以绝对路径索引。

自然语言搜索遇到“谁修改 MES_ScrapRecord”“哪些程序会写入 MES_Lot”这类问题，会先走这个索引。

## 查询扩展

查询会用小型同义词字典扩展（`SP` / `预存程序` / `stored procedure` / `usp`、数据表、批号、报废等；见 `local_codebase_indexer/synonyms.py`）。可在 `.ai_context/synonyms.json` 为项目新增或停用分组：`{"groups": [["wpnlot", "在制批"]], "disable": ["sp"]}`。这对缩写与中英对照有帮助，对自由改写的同义说法没用。

索引也会自动挖项目自己的词典：符号正上方注释里的中文双字词，配对符号名称的英文子词（`ReceiveLotAsync` 上面写 `/// 收帳` → 收帳 = receive）。至少 5 个符号支持、在带有该词注释的符号中占 15% 以上（有平滑）、且该英文词在那里出现的概率是整体的 15 倍以上才保留。在 3 万 chunk 的 C# 项目上约得到 350 组（收帳→receive、拆批→divide、油墨→ink、底片→film），换个说法的查询 MRR 由 0.57 提升到 0.64。

## 索引健康状态

`status`（也是 MCP 工具 `codebase_index_status`）不更新索引，只报告索引是否可信：`fresh`（新鲜）、`stale`（文件有变更或 git HEAD 已移动）、`degraded`（有解析失败的文件或数据库不一致）、`not_indexed`（尚未建立）。解析失败的文件会从索引移除、连同错误原因列出，并在每次更新时重试，所以搜索不会返回它的过期内容。`index --force` 可全部重建。status 也会显示运行中的版本；包的代码在进程启动后有改动时会提示重启 MCP server（也就是开新的 Claude Code session）。

速度（2.3k 文件、3 万 chunk、hashing 嵌入）：空数据库全量约 36 秒，切分支改写 400 个文件约 10 秒，无变动检查约 1 秒。每个文件在 savepoint 内写入（不可分割），每 500 个文件 commit 一次。

## 基准测试

`python benchmarks/run_benchmark.py` 用固定问题度量 Recall@5、Recall@10 与 MRR，详见 [`benchmarks/README.md`](../benchmarks/README.md)（英文）。

## 测试

```bash
python -m pytest -q
```
