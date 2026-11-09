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
import hashlib
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
    ["password", "pwd", "passphrase", "secret", "密碼", "密码"],
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

# General paraphrases help keyword/symbol recall without diluting vector queries.
GENERAL_GROUPS = [
    ["read", "fetch", "retrieve", "load", "讀取", "读取", "取得", "載入", "加载"],
    ["validate", "verify", "check", "檢核", "校驗", "校验"],
    ["calculate", "compute", "計算", "计算"],
    ["send", "transmit", "dispatch", "傳送", "发送", "發送"],
    ["cancel", "abort", "取消", "中止"],
    ["split", "divide", "partition", "separate", "分割", "拆分", "拆批"],
    ["merge", "combine", "合併", "合并"],
    ["serialize", "serialise", "marshal", "序列化"],
    ["deserialize", "deserialise", "unmarshal", "反序列化"],
]

_WORD = re.compile(r"[A-Za-z0-9_]+")


def _load_project(root: str | None, payload: bytes | None = None) -> tuple[list[list[str]], set[str]]:
    if not root:
        return [], set()
    path = os.path.join(index_dir(root), "synonyms.json")
    try:
        if payload is None:
            with open(path, 'rb') as stream:
                payload = stream.read()
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError('expected an object')
        groups, disabled = data.get('groups', []), data.get('disable', [])
        if not isinstance(groups, list) or any(not isinstance(g, list) or not g or
                any(not isinstance(word, str) or not word.strip() for word in g) for g in groups):
            raise ValueError('groups must contain nonempty lists of nonempty strings')
        if not isinstance(disabled, list) or any(not isinstance(word, str) for word in disabled):
            raise ValueError('disable must be a list of strings')
        return groups, {word.lower() for word in disabled}
    except FileNotFoundError:
        return [], set()
    except (OSError, ValueError, AttributeError) as exc:
        import sys
        print(f"[indexer] ignoring {path}: {exc}", file=sys.stderr)
        return [], set()


class Expander:
    def __init__(self, root: str | None = None):
        self.root = root
        self.version = ''
        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        payload = None
        try:
            with open(os.path.join(index_dir(self.root), 'synonyms.json'), 'rb') as stream:
                payload = stream.read()
                version = hashlib.sha256(payload).hexdigest()
        except FileNotFoundError:
            version = 'missing'
        except (OSError, TypeError):
            version = 'unreadable'
        if not force and version == self.version:
            return False
        extra, disabled = _load_project(self.root, payload) if payload is not None else ([], set())
        self.version = version
        self.project_groups = [[w.lower() for w in g] for g in extra]
        self.embedding_groups = [[w.lower() for w in g] for g in DEFAULT_GROUPS + extra]
        self.groups = self.embedding_groups + [[w.lower() for w in g] for g in GENERAL_GROUPS]
        self.disabled = disabled
        return True

    def expand(self, query: str, for_embedding: bool = False) -> list[str]:
        """Extra terms (not in the query yet) for every synonym group the query triggers."""
        low = query.lower()
        words = set(_WORD.findall(low))
        words |= {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}  # inserts -> insert
        extra: list[str] = []
        for g in (self.embedding_groups if for_embedding else self.groups):
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

    def sources(self, query: str, term: str) -> list[dict]:
        """Report the groups that actually caused an expansion, without inventing semantics."""
        low = query.lower()
        words = set(_WORD.findall(low))
        words |= {w[:-1] for w in words if len(w) > 3 and w.endswith('s')}
        out = []
        for source, groups in (('builtin', DEFAULT_GROUPS), ('general', GENERAL_GROUPS), ('project', self.project_groups)):
            for group in groups:
                group = [w.lower() for w in group]
                triggers = [w for w in group if w not in self.disabled and
                            ((w in words) if w.isascii() else (w in low))]
                if term in group and triggers:
                    out.append({'source': source, 'group': group[0], 'triggers': triggers})
        return out
