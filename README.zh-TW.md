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
bin/indexer stats
```

- 不給 `--root` 時使用目前目錄，也可用環境變數 `INDEXER_ROOT`。
- 加 `--json`（放在子命令前）輸出 JSON。
- `search`、`relations`、`context` 執行前會自動做一次增量更新（先比 mtime，mtime 變了才算 md5）。
- 規格中的工具名稱也可直接當子命令：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`（參數 `--query`、`--symbol_name --relation_direction`、`--file_path --focus_lines --padding_lines`）。

## 使用（Claude Code / MCP）

```bash
claude mcp add local-indexer -- python /path/to/local-codebase-indexer/bin/indexer --root /path/to/project serve
```

`serve` 以 stdio 提供 JSON-RPC（MCP），暴露三個工具：`codebase_semantic_search`、`codebase_get_symbol_relations`、`codebase_get_optimized_context`。

## 三層索引

| 層 | 實作 |
|---|---|
| Syntactic | tree-sitter 解析 C#、Python、JavaScript、TypeScript/TSX；SQL 用 regex 抽 PROCEDURE / VIEW / FUNCTION / TABLE；其餘文字檔（VB6 `.frm`、`.config`、`.md` 等）切成視窗區塊 |
| Topology | `using` / `import` 解析成檔案相依；繼承（base list）；型別使用（`invocation`）。存在 `dependencies` 表 |
| Semantic | 區塊嵌入 384 維，存 `vss_chunks`；查詢時以 FTS5 關鍵字、符號名稱、向量三路做 Reciprocal Rank Fusion，再展開前 3 名檔案的相依鄰居 |

編碼：先試 UTF-8，再試 Big5、cp1252。單檔超過 5MB 不索引；遵守根目錄與子目錄的 `.gitignore`；固定略過 `.git`、`bin`、`obj`、`node_modules` 等。

## 與規格的差異

- **向量儲存**：規格的 `sqlite-vss` 需要原生擴充且已停止維護，這裡 `vss_chunks` 改存 float32 BLOB，用 numpy 暴力計算 cosine，不需要額外安裝擴充。
- **嵌入模型**：預設是內建的 hashing 嵌入（識別字拆詞、雙詞組、中文雙字），快速、可重現，但**不理解同義詞**。要用真正的 transformer，匯出 `all-MiniLM-L6-v2` 或 `bge-small-en-v1.5` 為 ONNX，裝 `onnxruntime`、`tokenizers`，並設 `INDEXER_ONNX_MODEL=<含 model.onnx 與 tokenizer.json 的資料夾>`（此路徑目前沒有實機驗證）。切換嵌入後會自動重建索引。
- **使用關係**是識別字名稱比對，不做型別解析；同名類別、方法可能混在一起。繼承關係則來自 AST。
- 額外加了 `symbols.qualified_name`、`chunk_metadata.symbol_id`、`raw_refs`、`chunks_fts` 與 `meta` 表。

## 測試

```bash
python -m pytest -q
```
