"""Regression coverage for real CLI/MCP failures and incomplete navigation results."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from local_codebase_indexer.cli import main
from local_codebase_indexer.mcp_server import handle
from local_codebase_indexer.query import CodebaseTools, render_data_access
from local_codebase_indexer.scanner import ScanReport, explain_path, iter_files

CORPUS = Path(__file__).resolve().parents[1] / 'benchmarks' / 'corpus'


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                           *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def corpus(tmp_path):
    shutil.copytree(CORPUS, tmp_path, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.ai_context', '__pycache__'))
    t = CodebaseTools(str(tmp_path))
    t.refresh()
    yield tmp_path, t
    t.indexer.close()


@pytest.mark.parametrize('command', ['index', 'serve', 'doctor', 'status'])
def test_missing_root_fails_without_creating_it(tmp_path, capsys, command):
    missing = tmp_path / 'mistyped'
    assert main(['--root', str(missing), command]) == 1
    assert not missing.exists()
    assert 'existing directory' in capsys.readouterr().err


def test_cli_empty_index_and_options_after_subcommand(tmp_path, capsys):
    assert main(['index', '--root', str(tmp_path), '--json']) == 3
    result = json.loads(capsys.readouterr().out)
    assert result['scanned'] == 0 and 'doctor' in result['hints'][0]
    assert main(['doctor', '--root', str(tmp_path), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['eligible_files'] == 0


def test_nested_ignore_override_and_parent_directory_rule(tmp_path):
    (tmp_path / '.gitignore').write_text('*.py\nhidden/\n')
    sub = tmp_path / 'sub'; sub.mkdir()
    (sub / '.gitignore').write_text('!keep.py\n')
    (sub / 'keep.py').write_text('x=1\n')
    (sub / 'skip.py').write_text('x=2\n')
    hidden = tmp_path / 'hidden'; hidden.mkdir()
    (hidden / '.gitignore').write_text('!keep.py\n')
    (hidden / 'keep.py').write_text('x=3\n')
    assert [f.rel_path for f in iter_files(str(tmp_path))] == ['sub/keep.py']
    assert explain_path(str(tmp_path), 'sub/keep.py')['eligible']
    assert explain_path(str(tmp_path), 'sub/skip.py')['reason'] == 'ignored_file'
    assert explain_path(str(tmp_path), 'hidden/keep.py')['reason'] == 'ignored_directory'


def test_doctor_explains_exclusions(tmp_path):
    (tmp_path / 'keep.py').write_text('x=1\n')
    (tmp_path / 'binary.py').write_bytes(b'\0binary')
    (tmp_path / 'unsupported.xyz').write_text('hello')
    (tmp_path / 'large.py').write_bytes(b'x' * (5*1024*1024+1))
    (tmp_path / '.gitignore').write_text('skip.py\n')
    (tmp_path / 'skip.py').write_text('x=2\n')
    node = tmp_path / 'node_modules'; node.mkdir(); (node / 'thing.js').write_text('x=1')
    t = CodebaseTools(str(tmp_path))
    try:
        report = t.doctor('binary.py')
        assert report['eligible_files'] == 1
        assert report['path']['reason'] == 'binary'
        assert report['counts']['too_large'] == 1
        assert report['counts']['unsupported_extension'] >= 1
        assert explain_path(str(tmp_path), 'node_modules/thing.js')['reason'] == 'excluded_directory'
        assert explain_path(str(tmp_path), 'missing.py')['reason'] == 'missing'
        with pytest.raises(ValueError):
            t.doctor('../outside.py')
    finally:
        t.indexer.close()


@pytest.mark.parametrize('limit', [1, 2, 3, 5])
def test_search_limit_and_repeatable_pages(corpus, limit):
    _, t = corpus
    all_hits = t.semantic_search('MES_Lot', limit=10)['results']
    page = t.semantic_search('MES_Lot', limit=limit)
    assert len(page['results']) == limit
    assert [(r['file_path'], r['start_line']) for r in page['results']] == [
        (r['file_path'], r['start_line']) for r in all_hits[:limit]]
    next_page = t.semantic_search('MES_Lot', limit=limit, offset=limit)
    assert [(r['file_path'], r['start_line']) for r in next_page['results']] == [
        (r['file_path'], r['start_line']) for r in all_hits[limit:limit*2]]


def test_search_filters_apply_before_ranking(corpus):
    _, t = corpus
    hits = t.semantic_search('lot', file_pattern='*Repository.cs', language='csharp')['results']
    assert hits and all(r['file_path'].endswith('Repository.cs') for r in hits)
    assert t.semantic_search('lot', file_pattern='*.not-a-file')['results'] == []
    assert t.semantic_search('lot', file_pattern='*.cs', language='python')['hints']
    with pytest.raises(ValueError):
        t.semantic_search('lot', language='unknown')
    with pytest.raises(ValueError):
        t.semantic_search('lot', offset=-1)


def test_empty_search_has_actionable_hints_and_similar_names(tmp_path):
    t = CodebaseTools(str(tmp_path))
    try:
        assert 'doctor' in ' '.join(t.semantic_search('missing')['hints'])
    finally:
        t.indexer.close()


def test_mcp_recovers_from_bad_requests_and_arguments(tmp_path):
    bad = [[], None, 'text', {'jsonrpc': '2.0', 'id': {}, 'method': 'ping'},
           {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': []}]
    for args in ({'query': 123}, {'query': 'x', 'limit': True}, {'query': 'x', 'offset': -1},
                 {'query': 'x', 'limit': 0}, {'query': 'x', 'language': 'nope'}, None):
        bad.append({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'codebase_semantic_search', 'arguments': args}})
    messages = ['{broken'] + [json.dumps(m) for m in bad]
    messages += [json.dumps({'jsonrpc': '2.0', 'id': 99, 'method': 'ping'})]
    proc = subprocess.run([sys.executable, '-m', 'local_codebase_indexer', '--root', str(tmp_path), 'serve'],
                          input='\n'.join(messages)+'\n', text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    results = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(results) == len(messages)
    assert results[-1] == {'jsonrpc': '2.0', 'id': 99, 'result': {}}
    assert all('error' in r or r['result']['isError'] for r in results[:-1])
    assert 'Traceback' not in proc.stderr


def test_mcp_tool_exception_does_not_end_session(tmp_path, monkeypatch):
    t = CodebaseTools(str(tmp_path))
    try:
        def fail(*args, **kwargs):
            raise RuntimeError('fixture failure')
        monkeypatch.setattr(t, 'semantic_search', fail)
        r = handle(t, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': 'codebase_semantic_search', 'arguments': {'query': 'x'}}})
        assert r['error']['code'] == -32603
        assert handle(t, {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})['result'] == {}
        assert handle(t, {'jsonrpc': '2.0', 'method': 'ping'}) is None
    finally:
        t.indexer.close()


def test_impact_multiple_methods_removed_method_and_deleted_file(tmp_path):
    f = tmp_path / '程式.py'
    f.write_text('def first():\n    return 1\ndef second():\n    return 2\n', encoding='utf-8')
    git(tmp_path, 'init', '-q'); git(tmp_path, 'add', '.'); git(tmp_path, 'commit', '-qm', 'base')
    t = CodebaseTools(str(tmp_path))
    try:
        t.refresh()
        f.write_text('def first(x=0):\n    return x+10\ndef second(x=0):\n    return x+20\n', encoding='utf-8')
        assert {c['symbol'] for c in t.impact()['changed']} == {'first', 'second'}
        f.write_text('def first():\n    return 1\n', encoding='utf-8')
        r = t.impact()
        assert any(c['symbol'] == 'second' and c['snapshot'] == 'before' for c in r['changed'])
        assert r['removed_files'] == []
        f.unlink()
        r = t.impact()
        assert r['removed_files'] == ['程式.py']
        assert {c['symbol'] for c in r['changed']} == {'first', 'second'}
    finally:
        t.indexer.close()


def test_deleted_writer_keeps_old_tables_and_callers(corpus):
    root, t = corpus
    git(root, 'init', '-q'); git(root, 'add', '.'); git(root, 'commit', '-qm', 'base')
    (root / 'Repositories' / 'LotRepository.cs').unlink()
    r = t.impact()
    assert any(e['target'].lower().endswith('mes_lot') and e['snapshot'] == 'before' for e in r['tables'])
    assert r['programs'], 'The deleted writer must retain its callers from the base revision'
    assert r['removed_files'] == ['Repositories/LotRepository.cs']


def test_data_access_pages_reach_beyond_old_cap_and_full_output(tmp_path):
    methods = '\n'.join(f'public void Write{i}() {{ _db.Execute("UPDATE MES_X SET Qty = {i}"); }}' for i in range(245))
    (tmp_path / 'Repo.cs').write_text('using System.Data; namespace Demo { class Repo { IDbConnection _db;\n'+methods+'\n} }')
    t = CodebaseTools(str(tmp_path))
    try:
        all_rows = t.data_access('MES_X')['writers']
        assert len(all_rows) == 245
        assert 'Write244' in render_data_access(t.data_access('MES_X'), full=True)
        seen = []
        for offset in range(0, 245, 40):
            page = t.data_access('MES_X', limit=40, offset=offset, confidence='confirmed')
            assert page['pagination']['sections']['writers']['total'] == 245
            assert len(page['writers']) <= 40
            seen.extend(e['source'] for e in page['writers'])
        assert len(seen) == len(set(seen)) == 245
        assert t.data_access('MES_X', confidence='uncertain')['writers'] == []
        assert not t.data_access('does_not_exist', limit=40)['found']
    finally:
        t.indexer.close()


@pytest.mark.parametrize('query,expected', [('retrieve invoice', 'load_invoice'), ('verify ticket', 'validate_ticket'),
                                            ('compute balance', 'calculate_balance'), ('取消工作', 'cancel_job')])
def test_generic_paraphrases_find_implementations(tmp_path, query, expected):
    (tmp_path / 'service.py').write_text('''def load_invoice():
    return database.get_record()
def validate_ticket():
    return ticket.valid
def calculate_balance():
    return credit - debit
def cancel_job():
    worker.stop()
def unrelated():
    return None
''')
    t = CodebaseTools(str(tmp_path))
    try:
        assert t.semantic_search(query, limit=1)['results'][0]['symbol'] == expected
    finally:
        t.indexer.close()


def test_impact_reuses_complete_historical_index(corpus, monkeypatch):
    root, t = corpus
    git(root, 'init', '-q'); git(root, 'add', '.'); git(root, 'commit', '-qm', 'base')
    (root / 'Repositories' / 'LotRepository.cs').unlink()
    first = t.impact()
    assert list((root / '.ai_context' / 'impact').glob('*.db'))
    original = subprocess.run
    def no_archive(args, *a, **kw):
        assert 'archive' not in args, 'The second call should reuse the baseline index'
        return original(args, *a, **kw)
    monkeypatch.setattr(subprocess, 'run', no_archive)
    assert t.impact() == first


def test_explicit_git_ranges_and_subdirectory_root(tmp_path):
    sub = tmp_path / 'src'; sub.mkdir()
    f = sub / 'service.py'; f.write_text('def first():\n    return 1\n')
    git(tmp_path, 'init', '-q'); git(tmp_path, 'add', '.'); git(tmp_path, 'commit', '-qm', 'base')
    f.unlink(); git(tmp_path, 'add', '.'); git(tmp_path, 'commit', '-qm', 'remove')
    t = CodebaseTools(str(sub))
    try:
        r = t.impact('HEAD~1...HEAD')
        assert r['removed_files'] == ['service.py']
        assert [c['symbol'] for c in r['changed']] == ['first']
    finally:
        t.indexer.close()


def test_confidence_filter_preserves_default_summary(tmp_path):
    methods = '\n'.join(f'public void Write{i}() {{ _db.Execute("UPDATE MES_X SET Qty = {i}"); }}' for i in range(8))
    (tmp_path / 'Repo.cs').write_text('using System.Data; class Repo { IDbConnection _db;\n'+methods+'\n}')
    t = CodebaseTools(str(tmp_path))
    try:
        result = t.data_access('MES_X', confidence='confirmed')
        assert 'pagination' not in result and len(result['writers']) == 8
        assert '... 3 more' in render_data_access(result)
        assert '... 3 more' not in render_data_access(result, full=True)
    finally:
        t.indexer.close()
