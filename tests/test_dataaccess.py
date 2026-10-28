import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from local_codebase_indexer import dataflow  # noqa: E402
from local_codebase_indexer.query import CodebaseTools  # noqa: E402
from local_codebase_indexer.sqlaccess import extract_sql_accesses  # noqa: E402
from local_codebase_indexer.synonyms import Expander  # noqa: E402


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


def test_programs_roll_up_controller_actions(tools):
    progs = {p["program"]: p for p in tools.data_access("MES_ScrapRecord")["programs"]}
    assert "Scrap" in progs and "write" in progs["Scrap"]["access"] and "Create" in progs["Scrap"]["actions"]


def test_relations_accepts_type_dot_method(tools):
    res = tools.symbol_relations("ScrapService.Save", "upstream")
    assert [d["qualified_name"] for d in res["definitions"]] == ["Acme.Mes.Services.ScrapService.Save"]
    full = tools.symbol_relations("Acme.Mes.Services.LotService.Save", "upstream")
    assert [d["qualified_name"] for d in full["definitions"]] == ["Acme.Mes.Services.LotService.Save"]


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


def test_bare_table_name_finds_its_repository_not_every_lot_class(tmp_path):
    """Real-world miss: searching SFC_MLot ranked OutsourceLot / PNLToPCSLot above SFC_MLotRepository because the
    sub-word 'lot' matched everything, and the table was spelled SFC_Mlot in the SQL string."""
    (tmp_path / "Repo").mkdir()
    (tmp_path / "Models").mkdir()
    (tmp_path / "Repo" / "SFC_MLotRepository.cs").write_text("""using System.Threading.Tasks;
using Dapper;
namespace E {
  public class SFC_MLotRepository : ISFC_MLotRepository {
    private readonly System.Data.IDbConnection _db;
    public Task<int> Deduct(decimal qty) {
        string sql = @" UPDATE SFC_Mlot SET Qty = Qty - @qty WHERE PostFlag = 'W'; DELETE FROM SFC_Mlot WHERE Qty <= 0;";
        return _db.ExecuteAsync(sql, new { qty });
    }
  }
  public interface ISFC_MLotRepository { Task<int> Deduct(decimal qty); }
}
""", encoding="utf-8")
    lots = "".join(f"public class Lot{i} : SFCLot {{ }} public class ReworkLot{i} : SFCLot {{ }}\n" for i in range(8))
    (tmp_path / "Models" / "SFCLot.cs").write_text(
        "namespace E { public partial class SFCLot { }\n" + lots + "public class OutsourceLot : SFCLot { } }", encoding="utf-8")
    t = CodebaseTools(str(tmp_path))
    for q in ("SFC_MLot", "SFC_Mlot", "sfc_mlot"):
        top = t.semantic_search(q, limit=5)["results"]
        assert top[0]["file_path"].endswith("SFC_MLotRepository.cs"), q
        assert sum(r["file_path"].endswith("SFC_MLotRepository.cs") for r in top) >= 3, q
        assert any((r.get("data_access") or {}).get("access") == "write" for r in top), q
    t.indexer.close()


SB_REPO = '''using System.Text;
namespace Acme.Repo
{
    public class MlotRepository
    {
        private readonly IDbContext _ctx;
        public int Insert(Lot lot)
        {
            StringBuilder sql = new StringBuilder();
            sql.Append(" INSERT INTO SFC_Mlot (LotId, Qty) ");
            sql.Append(" VALUES (@lotId, @qty) ");
            return _ctx.Execute(sql.ToString(), lot);
        }
        public Lot[] Get(string id)
        {
            var sb = new StringBuilder("SELECT * ");
            sb.AppendLine("FROM SFC_Mlot a").Append("JOIN SFC_Log b ON b.Id = a.Id ");
            string text = sb.ToString();
            return _ctx.Query<Lot>(text, new { id });
        }
        public void Twice()
        {
            var sb = new StringBuilder();
            sb.Append("DELETE FROM SFC_Old WHERE Id = @id");
            _ctx.Execute(sb.ToString(), null);
            sb.Clear();
            sb.AppendFormat("UPDATE {0} SET Qty = 0 WHERE Id = @id", "SFC_Cur");
            _ctx.Execute(sb.ToString(), null);
        }
    }
}
'''


