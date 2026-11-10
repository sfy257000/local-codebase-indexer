"""SQL syntax and encoding cases reported by users, including negative examples."""
import codecs

import pytest

from local_codebase_indexer.query import CodebaseTools, render_data_access
from local_codebase_indexer.sqlaccess import extract_sql_accesses


@pytest.mark.parametrize('sql,table,op', [
    ('UPDATE a SET x=1 FROM dbo.Lot a;', 'Lot', 'update'),
    ('DELETE a FROM dbo.Lot a;', 'Lot', 'delete'),
    ('UPDATE an SET x=1 FROM dbo.Lot an;', 'Lot', 'update'),
    ('UPDATE dbo.Lot WITH (ROWLOCK) SET x=1;', 'Lot', 'update'),
    ('UPDATE TOP (@n) dbo.Lot SET x=1;', 'Lot', 'update'),
    ('UPDATE TOP (@n + 1) dbo.Lot WITH (INDEX(ix), ROWLOCK) SET x=1;', 'Lot', 'update'),
    ('DELETE TOP (@n) FROM dbo.Lot;', 'Lot', 'delete'),
    ('UPDATE t SET x=1 FROM dbo.Lot (NOLOCK) t;', 'Lot', 'update'),
])
def test_write_targets(sql, table, op):
    accesses = extract_sql_accesses(sql)
    assert any(a.name == table and a.access == 'write' and a.op == op and a.confidence == 'confirmed' for a in accesses)
    assert not any(a.name.lower() in {'a', 'an', 't', 'top'} for a in accesses)


@pytest.mark.parametrize('name,last,qualifier', [
    (r'[ntemes\mes].MESDB.dbo.Lot_Hist', 'Lot_Hist', r'ntemes\mes.MESDB.dbo'),
    ('[MES-DB].dbo.[Lot Master]', 'Lot Master', 'MES-DB.dbo'),
    ('[Order Details]', 'Order Details', ''),
    ('[a.b].[x]]y]', 'x]y', 'a.b'),
    ('"MES-DB".dbo."Order Details"', 'Order Details', 'MES-DB.dbo'),
    ('[select]', 'select', ''),
])
def test_quoted_names_stay_whole(name, last, qualifier):
    accesses = extract_sql_accesses(f'SELECT * FROM {name}; UPDATE {name} SET x=1;')
    assert {(a.name, a.qualifier, a.access) for a in accesses} == {(last, qualifier, 'read'), (last, qualifier, 'write')}


def test_no_keywords_inside_identifiers_comments_cursors_or_trim():
    sql = """/* outer /* nested */ DELETE FROM FalseTable; */
    FETCH NEXT FROM lotCursor INTO @id;
    SELECT TRIM('x' FROM col) FROM dbo.Real;
    SELECT * FROM [FROM FakeTable];
    SELECT * FROM dbo.Other (NOLOCK) o;
    """
    got = extract_sql_accesses(sql)
    assert {(a.name, a.kind) for a in got} == {('Real', 'table'), ('FROM FakeTable', 'table'), ('Other', 'table')}


def test_join_then_comma_list_and_nested_query():
    got = extract_sql_accesses('SELECT * FROM Alpha a JOIN Beta b ON a.id=b.id AND b.x IN (1,2), Gamma c WHERE a.id=c.id;')
    assert {a.name for a in got} == {'Alpha', 'Beta', 'Gamma'}
    got = extract_sql_accesses('SELECT TRIM((SELECT TOP 1 x FROM InnerTable) FROM col) FROM OuterTable;')
    assert {a.name for a in got} == {'InnerTable', 'OuterTable'}


def test_openquery_is_possible_not_confirmed():
    got = extract_sql_accesses("SELECT * FROM OPENQUERY(RemoteServer, 'SELECT * FROM dbo.RemoteLot');")
    assert any(a.name == 'RemoteLot' and a.confidence == 'uncertain' for a in got)
    assert not any(a.name.lower() in {'openquery', 'remoteserver'} for a in got)


