# Local Codebase Indexer（本機程式碼索引器）

[English](README.md) | **繁體中文** | [简体中文](README.zh-CN.md)

為 AI 程式助理（Claude Code 走 MCP，GitHub Copilot / Codex 走 CLI）打造的本機程式碼索引與 context 提供工具，讓它們在大型舊專案中用更少的 token 找到程式碼。索引全部存在專案下的 `.ai_context/indexer.db`，不連任何雲端服務。設計依據見 [`index.md`](index.md)。

## 安裝

```bash
pip install -r requirements.txt   # Python 3.10+
```

## 使用（CLI）

```bash
bin/indexer --root <專案路徑> index                      # 建立 / 增量更新索引（--force 全部重建）
bin/indexer search "database mapping for LotNo" --limit 5
bin/indexer relations IExistOpenHandleService --direction both   # upstream | downstream | both
bin/indexer context Services/LotService.cs --lines 120 --padding 15   # 別名：slice
bin/indexer data-access MES_Lot                           # 誰讀 / 寫 / 執行某資料表或預存程序；預存程序碰了哪些資料
bin/indexer stats
bin/indexer status                                       # 索引是否新鮮、失敗檔案、git commit、一致性檢查
```

- 不給 `--root` 時使用目前目錄，也可用環境變數 `INDEXER_ROOT`。
- 加 `--json`（放在子命令前）輸出 JSON。
- `search`、`relations`、`context` 執行前會自動做一次增量更新（先比 mtime，mtime 變了才算 md5）。
- 規格中的工具名稱也可直接當子命令：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`（參數 `--query`、`--symbol_name --relation_direction`、`--file_path --focus_lines --padding_lines`）。

## 使用（Claude Code / MCP）

```bash
claude mcp add local-indexer -- python /path/to/local-codebase-indexer/bin/indexer --root /path/to/project serve
```

`serve` 以 stdio 提供 JSON-RPC（MCP），暴露五個工具：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`、`codebase_get_data_access`、`codebase_index_status`。

## 三層索引

| 層 | 實作 |
|---|---|
| Syntactic | tree-sitter 解析 C#、Python、JavaScript、TypeScript/TSX；SQL 用 regex 抽 PROCEDURE / VIEW / FUNCTION / TABLE；其餘文字檔（VB6 `.frm`、`.config`、`.md` 等）切成視窗區塊 |
| Topology | `using` / `import` 解析成檔案相依；繼承（base list）；型別使用（`invocation`）。存在 `dependencies` 表 |
| Semantic | 區塊嵌入 384 維，存 `vss_chunks`；查詢時以 FTS5 關鍵字、符號名稱、向量三路做 Reciprocal Rank Fusion，再展開前 3 名檔案的相依鄰居 |

編碼：先試 UTF-8，再試 Big5、cp1252。單檔超過 5MB 不索引；遵守根目錄與子目錄的 `.gitignore`；固定略過 `.git`、`bin`、`obj`、`node_modules` 等。

## 與規格的差異

- **向量儲存**：規格的 `sqlite-vss` 需要原生擴充且已停止維護，這裡 `vss_chunks` 改存 float32 BLOB，用 numpy 暴力計算 cosine，不需要額外安裝擴充。
- **嵌入模型**：預設是內建的 hashing 嵌入（識別字拆詞、雙詞組、中文雙字），快速、可重現，但**不理解同義詞**。要用真正的 transformer，匯出 `all-MiniLM-L6-v2` 或 `bge-small-en-v1.5` 為 ONNX，裝 `onnxruntime`、`tokenizers`，並設 `INDEXER_ONNX_MODEL=<含 model.onnx 與 tokenizer.json 的資料夾>`（載入、補齊、池化與重建索引的邏輯已用一個小型合成模型測試過；真實模型的檢索品質尚未量測，設好變數後請執行 `python benchmarks/run_benchmark.py --compare benchmarks/baseline.json` 驗證；目前用平均池化，適合 MiniLM，bge 系列建議用 CLS 池化）。切換嵌入後會自動重建索引。
- **C# 呼叫關係**以有限度的型別推斷解析（using / namespace、欄位與建構式注入的型別、區域變數、介面對應實作），每條關係都標信心等級：`confirmed`（接收者型別已知）、`probable`（經由介面或唯一候選）、`candidate`（僅名稱相符）。沒有 Roslyn，所以不做依引數型別的多載解析、不處理委派 / lambda / 擴充方法，呼叫鏈只追一層。繼承關係來自 AST，可靠。
- 額外加了 `symbols.qualified_name`、`chunk_metadata.symbol_id`、`raw_refs`、`chunks_fts` 與 `meta` 表。

## 資料存取（SQL）

`data-access <資料表或預存程序>` 回答「誰寫入 / 讀取 / 執行 X」。資料來源：T-SQL（INSERT / UPDATE / DELETE / MERGE / TRUNCATE / SELECT INTO / FROM / JOIN / EXEC，含別名解析，排除 CTE、暫存表、系統物件）、C# 的 SQL 字串（字面值、`+`、字串內插、`string.Format`、常數、區域變數）經 Dapper、`SqlCommand` 或 EF `FromSql` 送出、搭配 `CommandType.StoredProcedure` 的 SP 名稱，以及 EF 的 `ToTable` / `[Table]` 對應。呼叫某 SP 的程式視為該 SP 所寫資料表的間接寫入者（最多追 4 層）。每筆都有信心等級；**動態 SQL（`EXEC(@sql)`、`sp_executesql`、由未知值組成的 SQL）無法解析，會獨立列為 `uncertain`**，不會混進確定的清單。已知缺口：`FROM a JOIN b ON ..., c` 會漏掉 `c`；把 SP 名稱傳給非資料庫方法時看不到。

自然語言搜尋遇到「誰修改 MES_ScrapRecord」「哪些程式會寫入 MES_Lot」這類問題，會先走這個索引。

## 查詢擴展

查詢會用小型同義詞字典擴展（`SP` / `預存程序` / `stored procedure` / `usp`、資料表、批號、報廢等；見 `indexer/synonyms.py`）。可在 `.ai_context/synonyms.json` 為專案新增或停用群組：`{"groups": [["wpnlot", "在製批"]], "disable": ["sp"]}`。這對縮寫與中英對照有幫助，對自由改寫的同義說法沒用。

## 索引健康狀態

`status`（也是 MCP 工具 `codebase_index_status`）不更新索引，只回報索引是否可信：`fresh`（新鮮）、`stale`（檔案有變更或 git HEAD 已移動）、`degraded`（有解析失敗的檔案或資料庫不一致）、`not_indexed`（尚未建立）。解析失敗的檔案會從索引移除、連同錯誤原因列出，並在每次更新時重試，所以搜尋不會回傳它的過期內容。`index --force` 可全部重建。

## 基準測試

`python benchmarks/run_benchmark.py` 以固定問題量測 Recall@5、Recall@10 與 MRR，詳見 [`benchmarks/README.md`](benchmarks/README.md)（英文）。

## 測試

```bash
python -m pytest -q
```
