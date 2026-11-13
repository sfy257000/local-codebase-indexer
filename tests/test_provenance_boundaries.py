"""Regressions for answers that escaped their SQL statement or lexical binding."""
import pytest

from local_codebase_indexer import dataflow
from local_codebase_indexer.query import CodebaseTools
from local_codebase_indexer.sqlaccess import extract_sql_accesses


@pytest.mark.parametrize('separator', [';\n', '\nGO\n', '\n'])
def test_executesql_does_not_analyze_the_next_statement(separator):
    sql = "EXEC sys.sp_executesql @sql" + separator + "SELECT 'DELETE FROM NotExecuted' AS Message;"
    accesses = extract_sql_accesses(sql)
    assert any(a.op == 'dynamic' and a.confidence == 'uncertain' for a in accesses)
    assert not any(a.name == 'NotExecuted' for a in accesses)


@pytest.mark.parametrize('name', ['sp_executesql', '[sp_executesql]', '[sys].[sp_executesql]'])
def test_executesql_analyzes_only_the_sql_argument(name):
    sql = f"EXEC {name} N'SELECT * FROM dbo.Real', N'@x nvarchar(max)', @x=N'DELETE FROM NotExecuted';"
    accesses = extract_sql_accesses(sql)
    assert any(a.name == 'Real' and a.confidence == 'uncertain' for a in accesses)
    assert not any(a.name == 'NotExecuted' for a in accesses)
    assert not any(a.name.lower() == 'sp_executesql' and a.confidence == 'confirmed' for a in accesses)


def test_executesql_column_name_is_not_a_dynamic_execution():
    accesses = extract_sql_accesses('SELECT sp_executesql FROM dbo.Logs;')
    assert {(a.name, a.access) for a in accesses} == {('Logs', 'read')}


@pytest.mark.parametrize('separator', [';\n', '\nGO\n', '\n'])
def test_cte_name_does_not_hide_a_later_physical_table(separator):
    accesses = extract_sql_accesses('WITH cte AS (SELECT * FROM dbo.Real) SELECT * FROM cte' + separator + 'SELECT * FROM cte;')
    assert {a.name for a in accesses} == {'Real', 'cte'}


def test_update_does_not_steal_an_alias_from_the_next_select():
    accesses = extract_sql_accesses('UPDATE x SET Qty=1\nSELECT * FROM dbo.Real x;')
    assert {a.name for a in accesses if a.access == 'write'} == {'x'}


def test_join_comma_sources_survive_a_go_column():
    accesses = extract_sql_accesses('SELECT * FROM Alpha a JOIN Beta b ON a.go=b.id, Gamma c;')
    assert {a.name for a in accesses} == {'Alpha', 'Beta', 'Gamma'}


def test_application_functions_are_not_sql_object_definitions(tmp_path):
    (tmp_path / 'code.py').write_text('def Lots():\n    return 1\n')
    (tmp_path / 'other.js').write_text('function Orders() { return 1; }')
    tools = CodebaseTools(str(tmp_path))
    try:
        tools.refresh()
        assert not tools.data_access('Lots')['found']
        assert not tools.data_access('Orders')['found']
        assert not {'lots', 'orders'} & dataflow.known_object_keys(tools.conn)
        assert tools.semantic_search('Lots')['results']
        assert tools.symbol_relations('Lots')['definitions']
    finally:
        tools.indexer.close()


@pytest.mark.parametrize('body', [
    'void Run(string Q) { db.Execute(Q); }',
    'void Run() { string Q = GetSql(); db.Execute(Q); }',
    'void Run() { string s = Q; s = GetSql(); db.Execute(s); }',
])
def test_csharp_unknown_bindings_do_not_execute_stale_constants(tmp_path, body):
    (tmp_path / 'Repo.cs').write_text('class Repo { const string Q="DELETE FROM dbo.Never WHERE Id=@id";\n' + body + '\n}')
    tools = CodebaseTools(str(tmp_path))
    try:
        result = tools.data_access('Never')
        assert not any(e['source'] == 'Repo.Run' and e['confidence'] == 'confirmed' for e in result.get('writers', []))
        assert tools.conn.execute("SELECT count(*) FROM data_access WHERE target_key='?' AND confidence='uncertain'").fetchone()[0] >= 1
    finally:
        tools.indexer.close()


