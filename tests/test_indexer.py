import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from indexer.mcp_server import handle  # noqa: E402
from indexer.query import CodebaseTools  # noqa: E402

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
    assert names == ["codebase_semantic_search", "codebase_get_symbol_relations", "codebase_get_optimized_context"]
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
