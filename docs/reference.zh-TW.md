# Local Codebase Indexer（本機程式碼索引器）

[English](../README.md) | **繁體中文** | [简体中文](../README.zh-CN.md)

為 AI 程式助理（Claude Code 走 MCP，GitHub Copilot / Codex 走 CLI）打造的本機程式碼索引與 context 提供工具，讓它們在大型舊專案中用更少的 token 找到程式碼。索引全部存在專案下的 `.ai_context/indexer.db`，不連任何雲端服務。設計依據見 [`index.md`](../index.md)。

## 安裝

```bash
pip install local-codebase-indexer              # Python 3.10+
# 或與其他套件隔離安裝：
pipx install local-codebase-indexer
# 選用：真正的嵌入模型（見下文）pip install "local-codebase-indexer[onnx]"
```

從原始碼：`pip install -e .`（在 clone 下 `bin/indexer` 仍可使用）。

### 當作 Claude Code plugin 安裝

```text
/plugin marketplace add sfy257000/local-codebase-indexer
/plugin install local-codebase-indexer@local-codebase-indexer
```

會替你開啟的專案註冊 MCP 伺服器（實際執行 `uvx local-codebase-indexer --root <專案> serve`，所以要先裝 [uv](https://docs.astral.sh/uv/)：`pip install uv`）。安裝後重開 Claude Code。

## 使用（CLI）

```bash
local-codebase-indexer --root <專案路徑> index                      # 建立 / 增量更新索引（--force 全部重建）
local-codebase-indexer search "database mapping for LotNo" --limit 5
local-codebase-indexer relations IExistOpenHandleService --direction both   # upstream | downstream | both
local-codebase-indexer context Services/LotService.cs --lines 120 --padding 15   # 別名：slice
local-codebase-indexer data-access MES_Lot                           # 誰讀 / 寫 / 執行某資料表或預存程序；預存程序碰了哪些資料（--full 列出全部）
local-codebase-indexer impact main...HEAD                            # 一次變更動到的符號、資料表、程式與頁面（不給參數＝未提交的變更）
local-codebase-indexer stats
local-codebase-indexer status                                       # 索引是否新鮮、失敗檔案、git commit、一致性檢查
```

- 不給 `--root` 時使用目前目錄，也可用環境變數 `INDEXER_ROOT`。
- 加 `--json`（可放在子命令前後）輸出 JSON。
- `search`、`relations`、`context` 執行前會自動做一次增量更新（先比 mtime，mtime 變了才算 md5）。
- 規格中的工具名稱也可直接當子命令：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`（參數 `--query`、`--symbol_name --relation_direction`、`--file_path --focus_lines --padding_lines`）。

## 使用（Claude Code / MCP）

```bash
claude mcp add local-indexer -- local-codebase-indexer --root /path/to/project serve
# 不安裝直接跑：claude mcp add local-indexer -- uvx local-codebase-indexer --root /path/to/project serve
```

`serve` 以 stdio 提供 JSON-RPC（MCP），暴露七個工具：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`、`codebase_get_data_access`、`codebase_impact_analysis`、`codebase_index_status`、`codebase_index_doctor`。長駐的 server 在距離上次檢查超過 15 秒時，查詢前會重新檢查索引（幾千個檔案無變動約 1 秒）。`codebase_get_data_access` 預設回摘要（每區 5 筆、程式全列），加 `detail` 才列全部；`codebase_semantic_search` 加 `related` 才附相依檔案。

## 0.7.0：穩定導覽與專案評測

- 搜尋排序不再隨頁數或每頁筆數改變。JSON 的 `pagination.next_cursor` 可傳給 `--cursor`／MCP `cursor`；查詢與篩選須保持相同，每頁筆數可改變。索引、模型或同義詞設定改變時，游標會要求重新搜尋；舊有 offset 仍可用，但沒有版本保護。
- `search --explain`／MCP `explain=true` 顯示命中詞、同義詞來源、各層排序貢獻，以及資料存取的 SQL 證據與可信度。分數不是機率，靜態分析也不代表執行時必然經過該路徑。
- 同義詞設定會在後續查詢重新載入。ONNX 模型、tokenizer、外部權重與設定會納入指紋，內容改變後重建索引；請使用專用模型目錄。大型 context 請求優先保留焦點行，並回報省略／裁切資訊。
- `impact A..B`／`A...B` 分析指定的兩個 Git 版本，不會混入 HEAD 或未提交的內容；`snapshot=after` 行號屬於區間終點版本。
- 多程序共用索引時會協調寫入與一致性讀取。忙碌時 CLI 回傳結束碼 4；`INDEXER_LOCK_TIMEOUT`／`INDEXER_BUSY_TIMEOUT` 分別控制鎖定與 SQLite 等待秒數。中斷的批次索引會被偵測並在下次索引時重建；不要刪除使用中的鎖定檔。

將真實使用問題與人工確認的答案放在 `.ai_context/eval.json`：

```json
{"version":1,"queries":[{"id":"invoice","query":"在哪裡讀取發票？","relevant":[{"file":"src/invoices.py","symbol":"load_invoice"}]}]}
```

```bash
local-codebase-indexer --root <專案> evaluate --queries <專案>/.ai_context/eval.json --save <專案>/.ai_context/baseline.json
local-codebase-indexer --root <專案> evaluate --queries <專案>/.ai_context/eval.json --baseline <專案>/.ai_context/baseline.json --min-recall 0.8 --max-recall-drop 0.02 --max-p95-ms 500 --max-mean-tokens 1500
```

評測追蹤 Recall@k、MRR、命中率、延遲與估計 token；token 使用字元數除以 4 的估算。空題目集或錯誤資料不會算成功，未達門檻回傳結束碼 3。`benchmarks/queries_project.json` 提供本儲存庫的 15 題導覽範例，並非獨立或實際使用者收集的評測；請以自己的真實問題建立品質門檻。

後續 GitHub Release 會自動完成測試、一次建置、版本檢查、PyPI 發布及附件／SHA256SUMS 上傳。附件使用 GitHub Actions 內建權杖，重跑時略過相同檔案，遇到不同校驗值則拒絕覆寫；修改 workflow 本身不會變更現有 Release。

## 診斷、篩選與分頁（0.6.0）

```bash
local-codebase-indexer --root <專案> doctor --path src/Service.cs
local-codebase-indexer --root <專案> search "讀取發票" --file '*.py' --language python --limit 5 --offset 0
local-codebase-indexer --root <專案> data-access MES_Lot --confidence confirmed --limit 20 --offset 20
local-codebase-indexer --root <專案> data-access MES_Lot --full
```

- `doctor` 顯示索引範圍、忽略項目、不支援的副檔名、過大／二進位檔案與索引狀態；`--path` 可查單一檔案的排除原因。排除目錄的數量不代表其中的檔案數量。
- 搜尋先套用檔案與語言篩選再排序。`--file` 是完整相對路徑的 glob，區分大小寫，`*` 可跨 `/`。JSON 的 `pagination.next_offset` 提供下一頁；翻頁時保留相同查詢與篩選，並避免在中途修改索引。
- `data-access` 的 `--confidence` 精確選取 `confirmed`／`probable`／`uncertain`；分頁對每個區段各自套用，JSON 提供各區總數與下一個 offset。`--full`／MCP `detail=true` 不再受舊有 40／200 筆上限限制。
- MCP 搜尋參數為 `file_pattern`、`language`、`limit`、`offset`；資料存取工具支援 `confidence`、`limit`、`offset`。新增 `codebase_index_doctor` 診斷工具。
- `impact` 同時分析修改前後的程式，包含刪除項目及舊有呼叫／資料表影響；`snapshot=before` 的行號屬於基準版本。首次會建立歷史索引，之後重用 `.ai_context/impact/` 快取；此目錄可安全刪除以釋放空間。歷史分析不重建 Git 之外的 `extra_roots`，既有靜態分析限制仍適用。
- 不存在的根目錄會報錯，不會自動建立；空索引或有失敗項目的 `index` 回傳結束碼 3。MCP 收到錯誤參數後仍可繼續處理下一個請求。
- 擴充一般中英文動詞與密碼用語；專案術語仍建議放入 `.ai_context/synonyms.json`。這是詞彙搜尋改善，並不保證能理解所有自然語句。

## 三層索引

| 層 | 實作 |
|---|---|
| Syntactic | tree-sitter 解析 C#、Python、JavaScript、TypeScript/TSX；SQL 用 regex 抽 PROCEDURE / VIEW / FUNCTION / TABLE；其餘文字檔（VB6 `.frm`、`.config`、`.md` 等）切成視窗區塊 |
| Topology | `using` / `import` 解析成檔案相依；繼承（base list）；型別使用（`invocation`）。存在 `dependencies` 表 |
| Semantic | 區塊嵌入 384 維，存 `vss_chunks`；查詢時以 FTS5 關鍵字、符號名稱、向量三路做 Reciprocal Rank Fusion，再展開前 3 名檔案的相依鄰居 |

編碼：先試 UTF-8，再試 Big5、cp1252。單檔超過 5MB 不索引；遵守根目錄與子目錄的 `.gitignore`；固定略過 `.git`、`bin`、`obj`、`node_modules` 等。`*.min.js` / `*.min.css` 也略過。已 commit 進 repo 的第三方或產生的程式可用 `.ai_context/ignore`（gitignore 語法）排除，例如 `Scripts/*`、`!Scripts/Custom/`、`Web References/`（EF database-first 產生的 model 不要排除，DbSet 宣告在它的 DbContext 裡）。

## 與規格的差異

- **向量儲存**：規格的 `sqlite-vss` 需要原生擴充且已停止維護，這裡 `vss_chunks` 改存 float32 BLOB，用 numpy 暴力計算 cosine，不需要額外安裝擴充。
- **嵌入模型**：預設是內建的 hashing 嵌入（識別字拆詞、雙詞組、中文雙字），快速、可重現，但**不理解同義詞**。要用真正的 transformer，匯出 `all-MiniLM-L6-v2` 或 `bge-small-en-v1.5` 為 ONNX，裝 `onnxruntime`、`tokenizers`，並設 `INDEXER_ONNX_MODEL=<含 model.onnx 與 tokenizer.json 的資料夾>`（目前用平均池化，適合 MiniLM，bge 系列建議用 CLS 池化）。切換嵌入後會自動重建索引。實測（0.4.0）：`all-MiniLM-L6-v2` 讓 benchmark MRR 提高 0.08–0.13，但在真實的 3 萬 chunk C# / 中文註解專案上，換句話說的查詢反而比 hashing 差（MRR 0.29 對 0.48；多語 MiniLM-L12 為 0.41），完整建索引從 2 分鐘變成 35–43 分鐘。因此預設維持 hashing；領域行話用 `.ai_context/synonyms.json` 比通用模型有效。
- **C# 呼叫關係**以有限度的型別推斷解析（using / namespace、欄位與建構式注入的型別、`Lazy<T>.Value`、區域變數、`foreach (var x in items.Where(..))` 的元素型別、介面對應實作），每條關係都標信心等級：`confirmed`（接收者型別已知）、`probable`（經由介面或唯一候選）、`candidate`（僅名稱相符）。沒有 Roslyn，所以不做依引數型別的多載解析、不處理委派 / lambda / 擴充方法，呼叫鏈只追一層。繼承關係來自 AST，可靠。
- 額外加了 `symbols.qualified_name`、`chunk_metadata.symbol_id`、`raw_refs`、`chunks_fts` 與 `meta` 表。

## 資料存取（SQL）

`data-access <資料表或預存程序>` 回答「誰寫入 / 讀取 / 執行 X」。資料來源：T-SQL（INSERT / UPDATE / DELETE / MERGE / TRUNCATE / SELECT INTO / FROM / JOIN / EXEC，含別名解析，排除 CTE、暫存表、系統物件）、C# 的 SQL 字串（字面值、`+`、字串內插、`string.Format`、常數、區域變數、`StringBuilder.Append` / `AppendLine` / `AppendFormat`）經 Dapper、`SqlCommand` 或 EF `FromSql` 送出、搭配 `CommandType.StoredProcedure` 的 SP 名稱、對 `DbSet<T>` 成員的 EF LINQ（`ctx.Orders.Where(..)` / `.Add(..)` / `.Remove(..)`、`from o in ctx.Orders`；只比對成員名稱，所以是 `probable`），以及 EF 的 `ToTable` / `[Table]` 對應。呼叫某 SP 的程式視為該 SP 所寫資料表的間接寫入者（最多追 4 層）。`programs`（USED BY PROGRAMS）沿 confirmed / probable 的 C# 呼叫往上追到 `*Controller` 方法（最多 8 層），（實作方法也會接到它所實作介面方法的呼叫者，涵蓋 `foreach (var step in steps) step.Run()` 這種管線），依 controller 檔案分組，並列出 `Url.Action("Action"[, "Controller"])` 或 AngularJS `ControllerPath + 'Action'`（controller 取自 `$scope.Controller = 'X'`）指向這些 action 的 Razor 頁面（`.cshtml`），查一張表就知道哪些畫面會動到它。方法讀了某 DbSet 又呼叫 `SaveChanges`、但沒有明確 `Add` / `Remove` 時，可能是在更新追蹤中的實體，列為可能（`uncertain`）寫入者。每筆都有信心等級；**動態 SQL（`EXEC(@sql)`、`sp_executesql`、由未知值組成的 SQL）無法解析，會獨立列為 `uncertain`**，不會混進確定的清單。EF 存取在 context 變數（`using (var db = new X())`、欄位或屬性、`this`）的型別是宣告或繼承該 DbSet 的類別時為 `confirmed`，否則 `probable`。往上追時沒有任何 confirmed / probable 呼叫者的方法列在 NO KNOWN CALLER：可能是 API、排程、`Main` 等入口，或是呼叫圖解不出的呼叫。已知缺口：`FROM a JOIN b ON ..., c` 會漏掉 `c`；把 SP 名稱傳給非資料庫方法時看不到。

只存在資料庫裡的預存程序，可匯出到 repo 外的資料夾，再寫進 `.ai_context/extra_roots`（一行一個資料夾，絕對路徑或相對於專案根目錄）；這些檔案以絕對路徑索引。

自然語言搜尋遇到「誰修改 MES_ScrapRecord」「哪些程式會寫入 MES_Lot」這類問題，會先走這個索引。

## 查詢擴展

查詢會用小型同義詞字典擴展（`SP` / `預存程序` / `stored procedure` / `usp`、資料表、批號、報廢等；見 `local_codebase_indexer/synonyms.py`）。可在 `.ai_context/synonyms.json` 為專案新增或停用群組：`{"groups": [["wpnlot", "在製批"]], "disable": ["sp"]}`。這對縮寫與中英對照有幫助，對自由改寫的同義說法沒用。

索引也會自動挖專案自己的字典：符號正上方註解裡的中文雙字詞，配對符號名稱的英文子詞（`ReceiveLotAsync` 上面寫 `/// 收帳` → 收帳 = receive）。至少 5 個符號支持、在帶有該詞註解的符號中占 15% 以上（有平滑）、且該英文詞在那裡出現的機率是整體的 15 倍以上才保留。在 3 萬 chunk 的 C# 專案上約得到 350 組（收帳→receive、拆批→divide、油墨→ink、底片→film），換句話說的查詢 MRR 由 0.57 提升到 0.64。

## 索引健康狀態

`status`（也是 MCP 工具 `codebase_index_status`）不更新索引，只回報索引是否可信：`fresh`（新鮮）、`stale`（檔案有變更或 git HEAD 已移動）、`degraded`（有解析失敗的檔案或資料庫不一致）、`not_indexed`（尚未建立）。解析失敗的檔案會從索引移除、連同錯誤原因列出，並在每次更新時重試，所以搜尋不會回傳它的過期內容。`index --force` 可全部重建。status 也會顯示執行中的版本；套件程式碼在程序啟動後有改動時會提示重開 MCP server（也就是開新的 Claude Code session）。

速度（2.3k 檔、3 萬 chunk、hashing 嵌入）：空資料庫全量約 36 秒，切分支改寫 400 個檔約 10 秒，無變動檢查約 1 秒。每個檔案在 savepoint 內寫入（不可分割），每 500 個檔案 commit 一次。

## 基準測試

`python benchmarks/run_benchmark.py` 以固定問題量測 Recall@5、Recall@10 與 MRR，詳見 [`benchmarks/README.md`](../benchmarks/README.md)（英文）。

## 測試

```bash
python -m pytest -q
```
