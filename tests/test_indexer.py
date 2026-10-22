import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from local_codebase_indexer.mcp_server import handle  # noqa: E402
from local_codebase_indexer.query import CodebaseTools  # noqa: E402

FILES = {
    "Models/Lot.cs": """using System;
namespace Acme.Models
{
    public class Lot
    {
        public string LotNo { get; set; }
    }
}
""",
    "Services/ILotService.cs": """using Acme.Models;
namespace Acme.Services
{
    public interface ILotService
    {
        Lot GetLot(string lotNo);
    }
}
""",
    "Services/LotService.cs": """using Acme.Models;
namespace Acme.Services
{
    public class LotService : ILotService
    {
        // Look up a manufacturing lot by lot number
        public Lot GetLot(string lotNo)
        {
            return null;
        }
    }
}
""",
    "Secret.cs": "class Secret { }\n",
    "tool.py": "import os\nfrom pkg.helper import run\n\nclass Base:\n    pass\n\nclass Child(Base):\n    def go(self):\n        return run()\n",
    "pkg/helper.py": "def run():\n    return 1\n",
    "web/app.ts": "import { Api } from './api';\nexport class App extends Api {\n  start() { return 1; }\n}\n",
    "web/api.ts": "export class Api {\n  call() { return 2; }\n}\n",
    "db/usp_Get.sql": "CREATE PROCEDURE dbo.usp_Get @a int\nAS\nSELECT 1\nGO\n",
    ".gitignore": "Secret.cs\n",
}


@pytest.fixture()
def project(tmp_path):
    for rel, content in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    t = CodebaseTools(str(tmp_path))
    t.refresh()
    yield tmp_path, t
    t.indexer.close()


def test_gitignore_respected(project):
    _, t = project
    paths = {r[0] for r in t.conn.execute("SELECT file_path FROM files")}
    assert "Secret.cs" not in paths
    assert "Services/LotService.cs" in paths


def test_symbols_extracted(project):
    _, t = project
    rows = {(r["symbol_type"], r["qualified_name"]) for r in t.conn.execute("SELECT * FROM symbols")}
    assert ("class", "Acme.Services.LotService") in rows
    assert ("interface", "Acme.Services.ILotService") in rows
    assert ("method", "Acme.Services.LotService.GetLot") in rows
    assert ("property", "Acme.Models.Lot.LotNo") in rows
    assert ("method", "Child.go") in rows
    assert ("procedure", "dbo.usp_Get") in rows


def test_dependency_graph(project):
    _, t = project
    deps = {tuple(r) for r in t.conn.execute("SELECT source_path, target_path, dependency_type FROM dependencies")}
    assert ("Services/LotService.cs", "Services/ILotService.cs", "inheritance") in deps
    assert ("Services/LotService.cs", "Models/Lot.cs", "import") in deps
    assert ("tool.py", "pkg/helper.py", "import") in deps
    assert ("web/app.ts", "web/api.ts", "import") in deps
    assert ("web/app.ts", "web/api.ts", "inheritance") in deps


def test_incremental_update_and_delete(project):
    root, t = project
    st = t.refresh()
    assert st.updated == 0 and st.added == 0 and st.unchanged == st.scanned

    f = root / "Services/LotService.cs"
    f.write_text(f.read_text(encoding="utf-8").replace("GetLot", "FetchLot"), encoding="utf-8")
    st = t.refresh()
    assert st.updated == 1
    names = {r[0] for r in t.conn.execute("SELECT symbol_name FROM symbols WHERE file_path='Services/LotService.cs'")}
    assert "FetchLot" in names and "GetLot" not in names

    # same content, new mtime -> md5 match, nothing re-indexed
    f.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(f, (1, 1))
    assert t.refresh().updated == 0

    (root / "web/api.ts").unlink()
    st = t.refresh()
    assert st.deleted == 1
    assert t.conn.execute("SELECT COUNT(*) FROM chunk_metadata WHERE file_path='web/api.ts'").fetchone()[0] == 0
    assert t.conn.execute("SELECT COUNT(*) FROM vss_chunks").fetchone()[0] == \
        t.conn.execute("SELECT COUNT(*) FROM chunk_metadata").fetchone()[0]


def test_semantic_search(project):
    _, t = project
    res = t.semantic_search("manufacturing lot lookup by number", limit=3)
    assert res["results"][0]["file_path"] == "Services/LotService.cs"
    assert res["results"][0]["symbol"].endswith("GetLot")


def test_symbol_relations(project):
    _, t = project
    res = t.symbol_relations("ILotService", "upstream")
    impl = res["definitions"][0]["upstream"]["inheritors_or_implementers"]
    assert [i["symbol"] for i in impl] == ["Acme.Services.LotService"]
    down = t.symbol_relations("LotService", "downstream")["definitions"][0]["downstream"]
    assert down["base_types"][0]["name"] == "ILotService"
    assert t.symbol_relations("Nope", "both")["definitions"] == []
    with pytest.raises(ValueError):
        t.symbol_relations("Lot", "sideways")