@pytest.mark.parametrize('declaration', ['Sub Run(ByVal Q As String)', 'Sub Run()\nDim Q As String'])
def test_vb6_local_binding_shadows_module_sql(tmp_path, declaration):
    (tmp_path / 'Repo.bas').write_text('Dim cn As ADODB.Connection\nPrivate Const Q As String = "DELETE FROM dbo.Never WHERE Id=@id"\n' + declaration + '\ncn.Execute Q\nEnd Sub\n')
    tools = CodebaseTools(str(tmp_path))
    try:
        result = tools.data_access('Never')
        assert not any(e['source'] == 'Repo.Run' and e['confidence'] == 'confirmed' for e in result.get('writers', []))
    finally:
        tools.indexer.close()


def test_vb6_receiver_parameter_does_not_inherit_module_ado_type(tmp_path):
    (tmp_path / 'Repo.bas').write_text('Dim cn As ADODB.Connection\nSub Run(ByVal cn As Object)\ncn.Execute "UPDATE dbo.Lot SET Qty=1"\nEnd Sub\n')
    tools = CodebaseTools(str(tmp_path))
    try:
        result = tools.data_access('Lot')
        assert any(e['source'] == 'Repo.Run' for e in result['writers'])
        assert all(e['confidence'] != 'confirmed' for e in result['writers'])
    finally:
        tools.indexer.close()


def test_real_sql_function_survives_application_name_collision(tmp_path):
    (tmp_path / 'code.py').write_text('def Lots():\n    return 1\n')
    (tmp_path / 'fn.sql').write_text('CREATE FUNCTION dbo.Lots() RETURNS int AS BEGIN RETURN 1; END;\nGO\n')
    tools = CodebaseTools(str(tmp_path))
    try:
        result = tools.data_access('Lots')
        assert result['found'] and result['kind'] == 'function'
        assert [e['file_path'] for e in result['definitions']] == ['fn.sql']
        assert 'lots' in dataflow.known_object_keys(tools.conn)
    finally:
        tools.indexer.close()


def test_named_executesql_and_normal_exec_keep_their_targets():
    accesses = extract_sql_accesses("EXEC sp_executesql @stmt=N'UPDATE dbo.Real SET Qty=1', @params=N'@p int', @p=1; EXEC dbo.Clean;")
    assert any(a.name == 'Real' and a.access == 'write' and a.confidence == 'uncertain' for a in accesses)
    assert any(a.name == 'Clean' and a.access == 'exec' and a.confidence == 'confirmed' for a in accesses)


def test_cte_filters_its_own_references_but_keeps_qualified_tables():
    accesses = extract_sql_accesses('WITH cte AS (SELECT * FROM dbo.Real) SELECT * FROM cte JOIN dbo.cte t ON 1=1;')
    assert {(a.name, a.qualifier) for a in accesses} == {('Real', 'dbo'), ('cte', 'dbo')}


def test_csharp_known_alias_and_execution_before_invalidation_are_preserved(tmp_path):
    (tmp_path / 'Repo.cs').write_text('''class Repo {
const string Q="DELETE FROM dbo.Real WHERE Id=@id";
void Run() { string s=Q; db.Execute(s); s=GetSql(); db.Execute(s); }
void Other() { db.Execute(Q); }
}''')
    tools = CodebaseTools(str(tmp_path))
    try:
        result = tools.data_access('Real')
        assert {'Repo.Run', 'Repo.Other'} <= {e['source'] for e in result['writers'] if e['confidence'] == 'confirmed'}
        assert tools.conn.execute("SELECT count(*) FROM data_access WHERE target_key='?'").fetchone()[0] >= 1
    finally:
        tools.indexer.close()