def test_stringbuilder_sql_is_tracked(tmp_path):
    (tmp_path / "MlotRepository.cs").write_text(SB_REPO, encoding="utf-8")
    t = CodebaseTools(str(tmp_path))
    try:
        w = {(e["source"].rsplit(".", 1)[-1], e["op"], e["confidence"]) for e in t.data_access("SFC_Mlot")["writers"]}
        assert ("Insert", "insert", "confirmed") in w
        r = {(e["source"].rsplit(".", 1)[-1], e["confidence"]) for e in t.data_access("SFC_Mlot")["readers"]}
        assert ("Get", "confirmed") in r
        assert any(e["source"].endswith("Get") for e in t.data_access("SFC_Log")["readers"])
        assert any(e["op"] == "delete" for e in t.data_access("SFC_Old")["writers"])
    finally:
        t.indexer.close()


EF_FILES = {
    "Ctx.cs": '''namespace Acme { public class Ctx : DbContext {
    public virtual DbSet<CamLog> TCM_CamLog { get; set; }
    public virtual DbSet<Shipment> Shipments { get; set; }
} }
''',
    "Sub.cs": "namespace Acme { public class SubCtx : Ctx { } public class NotEf { } }\n",
    "Map.cs": '''namespace Acme { public class Model { void Build(DbModelBuilder mb) { mb.Entity<Shipment>().ToTable("MES_Ship"); } } }
''',
    "Ctl.cs": '''namespace Acme { public class Ctl {
    Ctx IMES = new Ctx();
    public void Save(CamLog log, Order o) {
        var old = IMES.TCM_CamLog.Where(a => a.Id == 1).ToList();
        IMES.TCM_CamLog.Add(log);
        o.Lines.Add(log);
        var q = from s in IMES.Shipments where s.Id == 1 select s;
    }
    NotEf other;
    public void Local() { using (var db = new SubCtx()) { db.TCM_CamLog.Where(a => a.Id == 2); } }
    public void Odd() { other.TCM_CamLog.Where(a => a.Id == 3); }
    public void Rename(int id) {
        var log = IMES.TCM_CamLog.Find(id);
        log.Name = "x";
        IMES.SaveChanges();
    }
} }
''',
}


def test_ef_linq_on_dbset_members_is_data_access(tmp_path):
    for rel, content in EF_FILES.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    t = CodebaseTools(str(tmp_path))
    try:
        res = t.data_access("TCM_CamLog")
        assert {(e["source"].rsplit(".", 1)[-1], e["op"]) for e in res["writers"]} == {("Save", "insert")}
        assert any(e["source"].endswith("Save") for e in res["readers"])
        conf = {e["source"].rsplit(".", 1)[-1]: e["confidence"] for e in res["readers"]}
        # field typed Ctx / local new SubCtx (derives from Ctx): confirmed; a non-context receiver stays probable
        assert (conf["Save"], conf["Local"], conf["Odd"]) == ("confirmed", "confirmed", "probable")
        assert t.data_access("Shipments")["found"] is False  # resolved to its mapped table name
        # Shipment is mapped to MES_Ship
        mapped = t.data_access("MES_Ship")["readers"]
        assert any(e["source"].endswith("Save") and e["via"] == "ef_linq" for e in mapped)
        # o.Lines is not a DbSet, raw candidates never surface
        assert t.data_access("Lines")["found"] is False
        # tracked entity + SaveChanges: a possible update, kept out of the writers
        possible = t.data_access("TCM_CamLog")["possible"]
        assert [(e["source"].rsplit(".", 1)[-1], e["op"]) for e in possible] == [("Rename", "update")]
    finally:
        t.indexer.close()