def test_optimized_context(project):
    _, t = project
    res = t.optimized_context("Services/LotService.cs", [8], 1)
    assert res["ranges"] == [[7, 9]]
    assert "public Lot GetLot" in res["context"]
    merged = t.optimized_context("Services/LotService.cs", [3, 5], 1)
    assert merged["ranges"] == [[2, 6]]
    with pytest.raises(ValueError):
        t.optimized_context("../outside.txt", [1])


def test_big5_file_is_indexed(tmp_path):
    (tmp_path / "legacy.frm").write_bytes("' 換線條碼解碼\nSub Main()\nEnd Sub\n".encode("big5"))
    t = CodebaseTools(str(tmp_path))
    t.refresh()
    res = t.semantic_search("換線條碼", limit=1)
    assert res["results"] and "換線條碼" in res["results"][0]["snippet"]
    t.indexer.close()


def test_mcp_protocol(project):
    _, t = project
    init = handle(t, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "local-codebase-indexer"
    assert handle(t, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [x["name"] for x in handle(t, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert names == ["codebase_semantic_search", "codebase_get_symbol_relations", "codebase_get_optimized_context",
                     "codebase_get_data_access", "codebase_index_status"]
    call = handle(t, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "codebase_semantic_search", "arguments": {"query": "GetLot", "limit": 2}}})
    assert call["result"]["isError"] is False and "GetLot" in call["result"]["content"][0]["text"]
    bad = handle(t, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}})
    assert bad["error"]["code"] == -32602


def test_cli_end_to_end(project):
    root, _ = project
    exe = os.path.join(ROOT, "bin", "indexer")
    out = subprocess.run([sys.executable, exe, "--root", str(root), "search", "LotService", "--limit", "1"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "LotService" in out.stdout
    out = subprocess.run([sys.executable, exe, "--root", str(root), "--json", "context", "Models/Lot.cs",
                          "--lines", "5", "--padding", "0"], capture_output=True, text=True)
    assert json.loads(out.stdout)["ranges"] == [[5, 5]]


# ---------------------------------------------------------------------- index status / failures

def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def test_status_fresh_then_stale_then_deleted(project):
    root, t = project
    st = t.index_status()
    assert st["state"] == "fresh" and st["consistency"]["ok"] and st["failed_files"] == []
    assert st["git"]["current"] is None  # tmp dir is not a git repo

    (root / "web/new.ts").write_text("export class N {}\n", encoding="utf-8")
    st = t.index_status()
    assert st["state"] == "stale" and st["pending_changes"]["new_files"]["examples"] == ["web/new.ts"]
    assert "web/new.ts" not in {r[0] for r in t.conn.execute("SELECT file_path FROM files")}  # status never refreshes

    t.refresh()
    (root / "web/new.ts").unlink()
    assert t.index_status()["pending_changes"]["deleted_files"]["examples"] == ["web/new.ts"]
    t.refresh()
    assert t.index_status()["state"] == "fresh"


def test_failed_file_leaves_no_stale_data_and_is_retried(project, monkeypatch):
    root, t = project
    import local_codebase_indexer.core as core

    real = core.parse_file

    def boom(language, text, path):
        if path == "Services/LotService.cs":
            raise RuntimeError("parser exploded")
        return real(language, text, path)

    f = root / "Services/LotService.cs"
    f.write_text(f.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
    monkeypatch.setattr(core, "parse_file", boom)
    st = t.refresh()
    assert st.failed == 1
    # old data of the failed file is gone: search must not return it
    assert t.conn.execute("SELECT COUNT(*) FROM chunk_metadata WHERE file_path='Services/LotService.cs'").fetchone()[0] == 0
    assert all(r["file_path"] != "Services/LotService.cs" for r in t.semantic_search("GetLot")["results"])
    status = t.index_status()
    assert status["state"] == "degraded"
    assert status["failed_files"][0]["file_path"] == "Services/LotService.cs"
    assert "parser exploded" in status["failed_files"][0]["error"]
    assert status["consistency"]["ok"]

    t.refresh()  # still failing: still reported, retried
    assert t.index_status()["failed_files"]

    monkeypatch.setattr(core, "parse_file", real)
    st = t.refresh()  # fixed: indexed again, failure cleared
    assert st.failed == 0
    status = t.index_status()
    assert status["state"] == "fresh" and status["failed_files"] == []
    assert any(r["file_path"] == "Services/LotService.cs" for r in t.semantic_search("GetLot")["results"])


def test_failure_cleared_when_file_deleted(project, monkeypatch):
    root, t = project
    import local_codebase_indexer.core as core
    monkeypatch.setattr(core, "parse_file", lambda *a: (_ for _ in ()).throw(ValueError("x")))
    (root / "web/bad.ts").write_text("export class B {}\n", encoding="utf-8")
    t.refresh()
    assert [f["file_path"] for f in t.index_status()["failed_files"]] == ["web/bad.ts"]
    (root / "web/bad.ts").unlink()
    t.refresh()
    assert t.index_status()["failed_files"] == []


def test_consistency_check_detects_corruption(project):
    _, t = project
    t.conn.execute("DELETE FROM vss_chunks WHERE rowid=(SELECT MIN(rowid) FROM vss_chunks)")
    t.conn.commit()
    st = t.index_status()
    assert st["state"] == "degraded" and not st["consistency"]["ok"]
    assert any("without a vector" in p for p in st["consistency"]["problems"])
    t.refresh(force=True)  # documented remedy
    assert t.index_status()["consistency"]["ok"]


def test_git_metadata_and_head_moved(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "one")
    t = CodebaseTools(str(tmp_path))
    t.refresh()
    st = t.index_status()
    first = st["git"]["indexed"]["commit"]
    assert len(first) == 40 and st["git"]["current"]["commit"] == first and st["state"] == "fresh"

    (tmp_path / "b.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "two")
    st = t.index_status()
    assert st["git"]["head_moved"] and st["state"] == "stale"
    t.refresh()
    st = t.index_status()
    assert st["git"]["indexed"]["commit"] != first and st["state"] == "fresh"
    t.indexer.close()


def test_index_version_mismatch_forces_rebuild(project):
    root, t = project
    t.conn.execute("UPDATE meta SET value='0' WHERE key='index_version'")
    t.conn.commit()
    st = t.refresh()
    assert st.added == st.scanned and st.unchanged == 0


def test_mcp_status_does_not_disable_auto_refresh(project):
    root, t = project
    handle(t, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "codebase_index_status", "arguments": {}}})
    (root / "web/late.ts").write_text("export class LateOne {}\n", encoding="utf-8")
    t._fresh = False
    res = handle(t, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "codebase_semantic_search", "arguments": {"query": "LateOne"}}})
    assert "web/late.ts" in res["result"]["content"][0]["text"]


