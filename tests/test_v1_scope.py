from contextlib import contextmanager

import pytest

from local_codebase_indexer import sqlidentity
from local_codebase_indexer.query import CodebaseTools, render_data_access


@contextmanager
def indexed(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    tools = CodebaseTools(str(root), auto_refresh=False)
    try:
        assert not tools.refresh().failed
        yield tools
    finally:
        tools.indexer.close()


@pytest.mark.parametrize(
    "left,right",
    [
        ("dbo.Lot", "archive.Lot"),
        ("A.dbo.Lot", "B.dbo.Lot"),
        ("[server\\one].A.dbo.Lot", "[server\\two].A.dbo.Lot"),
        ("[a.b].dbo.Lot", "[a].[b].dbo.Lot"),
    ],
)
def test_qualified_objects_never_share_confirmed_writers(tmp_path, left, right):
    sql = f"CREATE TABLE {left} (id int);\nGO\nCREATE TABLE {right} (id int);\nGO\n"
    sql += f"CREATE PROCEDURE dbo.First AS UPDATE {left} SET id=1;\nGO\nCREATE PROCEDURE dbo.Second AS UPDATE {right} SET id=2;\nGO\n"
    with indexed(tmp_path, {"objects.sql": sql}) as t:
        for query, wanted, unwanted in (
            (left, "First", "Second"),
            (right, "Second", "First"),
        ):
            r = t.data_access(query)
            assert r["found"] and not r["ambiguous"]
            assert len(r["definitions"]) == 1
            assert any(e["source"].endswith(wanted) for e in r["writers"])
            assert all(not e["source"].endswith(unwanted) for e in r["writers"])
        ambiguous = t.data_access("Lot")
        assert ambiguous["ambiguous"] and not ambiguous["found"]
        assert len(ambiguous["candidates"]) == 2
        assert "Ambiguous" in render_data_access(ambiguous)


def test_unknown_default_schema_is_possible_not_confirmed(tmp_path):
    with indexed(
        tmp_path,
        {
            "scope.sql": "CREATE TABLE dbo.Lot(id int);\nGO\nCREATE TABLE archive.Lot(id int);\nGO\nUPDATE Lot SET id=1;\n"
        },
    ) as t:
        for name in ("dbo.Lot", "archive.Lot"):
            r = t.data_access(name)
            assert not r["writers"]
            assert r["possible"] and all(
                e["confidence"] == "uncertain" for e in r["possible"]
            )


def test_use_database_context_survives_procedure_segmentation(tmp_path):
    sql = "USE [One];\nCREATE TABLE dbo.Lot(id int);\nGO\nCREATE PROCEDURE dbo.First AS UPDATE dbo.Lot SET id=1;\nGO\n"
    sql += "USE [Two];\nCREATE TABLE dbo.Lot(id int);\nGO\nCREATE PROCEDURE dbo.Second AS UPDATE dbo.Lot SET id=2;\nGO\n"
    with indexed(tmp_path, {"use.sql": sql}) as t:
        for query, wanted in (("One.dbo.Lot", "First"), ("Two.dbo.Lot", "Second")):
            r = t.data_access(query)
            assert [e["source"] for e in r["writers"]] == ["dbo." + wanted]
            assert r["writers"][0]["confidence"] == "confirmed"
        assert t.data_access("dbo.Lot")["ambiguous"]


def test_same_named_procedures_do_not_cross_indirect_chains(tmp_path):
    sql = "CREATE TABLE dbo.Live(id int);\nGO\nCREATE TABLE archive.Old(id int);\nGO\n"
    sql += "CREATE PROCEDURE dbo.Save AS UPDATE dbo.Live SET id=1;\nGO\nCREATE PROCEDURE archive.Save AS UPDATE archive.Old SET id=2;\nGO\n"
    sql += "CREATE PROCEDURE dbo.Parent AS EXEC dbo.Save;\nGO\nCREATE PROCEDURE archive.Parent AS EXEC archive.Save;\nGO\n"
    with indexed(tmp_path, {"chain.sql": sql}) as t:
        r = t.data_access("dbo.Live")
        assert {e["source"] for e in r["writers"]} == {"dbo.Save", "dbo.Parent"}
        assert {
            e["target"] for e in t.data_access("archive.Parent")["touches"]["writes"]
        } == {"Old"}
        rel = t.symbol_relations("dbo.Save")
        assert all(
            "archive" not in e["symbol"]
            for d in rel["definitions"]
            for e in d["upstream"]["callers"]
        )


def test_sql_depth_cutoffs_are_visible_and_can_be_extended(tmp_path):
    sql = "CREATE TABLE dbo.Lot(id int);\nGO\nCREATE PROCEDURE dbo.P0 AS UPDATE dbo.Lot SET id=1;\nGO\n"
    sql += "\n".join(
        f"CREATE PROCEDURE dbo.P{i} AS EXEC dbo.P{i - 1};\nGO" for i in range(1, 7)
    )
    with indexed(tmp_path, {"deep.sql": sql}) as t:
        short = t.data_access("dbo.Lot", sql_depth=2)
        assert not short["coverage"]["complete_within_limits"]
        assert any(
            w["code"] == "sql_depth_limit" for w in short["coverage"]["warnings"]
        )
        full = t.data_access("dbo.Lot", sql_depth=10)
        assert full["coverage"]["complete_within_limits"]
        assert len(full["writers"]) == 7
        path = next(e["path"] for e in full["writers"] if e["source"] == "dbo.P6")
        assert path[-1]["symbol"] == "[dbo].[lot]" and len(path) == 8


@pytest.mark.parametrize(
    "name", ["[a.b].[c d]", "[a]]b].[ x ]", "Server.DB..Thing", '"a.b"."x""y"']
)
def test_identity_round_trips_delimiters(name):
    value = sqlidentity.parts(name)
    assert sqlidentity.parts(sqlidentity.display(value)) == value
    assert sqlidentity.decode(sqlidentity.encode(value)) == value


def test_old_index_format_rebuilds_into_scoped_rows(tmp_path):
    with indexed(
        tmp_path,
        {"one.sql": "CREATE TABLE dbo.Lot(id int);\nGO\nUPDATE dbo.Lot SET id=1;"},
    ) as t:
        t.conn.execute("UPDATE meta SET value='10' WHERE key='index_version'")
        t.conn.execute("UPDATE data_access SET target_identity='' ")
        t.conn.commit()
        assert t.refresh().added == 1
        assert (
            t.conn.execute(
                "SELECT value FROM meta WHERE key='index_version'"
            ).fetchone()[0]
            == "11"
        )
        assert (
            t.data_access("dbo.Lot")["writers"][0]["object_identity"] == "[dbo].[lot]"
        )


def test_call_chain_cutoff_and_action_specific_page_evidence(tmp_path):
    source = 'class Leaf { SqlConnection db; public void Save(){ db.Execute("UPDATE dbo.Lot SET id=1;"); } }\n'
    for n in range(5):
        child = "Leaf" if n == 0 else f"Service{n - 1}"
        method = "Save" if n == 0 else "Run"
        source += f"class Service{n} {{ {child} next; public void Run(){{next.{method}();}} }}\n"
    source += "class OrdersController { Service4 svc; public void Post(){svc.Run();} public void Other(){} }"
    with indexed(
        tmp_path,
        {
            "chain.cs": source,
            "table.sql": "CREATE TABLE dbo.Lot(id int);",
            "Views/Orders/Edit.cshtml": '@Url.Action("Post", "Orders")',
            "Views/Orders/Other.cshtml": '@Url.Action("Other", "Orders")',
        },
    ) as t:
        clipped = t.data_access("dbo.Lot", call_depth=2)
        assert any(
            w["code"] == "call_depth_limit" for w in clipped["coverage"]["warnings"]
        )
        full = t.data_access("dbo.Lot", call_depth=16)
        assert full["programs"] and full["coverage"]["complete_within_limits"]
        trace = full["programs"][0]["paths"][0]
        assert trace["steps"][0]["symbol"] == "OrdersController.Post"
        assert len(trace["steps"]) >= 8
        assert [p["file_path"] for p in trace["pages"]] == ["Views/Orders/Edit.cshtml"]


def test_unresolved_procedure_definition_is_reported_as_partial(tmp_path):
    with indexed(
        tmp_path, {"one.sql": "CREATE PROCEDURE dbo.P AS EXEC dbo.ExternalProc;\nGO\n"}
    ) as t:
        r = t.data_access("dbo.P")
        assert not r["coverage"]["complete_within_limits"]
        assert any(
            w["code"] == "unresolved_sql_definition" for w in r["coverage"]["warnings"]
        )


def test_compact_methods_and_schema_accesses_keep_precise_owners(tmp_path):
    source = 'class Repo { SqlConnection db; public void Live(){db.Execute("UPDATE dbo.Lot SET id=1;");} public void Old(){db.Execute("UPDATE archive.Lot SET id=1;");} public void Both(){db.Execute("UPDATE dbo.Lot SET id=1; UPDATE archive.Lot SET id=2;");} }'
    with indexed(
        tmp_path,
        {
            "one.cs": source,
            "tables.sql": "CREATE TABLE dbo.Lot(id int);\nGO\nCREATE TABLE archive.Lot(id int);",
        },
    ) as t:
        assert {w["source"] for w in t.data_access("dbo.Lot")["writers"]} == {
            "Repo.Live",
            "Repo.Both",
        }
        assert {w["source"] for w in t.data_access("archive.Lot")["writers"]} == {
            "Repo.Old",
            "Repo.Both",
        }


def test_same_line_overloads_have_distinct_symbol_ids(tmp_path):
    with indexed(
        tmp_path,
        {
            "a.cs": "class C { public void Run(){} public void Run(int x){} public void Caller(){Run(1);} }"
        },
    ) as t:
        definitions = t.symbol_relations("C.Run")["definitions"]
        assert len(definitions) == 2 and len({d["id"] for d in definitions}) == 2
        assert sum(len(d["upstream"]["callers"]) for d in definitions) == 1


def test_ef_explicit_schema_and_delimited_proc_names_are_preserved(tmp_path):
    source = """class Entity {} class Model { public void Configure(ModelBuilder m){m.Entity<Entity>().ToTable("Lot", "archive");} }
class Repo { SqlConnection db; public void Save(){db.Execute("[archive].[Save Lot]", commandType: CommandType.StoredProcedure);} }
[Table("Lot", Schema="archive")] class OldEntity {}"""
    sql = "CREATE TABLE dbo.Lot(id int);\nGO\nCREATE TABLE archive.Lot(id int);\nGO\nCREATE PROCEDURE archive.[Save Lot] AS UPDATE archive.Lot SET id=1;\nGO\n"
    with indexed(tmp_path, {"a.cs": source, "a.sql": sql}) as t:
        assert not t.data_access("dbo.Lot")["mapped_by"]
        mapped = t.data_access("archive.Lot")["mapped_by"]
        assert len(mapped) == 2 and all(
            e["object_identity"] == "[archive].[lot]" for e in mapped
        )
        assert any(
            e["source"] == "Repo.Save" for e in t.data_access("archive.Lot")["writers"]
        )


def test_database_qualified_sql_symbol_relations_and_ambiguous_names(tmp_path):
    sql = "USE One;\nCREATE TABLE dbo.Lot(id int);\nGO\nUSE Two;\nCREATE TABLE dbo.Lot(id int);\nGO\n"
    with indexed(tmp_path, {"a.sql": sql}) as t:
        assert len(t.symbol_relations("One.dbo.Lot")["definitions"]) == 1
        assert t.symbol_relations("dbo.Lot")["sql_ambiguous"]


def test_partial_database_and_schema_information_is_not_discarded(tmp_path):
    sql = "CREATE TABLE dbo.Lot(id int);\nGO\nUSE One;\nUPDATE Lot SET id=1;\n"
    with indexed(tmp_path, {"partial.sql": sql}) as t:
        assert t.data_access("Lot")["ambiguous"]
        r = t.data_access("One.dbo.Lot")
        assert r["found"] and not r["writers"]
        assert r["possible"] and all(
            e["confidence"] == "uncertain" for e in r["possible"]
        )
        assert r["object_identity"] == "[one].[dbo].[lot]"
        assert not sqlidentity.refines(("one", "", "lot"), ("dbo", "lot"))


def test_old_database_without_identity_columns_is_migrated(tmp_path):
    import sqlite3

    from local_codebase_indexer import db

    folder = tmp_path / ".ai_context"
    folder.mkdir()
    conn = sqlite3.connect(folder / "indexer.db")
    old_schema = db.SCHEMA.replace(
        "detail TEXT NOT NULL DEFAULT '',\n    target_identity TEXT NOT NULL DEFAULT ''",
        "detail TEXT NOT NULL DEFAULT ''",
    )
    assert "target_identity TEXT" not in old_schema
    conn.executescript(old_schema)
    conn.execute("INSERT INTO meta VALUES('index_version','10')")
    conn.commit()
    conn.close()
    with indexed(tmp_path, {"a.sql": "UPDATE dbo.Lot SET id=1;"}) as t:
        assert "target_identity" in {
            r[1] for r in t.conn.execute("PRAGMA table_info(data_access)")
        }
        assert t.data_access("dbo.Lot")["writers"]


def test_ambiguous_ef_entity_mapping_is_never_a_confirmed_writer(tmp_path):
    source = """class E {} class Ctx { public DbSet<E> Lots {get;set;} }
class Model { public void Map(ModelBuilder m){m.Entity<E>().ToTable("Lot","dbo"); m.Entity<E>().ToTable("Lot","archive");} }
class Repo { Ctx ctx; public void Write(){ctx.Lots.Add(new E());} }"""
    with indexed(
        tmp_path,
        {
            "a.cs": source,
            "a.sql": "CREATE TABLE dbo.Lot(id int);\nGO\nCREATE TABLE archive.Lot(id int);",
        },
    ) as t:
        for name in ("dbo.Lot", "archive.Lot"):
            r = t.data_access(name)
            assert not any(w["via"] == "ef_linq" for w in r["writers"])
            assert any(
                w["via"] == "ef_linq" and w["confidence"] == "uncertain"
                for w in r["possible"]
            )


def test_stronger_procedure_path_replaces_a_previous_uncertain_path(tmp_path):
    sql = "CREATE TABLE dbo.Lot(id int);\nGO\nCREATE PROCEDURE dbo.P AS\nEXEC(N'UPDATE dbo.Lot SET id=1;');\nUPDATE dbo.Lot SET id=2;\nGO\nCREATE PROCEDURE dbo.Parent AS EXEC dbo.P;\nGO\n"
    with indexed(tmp_path, {"a.sql": sql}) as t:
        r = t.data_access("dbo.Lot")
        assert any(
            e["source"] == "dbo.Parent" and e["confidence"] == "confirmed"
            for e in r["writers"]
        )
        assert not any(e["source"] == "dbo.Parent" for e in r["possible"])


def test_procedure_mediated_schema_changes_stay_separate_from_row_writes(tmp_path):
    sql = "CREATE TABLE dbo.Lot(id int);\nGO\nCREATE PROCEDURE dbo.ChangeSchema AS ALTER TABLE dbo.Lot ADD extra int;\nGO\n"
    source = 'class Repo { SqlConnection db; public void Change(){db.Execute("dbo.ChangeSchema", commandType: CommandType.StoredProcedure);} }'
    with indexed(tmp_path, {"a.sql": sql, "a.cs": source}) as t:
        r = t.data_access("dbo.Lot")
        assert not r["writers"]
        assert any(e["source"] == "Repo.Change" for e in r["schema_changes"])


def test_use_keywords_inside_multiline_delimited_identifiers_are_ignored():
    masked = "USE RealDb;\nSELECT [first\nUSE FakeDb\nlast];\nUPDATE dbo.Lot SET id=1;"
    assert sqlidentity.contextual(["dbo", "Lot"], masked, len(masked)) == (
        "realdb",
        "dbo",
        "lot",
    )


def test_unicode_identifier_normalization_matches_stored_lookup_keys(tmp_path):
    with indexed(
        tmp_path,
        {
            "unicode.sql": "CREATE TABLE dbo.[Σigma](id int);\nGO\nUPDATE dbo.[Σigma] SET id=1;\nCREATE TABLE dbo.[Straße](id int);\nGO\nUPDATE dbo.[Straße] SET id=1;"
        },
    ) as t:
        assert t.data_access("dbo.[σigma]")["definitions"]
        assert t.data_access("dbo.[Straße]")["writers"]
