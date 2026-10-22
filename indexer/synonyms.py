"""Cheap domain-term query expansion.

A query that mentions one word of a synonym group also searches the other words of that group, so "SP" finds
``usp_*`` / ``proc`` code and "預存程序" finds English identifiers. Groups are plain lists: the first entry is
the canonical English word. A project can extend or override the built-in groups with
``<root>/.ai_context/synonyms.json``: ``{"groups": [["scrap", "報廢", "ng"]], "disable": ["sp"]}``.

Expansion only adds terms to the keyword / symbol layers and to the text that is embedded; it never removes terms.
Single-word (ASCII) triggers must match a whole word, CJK triggers match as substrings.
"""

from __future__ import annotations

import json
import os
import re

from .db import index_dir

# Generic software + database terms (English <-> zh-TW / zh-CN), then a few common manufacturing words.
DEFAULT_GROUPS: list[list[str]] = [
    ["procedure", "sp", "proc", "usp", "stored", "預存程序", "預儲程序", "儲存程序", "存储过程", "预存程序"],
    ["table", "資料表", "數據表", "数据表", "表格", "資料庫表"],
    ["view", "檢視表", "視圖", "视图"],
    ["column", "field", "欄位", "字段", "栏位"],
    ["database", "db", "資料庫", "数据库"],
    ["query", "select", "查詢", "查询", "撈資料"],
    ["insert", "add", "create", "新增", "建立", "添加", "创建"],
    ["update", "modify", "change", "更新", "修改", "變更", "变更", "異動"],
    ["delete", "remove", "刪除", "删除", "移除"],
    ["save", "persist", "store", "儲存", "保存", "存檔"],
    ["login", "signin", "authenticate", "auth", "登入", "登录", "驗證", "验证", "認證"],
    ["password", "pwd", "密碼", "密码"],
    ["user", "account", "使用者", "用戶", "用户", "帳號", "账号"],
    ["permission", "role", "權限", "权限", "角色"],
    ["config", "configuration", "settings", "設定", "配置", "组态"],
    ["connection", "connectionstring", "連線", "连接", "连线"],
    ["error", "exception", "failure", "錯誤", "错误", "例外", "異常"],
    ["log", "logging", "紀錄", "記錄", "日誌", "日志"],
    ["cache", "快取", "缓存"],
    ["report", "報表", "报表"],
    ["print", "printer", "列印", "打印", "印表機", "打印机"],
    ["label", "tag", "標籤", "标签"],
    ["export", "匯出", "导出"],
    ["import", "匯入", "导入"],
    ["test", "unittest", "測試", "测试"],
    # manufacturing
    ["lot", "batch", "批號", "批次", "批号"],
    ["scrap", "報廢", "报废"],
    ["inspection", "inspect", "檢驗", "檢查", "检验", "检查"],
    ["defect", "defective", "缺點", "缺陷", "不良"],
    ["yield", "良率"],
    ["workorder", "工單", "工单"],
    ["equipment", "machine", "device", "設備", "机台", "機台", "设备"],
    ["status", "state", "狀態", "状态"],
]

_WORD = re.compile(r"[A-Za-z0-9_]+")


def _load_project(root: str | None) -> tuple[list[list[str]], set[str]]:
    if not root:
        return [], set()
    path = os.path.join(index_dir(root), "synonyms.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        groups = [[str(w) for w in g] for g in data.get("groups", []) if isinstance(g, list)]
        return groups, {str(w).lower() for w in data.get("disable", [])}
    except FileNotFoundError:
        return [], set()
    except (OSError, ValueError, AttributeError) as exc:
        import sys
        print(f"[indexer] ignoring {path}: {exc}", file=sys.stderr)
        return [], set()


class Expander:
    def __init__(self, root: str | None = None):
        extra, disabled = _load_project(root)
        self.groups = [[w.lower() for w in g] for g in DEFAULT_GROUPS + extra]
        self.disabled = disabled

    def expand(self, query: str) -> list[str]:
        """Extra terms (not in the query yet) for every synonym group the query triggers."""
        low = query.lower()
        words = set(_WORD.findall(low))
        words |= {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}  # inserts -> insert
        extra: list[str] = []
        for g in self.groups:
            hit = [w for w in g if w not in self.disabled and
                   ((w in words) if w.isascii() else (w in low))]
            if not hit:
                continue
            for w in g:
                if w in self.disabled or w in hit or w in extra:
                    continue
                if w.isascii() and w in words:
                    continue
                extra.append(w)
        return extra