def test_vb6_ado_parameter_is_still_known_and_literal_text_does_not_declare_types(tmp_path):
    (tmp_path / 'Repo.bas').write_text('''Sub Known(ByVal cn As ADODB.Connection)
cn.Execute "UPDATE dbo.Real SET Qty=1"
End Sub
Sub Unknown()
note = "cn As ADODB.Connection"
cn.Execute "UPDATE dbo.Unproven SET Qty=1"
End Sub
''')
    tools = CodebaseTools(str(tmp_path))
    try:
        assert any(e['confidence'] == 'confirmed' for e in tools.data_access('Real')['writers'])
        assert all(e['confidence'] != 'confirmed' for e in tools.data_access('Unproven')['writers'])
    finally:
        tools.indexer.close()


def test_unknown_query_parameters_are_not_reported_as_sql(tmp_path):
    (tmp_path / 'Repo.cs').write_text('class Repo { void Run(object p) { db.Execute("UPDATE dbo.Real SET Qty=1", p); } }')
    tools = CodebaseTools(str(tmp_path))
    try:
        assert tools.data_access('Real')['writers']
        assert tools.conn.execute("SELECT count(*) FROM data_access WHERE target_key='?'").fetchone()[0] == 0
    finally:
        tools.indexer.close()


def test_sql_looking_query_parameter_is_not_a_confirmed_command(tmp_path):
    (tmp_path / 'Repo.cs').write_text('''class Repo {
void Run() {
string message="DELETE FROM dbo.NotExecuted WHERE Id=@id";
db.Execute("SELECT * FROM dbo.Real", message);
}
}''')
    tools = CodebaseTools(str(tmp_path))
    try:
        assert any(e['confidence'] == 'confirmed' for e in tools.data_access('Real')['readers'])
        assert all(e['confidence'] != 'confirmed' for e in tools.data_access('NotExecuted').get('writers', []))
    finally:
        tools.indexer.close()


@pytest.mark.parametrize('tail', ["PRINT 'DELETE FROM NotExecuted';", "IF @x='DELETE FROM NotExecuted' SELECT 1;"])
def test_executesql_does_not_consume_logging_or_conditions(tail):
    accesses = extract_sql_accesses('EXEC sp_executesql @sql\n' + tail)
    assert not any(a.name == 'NotExecuted' for a in accesses)


@pytest.mark.parametrize('consumer', [
    'SELECT * FROM cte\nUNION ALL SELECT * FROM cte',
    'INSERT INTO dbo.Target\nSELECT * FROM cte',
    'UPDATE dbo.Target SET Qty=(SELECT count(*) FROM cte)',
])
def test_cte_scope_keeps_union_insert_and_update_consumers(consumer):
    sql = 'WITH cte AS (SELECT * FROM dbo.Real)\n' + consumer + '\nSELECT * FROM cte;'
    accesses = extract_sql_accesses(sql)
    assert [a.line for a in accesses if a.name == 'cte'] == [sql.count('\n') + 1]


def test_explicit_field_keeps_its_binding_when_a_parameter_shadows_it(tmp_path):
    (tmp_path / 'Repo.cs').write_text('''class Repo {
readonly string Q="UPDATE dbo.Real SET Qty=1";
void Run(string Q) { db.Execute(this.Q); }
}''')
    tools = CodebaseTools(str(tmp_path))
    try:
        assert any(e['source'] == 'Repo.Run' and e['confidence'] == 'confirmed' for e in tools.data_access('Real')['writers'])
    finally:
        tools.indexer.close()


def test_explicit_field_invalidation_is_an_uncertain_execution(tmp_path):
    (tmp_path / 'Repo.cs').write_text('''class Repo {
string Q="DELETE FROM dbo.Never WHERE Id=@id";
void Run() { this.Q=GetSql(); db.Execute(this.Q); }
}''')
    tools = CodebaseTools(str(tmp_path))
    try:
        assert not any(e['source'] == 'Repo.Run' and e['confidence'] == 'confirmed' for e in tools.data_access('Never')['writers'])
        assert tools.conn.execute("SELECT count(*) FROM data_access WHERE target_key='?' AND confidence='uncertain'").fetchone()[0] >= 1
    finally:
        tools.indexer.close()