PIPE_FILES = {
    "Steps.cs": '''namespace Acme {
public interface IStep { void Process(int id); }
public class WriteLot : IStep { IDb _db;
    public void Process(int id) { _db.Execute("DELETE FROM MES_Pipe WHERE Id = @id", new { id }); } }
public class Other : IStep { public void Process(int id) { } }
public interface IWipApp { void Run(int id); }
public class WipApp : IWipApp { IStep[] _steps;
    public void Run(int id) { foreach (var s in _steps) s.Process(id); } }
}
''',
    "WK0001Controller.cs": '''using System;
namespace Acme { public class WK0001Controller { Lazy<IWipApp> _app;
    public void Save(int id) { _app.Value.Run(id); } } }
''',
}


def test_programs_follow_lazy_value_and_interface_pipelines(tmp_path):
    for rel, content in PIPE_FILES.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    t = CodebaseTools(str(tmp_path))
    try:
        callers = t.symbol_relations("IWipApp.Run", "upstream")["definitions"][0]["upstream"]["callers"]
        assert [(c["symbol"], c["confidence"]) for c in callers] == [("Acme.WK0001Controller.Save", "confirmed")]
        progs = t.data_access("MES_Pipe")["programs"]
        assert [(p["program"], p["access"], p["actions"]) for p in progs] == [("WK0001", ["write"], ["Save"])]
    finally:
        t.indexer.close()


def test_razor_page_url_action_links_to_controller_action(tmp_path):
    for rel, content in PIPE_FILES.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    page = tmp_path / "Views" / "WK0001" / "WK0001.cshtml"
    page.parent.mkdir(parents=True)
    page.write_text('<script>\nfetch(\'@Url.Action("Save")\', { method: "POST" });\n'
                    'var u = "@Url.Action("Run", "Missing")";\n</script>\n', encoding="utf-8")
    ng = tmp_path / "Views" / "Shared" / "Legacy.cshtml"  # AngularJS: controller named in the page, not the folder
    ng.parent.mkdir(parents=True)
    ng.write_text("$scope.Controller = 'WK0001';\n$http.post($scope.ControllerPath + 'Save', {});\n", encoding="utf-8")
    t = CodebaseTools(str(tmp_path))
    try:
        callers = t.symbol_relations("WK0001Controller.Save", "upstream")["definitions"][0]["upstream"]["callers"]
        assert sorted((c["file_path"], c["line"], c["confidence"]) for c in callers) == [
            ("Views/Shared/Legacy.cshtml", 2, "confirmed"), ("Views/WK0001/WK0001.cshtml", 2, "confirmed")]
        assert t.data_access("MES_Pipe")["programs"][0]["pages"] == [
            "Views/Shared/Legacy.cshtml", "Views/WK0001/WK0001.cshtml"]
    finally:
        t.indexer.close()


def test_program_code_plus_verb_finds_the_method_in_that_program(tmp_path):
    svc = "namespace E {{ public class {0}Service {{\n public void GetRecords() {{ }}\n public void SetRecord() {{ }}\n" \
          " public void DeleteRecord() {{ }}\n}} }}\n"
    (tmp_path / "Services").mkdir()
    for code in ("WD0096", "WD0001", "WD0002", "WD0003"):
        (tmp_path / "Services" / f"{code}Service.cs").write_text(svc.format(code), encoding="utf-8")
    (tmp_path / "WD0096Controller.cs").write_text(
        "namespace E { public class WD0096Controller { public void WD0096() { } } }\n", encoding="utf-8")
    (tmp_path / ".ai_context").mkdir()
    (tmp_path / ".ai_context" / "synonyms.json").write_text('{"groups": [["setrecord", "存檔"]]}', encoding="utf-8")
    t = CodebaseTools(str(tmp_path))
    try:
        top = t.semantic_search("WD0096 存檔", limit=3)["results"]
        assert top[0]["symbol"] == "E.WD0096Service.SetRecord"
    finally:
        t.indexer.close()