# ---------------------------------------------------------------------- Chinese search / benchmark guard

def test_chinese_query_matches_inside_a_longer_run(tmp_path):
    (tmp_path / "a.cs").write_text("class A {\n  // 新增報廢資料，呼叫預存程序\n  void Insert() { }\n}\n", encoding="utf-8")
    (tmp_path / "b.cs").write_text("class B {\n  // 無關的說明文字\n  void Other() { }\n}\n", encoding="utf-8")
    t = CodebaseTools(str(tmp_path))
    t.refresh()
    # none of the query's runs equals a whole run in the file: only character bigrams can match
    res = t.semantic_search("報廢資料寫入", limit=1)
    assert res["results"][0]["file_path"] == "a.cs"
    t.indexer.close()


def test_benchmark_corpus_is_valid_and_floor_holds(tmp_path):
    out = tmp_path / "bench.json"
    run = subprocess.run([sys.executable, os.path.join(ROOT, "benchmarks", "run_benchmark.py"),
                          "--suite", "synthetic", "--save", str(out)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert "LINT:" not in run.stdout  # synonym queries must not share words with their answers
    cats = json.loads(out.read_text(encoding="utf-8"))["suites"]["synthetic"]["categories"]
    assert cats["exact_identifier"]["recall5"] == 1.0
    assert cats["chinese"]["recall5"] == 1.0


def test_git_output_is_decoded_as_utf8_not_the_locale(tmp_path, monkeypatch):
    """On Windows the locale codec (cp950) cannot decode git's UTF-8 output and the reader thread crashed."""
    import subprocess

    import local_codebase_indexer.core as core
    seen = {}
    real = subprocess.run

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    core.git_info(str(tmp_path))
    assert seen.get("encoding") == "utf-8" and seen.get("errors") == "replace"


def test_mcp_serve_reads_utf8_even_when_locale_is_cp950(project):
    q = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "codebase_semantic_search", "arguments": {"query": "批號 LotNo", "limit": 2}}}, ensure_ascii=False)
    env = dict(os.environ, PYTHONIOENCODING="cp950")
    r = subprocess.run([sys.executable, "-m", "local_codebase_indexer", "--root", str(project), "serve"],
                       input=(q + "\n").encode("utf-8"), capture_output=True, env=env, cwd=ROOT, timeout=60)
    out = json.loads(r.stdout.decode("utf-8"))
    assert "result" in out and "error" not in out, r.stderr.decode("utf-8", "replace")