@pytest.mark.parametrize('encoding,bom', [('utf-16-le', codecs.BOM_UTF16_LE), ('utf-16-be', codecs.BOM_UTF16_BE),
                                         ('utf-32-le', codecs.BOM_UTF32_LE), ('utf-32-be', codecs.BOM_UTF32_BE)])
def test_unicode_sql_is_indexed_and_status_is_fresh(tmp_path, encoding, bom):
    (tmp_path / 'export.sql').write_bytes(bom + '-- 中文\nUPDATE dbo.Lot SET Status=1;\n'.encode(encoding))
    tools = CodebaseTools(str(tmp_path))
    try:
        stats = tools.refresh()
        assert stats.added == 1 and stats.skipped == 0 and stats.failed == 0
        assert tools.data_access('Lot')['writers']
        assert tools.index_status()['state'] == 'fresh'
        assert tools.refresh().unchanged == 1
        assert 'UPDATE dbo.Lot' in tools.optimized_context('export.sql', [2])['context']
        assert 'eligible' == tools.doctor('export.sql')['path']['reason']
    finally:
        tools.indexer.close()


def test_constant_sql_is_attributed_to_each_executing_method(tmp_path):
    (tmp_path / 'Repo.cs').write_text('''class Repo {
      const string Q = "DELETE FROM dbo.Lot WHERE Id=@id";
      void Clean() { db.Execute(Q); }
      void CleanAgain() { db.Execute(Q); }
      void Hint() { db.Execute("UPDATE dbo.Lot WITH (ROWLOCK) SET Status=1"); }
    }''', encoding='utf-8')
    tools = CodebaseTools(str(tmp_path))
    try:
        writers = tools.data_access('Lot')['writers']
        assert {'Repo.Clean', 'Repo.CleanAgain', 'Repo.Hint'} <= {w['source'] for w in writers}
        assert all(w['confidence'] == 'confirmed' for w in writers if w['source'] in {'Repo.Clean', 'Repo.CleanAgain'})
    finally:
        tools.indexer.close()


def test_schema_changes_are_separate_from_data_writes(tmp_path):
    (tmp_path / 'change.sql').write_text('ALTER TABLE dbo.Lot ADD Status int;\nCREATE INDEX IX_Lot ON dbo.Lot(Status);\nDROP TABLE dbo.OldLot;', encoding='utf-8')
    tools = CodebaseTools(str(tmp_path))
    try:
        res = tools.data_access('Lot')
        assert not res['writers']
        assert {a['op'] for a in res['schema_changes']} == {'alter_table', 'create_index'}
        assert 'SCHEMA CHANGED BY' in render_data_access(res, full=True)
        assert tools.data_access('OldLot')['schema_changes'][0]['op'] == 'drop_table'
    finally:
        tools.indexer.close()


def test_binary_files_do_not_keep_project_stale(tmp_path):
    (tmp_path / 'ok.sql').write_text('SELECT * FROM dbo.Lot;')
    (tmp_path / 'binary.sql').write_bytes(b'not\x00text')
    tools = CodebaseTools(str(tmp_path))
    try:
        tools.refresh()
        assert tools.index_status()['state'] == 'fresh'
        assert tools.doctor('binary.sql')['path']['reason'] == 'binary'
    finally:
        tools.indexer.close()


def test_project_ignore_can_opt_in_default_directories_but_not_index_state(tmp_path):
    from local_codebase_indexer.scanner import iter_files, explain_path
    for directory in ('bin', 'packages', 'build', 'dist', '.git', '.ai_context'):
        folder = tmp_path / directory
        folder.mkdir()
        (folder / 'source.cs').write_text('class Source {}')
    assert list(iter_files(str(tmp_path))) == []
    (tmp_path / '.gitignore').write_text('bin/\npackages/\nbuild/\ndist/\n')
    (tmp_path / '.ai_context' / 'ignore').write_text('!bin/\n!packages/\n!build/\n!dist/\n!.git/\n!.ai_context/\n')
    expected = {f'{name}/source.cs' for name in ('bin', 'packages', 'build', 'dist')}
    assert {f.rel_path for f in iter_files(str(tmp_path))} == expected
    for path in expected:
        assert explain_path(str(tmp_path), path)['eligible']
    assert not explain_path(str(tmp_path), '.ai_context/source.cs')['eligible']