def test_extra_roots_index_exported_sql_outside_the_project(tmp_path):
    proj, sql = tmp_path / "proj", tmp_path / "sql_export"
    (proj / ".ai_context").mkdir(parents=True)
    sql.mkdir()
    (sql / "usp_X.sql").write_text("CREATE PROCEDURE usp_X AS\nUPDATE MES_X SET Qty = 0\nGO\n", encoding="utf-8")
    (proj / "Repo.cs").write_text('''using System.Data;
namespace E { public class Repo { IDbConnection _db;
    public void Run() { _db.Execute("usp_X", commandType: CommandType.StoredProcedure); } } }
''', encoding="utf-8")
    (proj / ".ai_context" / "extra_roots").write_text("# exported SPs\n../sql_export\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    t = CodebaseTools(str(proj))
    try:
        w = t.data_access("MES_X")["writers"]
        assert {e["source"].rsplit(".", 1)[-1] for e in w} >= {"usp_X", "Run"}  # Run: indirect through the SP
        sql_path = next(e["file_path"] for e in w if e["source"].endswith("usp_X"))
        assert sql_path == (sql / "usp_X.sql").as_posix()
        assert "UPDATE MES_X" in t.optimized_context(sql_path, [2], 1)["context"]
        with pytest.raises(ValueError):
            t.optimized_context(str(tmp_path / "secret.txt"), [1], 1)
    finally:
        t.indexer.close()


def test_data_access_summary_by_default_full_on_request(tools):
    from local_codebase_indexer.query import render_data_access
    res = tools.data_access("MES_ScrapRecord")
    e = res["writers"][0]
    res = {**res, "writers": [dict(e, line=i) for i in range(1, 9)]}
    short, full = render_data_access(res), render_data_access(res, full=True)
    assert "... 3 more (detail=true" in short and "note:" not in short
    assert "more" not in full.split("READ BY")[0] and "note:" in full
    assert "USED BY PROGRAMS" in short


def test_related_files_only_on_request(tools):
    assert tools.semantic_search("scrap record", limit=3)["related_files"] == []
    assert tools.semantic_search("scrap record", limit=3, related=True)["related_files"]


def test_paths_without_a_controller_list_their_root(tmp_path):
    for rel, content in PIPE_FILES.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    (tmp_path / "Job.cs").write_text(
        "namespace Acme { public class NightlyJob { WipApp _w;\n  public void Run() {\n    _w.Run(1);\n  }\n} }\n",
        encoding="utf-8")
    t = CodebaseTools(str(tmp_path))
    try:
        res = t.data_access("MES_Pipe")
        assert [p["program"] for p in res["programs"]] == ["WK0001"]
        assert [r["symbol"] for r in res["no_known_caller"]] == ["Acme.NightlyJob.Run"]
    finally:
        t.indexer.close()


def _git(root, *args):
    import subprocess
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_impact_maps_a_diff_to_symbols_tables_programs_and_pages(tmp_path):
    for rel, content in PIPE_FILES.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    page = tmp_path / "Views" / "WK0001" / "WK0001.cshtml"
    page.parent.mkdir(parents=True)
    page.write_text("<script>\nfetch('@Url.Action(\"Save\")');\n</script>\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    steps = tmp_path / "Steps.cs"
    steps.write_text(steps.read_text(encoding="utf-8").replace("DELETE FROM MES_Pipe", "DELETE FROM MES_Pipe /* x */"),
                     encoding="utf-8")
    (tmp_path / "notes.md").write_text("new file\n", encoding="utf-8")
    t = CodebaseTools(str(tmp_path))
    try:
        res = t.impact()
        assert [c["symbol"] for c in res["changed"]] == ["Acme.WriteLot.Process"]
        assert "notes.md" in res["unmapped_files"]  # untracked, no symbols
        assert {(x["target"], x["access"]) for x in res["tables"]} == {("MES_Pipe", "write")}
        assert [(p["program"], p["pages"]) for p in res["programs"]] == [("WK0001", ["Views/WK0001/WK0001.cshtml"])]
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "change")
        assert [c["symbol"] for c in t.impact("HEAD~1")["changed"]] == ["Acme.WriteLot.Process"]
        assert t.impact()["changed"] == []
        with pytest.raises(ValueError):
            t.impact("--output=x")
    finally:
        t.indexer.close()
