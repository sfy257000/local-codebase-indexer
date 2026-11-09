# 社群貼文草稿：尚未發送

適合允許作者分享工具的 C#／SQL Server 維護社群。發文前確認該社群的推廣規則，並保留作者身分揭露。

## 標題

從 SQL Server 資料表追到 C# 呼叫者與 Razor 頁面：本機 CLI／MCP 工具

## 內文

我是 Local Codebase Indexer 的作者。這是採用 MIT 授權的 Python 工具，想解決維護舊系統時常見的問題：「誰會寫入這張表？哪些 Controller Action 與頁面會走到它？」

針對 C#／SQL Server 原始碼，工具串接 T-SQL 資料存取、C# SQL／預存程序呼叫、帶型別線索的呼叫關係，以及 Controller／頁面連結。CLI 會列出檔案位置與信心等級，也能透過 MCP 提供同樣的證據給 AI 助手。

不用準備 SQL Server 或 .NET，先跑內建合成範例：

```bash
python -m pip install --upgrade local-codebase-indexer
local-codebase-indexer demo
```

範例會找出透過 `usp_ApproveOrder` 間接寫入的 Repository，再追到 `OrdersController` 與 `Views/Orders/Details.cshtml`。經過介面派送的 Controller 關係標為 `probable`。這個小範例四項檢查通過，不代表正式專案的準確率，也沒有宣稱省下多少時間。動態 SQL、多載、delegate 等情況仍有限制。

原始碼與 32 秒示範：https://github.com/sfy257000/local-codebase-indexer

逐段對照原始碼的案例與完整報告：https://github.com/sfy257000/local-codebase-indexer/blob/main/docs/case-study.md

索引留在專案目錄中。若接 AI 助手，回傳的程式片段送到哪裡仍由用戶端與模型供應商設定決定。