def test_schema_change_is_reported_by_git_impact(tmp_path):
    import subprocess
    def git(*args):
        subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args], check=True, capture_output=True)
    git('init')
    (tmp_path / 'change.sql').write_text('ALTER TABLE dbo.Lot ADD OldValue int;\n')
    git('add', '.')
    git('commit', '-m', 'before')
    (tmp_path / 'change.sql').write_text('ALTER TABLE dbo.Lot ADD NewValue int;\n')
    tools = CodebaseTools(str(tmp_path))
    try:
        tools.refresh()
        result = tools.impact()
        assert any(a['target'] == 'Lot' and a['access'] == 'schema' and a['snapshot'] == 'current' for a in result['tables'])
        assert any(a['access'] == 'schema' and a['snapshot'] == 'before' for a in result['tables'])
    finally:
        tools.indexer.close()


@pytest.mark.parametrize('extension', ['frm', 'bas', 'cls'])
def test_vb6_sql_has_method_owners_and_conservative_confidence(tmp_path, extension):
    (tmp_path / f'Legacy.{extension}').write_text('''Attribute VB_Name = "Legacy"
Private cn As ADODB.Connection
Private Const CleanSql As String = "DELETE FROM dbo.OldLot WHERE Id=@id"
Private Sub Clean()
    cn.Execute CleanSql
End Sub
Private Sub Save()
    Dim sql As String
    sql = "UPDATE dbo.Lot WITH (ROWLOCK) " & _
          "SET Status='Approved' WHERE Id=" & id
    cn.Execute sql
    ' cn.Execute "DELETE FROM Commented"
    Rem cn.Execute "DELETE FROM CommentedToo"
End Sub
Private Sub Load()
    rs.Open "SELECT * FROM dbo.Lot", cn
End Sub
''', encoding='utf-8')
    tools = CodebaseTools(str(tmp_path))
    try:
        assert any(a['source'] == 'Legacy.Clean' and a['confidence'] == 'confirmed' for a in tools.data_access('OldLot')['writers'])
        result = tools.data_access('Lot')
        assert any(a['source'] == 'Legacy.Save' and a['confidence'] == 'probable' for a in result['writers'])
        assert any(a['source'] == 'Legacy.Load' and a['confidence'] == 'probable' for a in result['readers'])
        assert not tools.data_access('Commented')['found']
        assert not tools.data_access('CommentedToo')['found']
        assert tools.semantic_search('CleanSql')['results']
    finally:
        tools.indexer.close()


def test_quoted_definition_and_spaced_query_name(tmp_path):
    (tmp_path / 'names.sql').write_text('CREATE TABLE [Order Details] (Id int);\nUPDATE [Order Details] SET Id=1;', encoding='utf-8')
    tools = CodebaseTools(str(tmp_path))
    try:
        for query in ('Order Details', '[Order Details]'):
            result = tools.data_access(query)
            assert result['found'] and result['writers']
            assert result['definitions'][0]['qualified_name'] == 'Order Details'
    finally:
        tools.indexer.close()


def test_quoted_cte_is_not_a_physical_table():
    got = extract_sql_accesses('WITH [Lot CTE] AS (SELECT * FROM dbo.Lot) SELECT * FROM [Lot CTE];')
    assert {a.name for a in got} == {'Lot'}


def test_vb6_unknown_reassignment_does_not_reuse_old_confirmed_sql(tmp_path):
    (tmp_path / 'Legacy.bas').write_text('''Dim cn As ADODB.Connection
Sub Run()
  sql = "DELETE FROM dbo.OldLot WHERE Id=@id"
  sql = GetSql()
  cn.Execute sql
End Sub
''', encoding='utf-8')
    tools = CodebaseTools(str(tmp_path))
    try:
        assert not any(a['confidence'] == 'confirmed' for a in tools.data_access('OldLot')['writers'])
    finally:
        tools.indexer.close()


