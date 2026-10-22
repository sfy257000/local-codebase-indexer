import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from indexer import dataflow  # noqa: E402
from indexer.query import CodebaseTools  # noqa: E402
from indexer.sqlaccess import extract_sql_accesses  # noqa: E402
from indexer.synonyms import Expander  # noqa: E402


def acc(sql):
    return {(a.name.lower(), a.access, a.op, a.confidence) for a in extract_sql_accesses(sql)}


def test_sql_read_write_and_exec():
    got = acc("""
        INSERT INTO dbo.MES_A (x) SELECT x FROM MES_B b JOIN MES_C c ON c.id = b.id;
        UPDATE t SET v = 1 FROM MES_D t WHERE t.id = 1;
        DELETE FROM MES_E;
        EXEC dbo.usp_Other @p = 1;
    """)
    assert ("mes_a", "write", "insert", "confirmed") in got
    assert ("mes_b", "read", "select", "confirmed") in got
    assert ("mes_c", "read", "select", "confirmed") in got
    assert ("mes_d", "write", "update", "confirmed") in got  # alias resolved
    assert ("mes_e", "write", "delete", "confirmed") in got
    assert any(n == "usp_other" and a == "exec" for n, a, _, _ in got)


def test_sql_ignores_comments_strings_params_temp_tables_and_ctes():
    got = acc("""
        -- INSERT INTO Commented
        DECLARE @From datetime, @msg nvarchar(50) = 'DELETE FROM InString';
        WITH cte AS (SELECT 1 AS n) SELECT n FROM cte;
        SELECT * INTO #tmp FROM MES_Real;
        SELECT * FROM #tmp;
    """)
    names = {n for n, *_ in got}
    assert names == {"mes_real"}


def test_dynamic_sql_is_uncertain():
    got = acc("DECLARE @s nvarchar(max) = N'SELECT 1'; EXEC(@s);")
    assert any(op == "dynamic" and conf == "uncertain" for _, _, op, conf in got)


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    dst = tmp_path_factory.mktemp("corp") / "c"
    shutil.copytree(os.path.join(ROOT, "benchmarks", "corpus"), dst)
    return CodebaseTools(str(dst))


def names(entries):
    return {e["source"].split(".", 1)[-1] if "." in e["source"] else e["source"] for e in entries}


def test_table_writers_include_indirect_via_sp(tools):
    res = tools.data_access("MES_Lot")
    w = {e["source"] for e in res["writers"]}
    assert any(s.endswith("LotRepository.Upsert") for s in w)
    assert any(s.endswith("usp_Lot_UpdateStatus") for s in w)
    indirect = [e for e in res["writers"] if e["depth"] > 0]
    assert any(e["source"].endswith("LotRepository.UpdateStatus") and e["via_chain"] for e in indirect)
    assert res["mapped_by"]


def test_sp_is_executed_by_csharp_and_touches_table(tools):
    res = tools.data_access("usp_Scrap_Insert")
    assert any(e["source"].endswith("ScrapRepository.Insert") for e in res["executed_by"])
    assert any(e["target"].lower().endswith("mes_scraprecord") for e in res["touches"]["writes"])


def test_unknown_object_gives_suggestions(tools):
    res = tools.data_access("MES_Scra")
    assert res["found"] is False and any("scrap" in s.lower() for s in res["suggestions"])


def test_intent_detection_and_routing(tools):
    assert dataflow.detect_intents("who modifies MES_ScrapRecord") & {"write"}
    assert "write" in dataflow.detect_intents("哪些程式會寫入 MES_Lot")
    top = tools.semantic_search("哪些程式會寫入 MES_Lot", limit=5)["results"]
    got = " ".join(r["file_path"] + (r.get("symbol") or "") for r in top)
    assert "usp_Lot_UpdateStatus" in got and "LotRepository" in got


def test_callers_are_resolved_with_confidence_not_by_name(tools):
    res = tools.symbol_relations("Save", "upstream")
    callers = {d["qualified_name"]: d["upstream"]["callers"] for d in res["definitions"]}
    iface = callers["Acme.Mes.Services.IScrapService.Save"]
    assert [c["symbol"] for c in iface] == ["Acme.Mes.Controllers.ScrapController.Create"]
    assert iface[0]["confidence"] == "confirmed"
    # same-named methods of unrelated types must not inherit that caller
    assert callers["Acme.Mes.Services.LotService.Save"] == []
    assert callers["Acme.Mes.Services.InspectionService.Save"] == []
    impl = callers["Acme.Mes.Services.ScrapService.Save"]
    assert impl and all(c["confidence"] != "confirmed" for c in impl)  # reached through the interface


def test_synonym_expansion_and_project_override(tmp_path):
    e = Expander()
    assert {"procedure", "proc"} <= set(e.expand("SP that inserts scrap records"))
    assert "scrap" in e.expand("預存程序 新增報廢")
    d = tmp_path / ".ai_context"
    d.mkdir()
    (d / "synonyms.json").write_text('{"groups": [["wpnlot", "在製批"]], "disable": ["sp"]}', encoding="utf-8")
    e2 = Expander(str(tmp_path))
    assert "wpnlot" in e2.expand("在製批 查詢")
    assert "proc" not in e2.expand("SP list")