def test_constant_names_in_other_classes_do_not_cross_contaminate(tmp_path):
    (tmp_path / 'Repos.cs').write_text('''class First {
 const string Q = "DELETE FROM dbo.FirstLot WHERE Id=@id";
 void Clean() { db.Execute(Q); }
}
class Second {
 const string Q = "DELETE FROM dbo.SecondLot WHERE Id=@id";
 void Clean() { db.Execute(Q); }
}
''', encoding='utf-8')
    tools = CodebaseTools(str(tmp_path))
    try:
        first = tools.data_access('FirstLot')['writers']
        second = tools.data_access('SecondLot')['writers']
        assert any(a['source'] == 'First.Clean' and a['confidence'] == 'confirmed' for a in first)
        assert any(a['source'] == 'Second.Clean' and a['confidence'] == 'confirmed' for a in second)
        assert not any(a['source'] == 'Second.Clean' for a in first)
        assert not any(a['source'] == 'First.Clean' for a in second)
    finally:
        tools.indexer.close()


def test_top_insert_and_quoted_semicolon_alias():
    got = extract_sql_accesses('INSERT TOP (@n) INTO dbo.Lot SELECT * FROM dbo.Other;')
    assert [(a.name, a.op) for a in got if a.access == 'write'] == [('Lot', 'insert')]
    got = extract_sql_accesses('UPDATE a SET x=1 FROM dbo.[Odd;Table] a;')
    assert any(a.name == 'Odd;Table' and a.access == 'write' for a in got)


def test_go_ends_procedure_before_schema_script(tmp_path):
    (tmp_path / 'batches.sql').write_text('''CREATE PROCEDURE dbo.ReadLots AS
SELECT * FROM dbo.Lot;
GO
ALTER TABLE dbo.Lot ADD Status int;
''', encoding='utf-8')
    tools = CodebaseTools(str(tmp_path))
    try:
        result = tools.data_access('Lot')
        assert result['readers'][0]['source'] == 'dbo.ReadLots'
        assert result['schema_changes'][0]['source_type'] == 'file'
        assert not tools.data_access('ReadLots')['touches']['schema_changes']
    finally:
        tools.indexer.close()


def test_schema_confidence_filter_and_pagination(tmp_path):
    (tmp_path / 'change.sql').write_text('ALTER TABLE dbo.Lot ADD X int;\nCREATE INDEX IX ON dbo.Lot(X);')
    tools = CodebaseTools(str(tmp_path))
    try:
        result = tools.data_access('Lot', limit=1, confidence='confirmed')
        assert len(result['schema_changes']) == 1
        assert result['pagination']['sections']['schema_changes']['total'] == 2
        second = tools.data_access('Lot', limit=1, offset=1, confidence='confirmed')
        assert second['schema_changes'][0]['op'] == 'create_index'
    finally:
        tools.indexer.close()


def test_vb6_routine_is_not_a_sql_object_or_indirect_procedure_writer(tmp_path):
    (tmp_path / 'LegacyController.bas').write_text('''Dim cn As ADODB.Connection
Sub Clean()
  cn.Execute "UPDATE dbo.Lot SET Qty=1"
End Sub
''', encoding='utf-8')
    (tmp_path / 'calls.sql').write_text('EXEC dbo.Clean;\n', encoding='utf-8')
    tools = CodebaseTools(str(tmp_path))
    try:
        result = tools.data_access('Lot')
        assert result['kind'] == 'table'
        assert not result['programs']
        assert any(a['source'] == 'LegacyController.Clean' for a in result['writers'])
        assert not any(a['file_path'] == 'calls.sql' for a in result['writers'])
        assert not tools.data_access('Clean').get('definitions')
    finally:
        tools.indexer.close()
