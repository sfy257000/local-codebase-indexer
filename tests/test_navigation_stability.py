"""Large-candidate, historical, and multi-process regressions from real navigation workflows."""
import json
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_codebase_indexer import db
from local_codebase_indexer.cli import main
from local_codebase_indexer.locking import index_lock
from local_codebase_indexer.query import CodebaseTools, MAX_CONTEXT_CHARS, render_search


def _git(root, *args):
    return subprocess.run(['git', '-C', str(root), '-c', 'user.name=test', '-c',
                           'user.email=test@example.invalid', *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def noisy_project(tmp_path):
    rng = random.Random(42)
    words = ['alpha', 'beta', 'gamma', 'delta', 'cache', 'record', 'buffer', 'report',
             'value', 'data', 'file', 'status', 'event', 'queue', 'source', 'target']
    text = '\n'.join(f'def alpha_{i}():\n    return ' + repr(' '.join(rng.choices(words, k=rng.randrange(5, 40)))) + '\n'
                     for i in range(200))
    (tmp_path / 'service.py').write_text(text)
    t = CodebaseTools(str(tmp_path))
    yield tmp_path, t
    t.indexer.close()


def _keys(results):
    return [(r['file_path'], r['start_line'], r['end_line']) for r in results]


def test_offset_and_cursor_pages_have_no_duplicates_or_missing_hits(noisy_project):
    _, t = noisy_project
    expected = _keys(t.semantic_search('alpha beta', limit=50)['results'])
    offset_pages = []
    for offset in range(0, 50, 5):
        offset_pages += _keys(t.semantic_search('alpha beta', limit=5, offset=offset)['results'])
    assert offset_pages == expected
    hits, cursor = [], None
    for _ in range(100):
        page = t.semantic_search('alpha beta', limit=7, cursor=cursor)
        hits += _keys(page['results'])
        cursor = page['pagination']['next_cursor']
        if cursor is None:
            break
    else:
        pytest.fail('Cursor did not terminate')
    assert len(hits) == len(set(hits)) == 200
    assert hits[:50] == expected


def test_cursors_survive_noop_refresh_but_reject_changes(noisy_project):
    root, t = noisy_project
    cursor = t.semantic_search('alpha beta')['pagination']['next_cursor']
    t.refresh()
    assert t.semantic_search('alpha beta', cursor=cursor)['results']
    with pytest.raises(ValueError, match='settings'):
        t.semantic_search('other query', cursor=cursor)
    with pytest.raises(ValueError, match='settings'):
        t.semantic_search('alpha beta', cursor=cursor, language='python')
    with pytest.raises(ValueError, match='not both'):
        t.semantic_search('alpha beta', cursor=cursor, offset=5)
    with pytest.raises(ValueError, match='Invalid search cursor'):
        t.semantic_search('alpha beta', cursor='not-a-cursor')
    (root / 'new.py').write_text('def alpha_new():\n    return "beta"\n')
    t.refresh()
    with pytest.raises(ValueError, match='index or query settings'):
        t.semantic_search('alpha beta', cursor=cursor)


def test_cursor_rejects_synonym_change_without_server_restart(noisy_project):
    root, t = noisy_project
    cursor = t.semantic_search('alpha beta')['pagination']['next_cursor']
    (root / '.ai_context' / 'synonyms.json').write_text(json.dumps({'groups': [['alpha', 'newalias']]}))
    with pytest.raises(ValueError, match='settings'):
        t.semantic_search('alpha beta', cursor=cursor)
    assert 'newalias' in t.expander.expand('alpha')
    (root / '.ai_context' / 'synonyms.json').unlink()
    t.semantic_search('alpha beta')
    assert 'newalias' not in t.expander.expand('alpha')


def test_historical_range_does_not_use_head_or_dirty_worktree(tmp_path):
    f = tmp_path / 'service.py'
    f.write_text('def first():\n    return 1\n')
    _git(tmp_path, 'init', '-q'); _git(tmp_path, 'add', '.'); _git(tmp_path, 'commit', '-qm', 'A')
    first = _git(tmp_path, 'rev-parse', 'HEAD').strip()
    f.write_text('def first():\n    return 2\n')
    _git(tmp_path, 'add', '.'); _git(tmp_path, 'commit', '-qm', 'B')
    second = _git(tmp_path, 'rev-parse', 'HEAD').strip()
    f.write_text('def unrelated():\n    return 3\n')
    _git(tmp_path, 'add', '.'); _git(tmp_path, 'commit', '-qm', 'C')
    f.write_text('def dirty_function():\n    return 4\n')
    t = CodebaseTools(str(tmp_path))
    try:
        for base in (f'{first}..{second}', f'{first}...{second}'):
            result = t.impact(base)
            assert [(c['symbol'], c['snapshot']) for c in result['changed']] == [('first', 'after')]
            assert result['revisions'] == {'before': first, 'after': second}
            assert f.read_text() == 'def dirty_function():\n    return 4\n'
        assert any(c['symbol'] == 'dirty_function' for c in t.impact()['changed'])
    finally:
        t.indexer.close()


def test_context_retains_focus_and_clips_individual_long_lines(tmp_path):
    (tmp_path / 'wide.py').write_text(('x = "' + 'a' * 75 + '"\n') * 1000)
    (tmp_path / 'long.py').write_text('a' * (MAX_CONTEXT_CHARS * 2) + '\n' + 'b' * MAX_CONTEXT_CHARS + '\n')
    t = CodebaseTools(str(tmp_path))
    try:
        result = t.optimized_context('wide.py', [500], 150)
        assert result['truncated'] and result['omitted_lines'] > 0
        assert '500 | ' in result['context'] and 0 < len(result['context']) <= MAX_CONTEXT_CHARS
        result = t.optimized_context('long.py', [1, 2], 0)
        assert result['truncated_lines'] == [1, 2]
        assert '1 | ' in result['context'] and '2 | ' in result['context']
        assert len(result['context']) <= MAX_CONTEXT_CHARS
        with pytest.raises(ValueError, match='outside'):
            t.optimized_context('wide.py', [9000], 10)
        with pytest.raises(ValueError, match='positive'):
            t.optimized_context('wide.py', [0], 10)
    finally:
        t.indexer.close()


def test_synonym_explanations_include_real_sources_and_mined_words(tmp_path):
    (tmp_path / 'service.py').write_text('def load_invoice():\n    return 1\n')
    t = CodebaseTools(str(tmp_path))
    try:
        (tmp_path / '.ai_context' / 'synonyms.json').write_text(json.dumps({'groups': [['invoice', '發票']]}))
        result = t.semantic_search('retrieve 發票', limit=1, explain=True)
        detail = result['results'][0]['explanation']
        expanded = {item['term']: item for item in detail['matched_expansions']}
        assert expanded['invoice']['sources'][0]['source'] == 'project'
        assert expanded['load']['sources'][0]['source'] == 'general'
        assert detail['layers']['keyword']['rank'] >= 1
        assert 'expansion:' in render_search(result)
        assert 'explanation' not in t.semantic_search('invoice', limit=1)['results'][0]
    finally:
        t.indexer.close()


def test_search_cli_explain_and_cursor(noisy_project, capsys):
    root, _ = noisy_project
    assert main(['search', 'alpha beta', '--root', str(root), '--limit', '3', '--explain', '--json']) == 0
    first = json.loads(capsys.readouterr().out)
    cursor = first['pagination']['next_cursor']
    assert 'explanation' in first['results'][0]
    assert main(['search', 'alpha beta', '--root', str(root), '--cursor', cursor, '--json']) == 0
    second = json.loads(capsys.readouterr().out)
    assert set(_keys(first['results'])).isdisjoint(_keys(second['results']))


def test_external_sqlite_writer_returns_busy_without_traceback(tmp_path):
    (tmp_path / 'service.py').write_text('def first():\n    return 1\n')
    t = CodebaseTools(str(tmp_path)); t.refresh()
    try:
        t.conn.execute('BEGIN IMMEDIATE')
        env = dict(os.environ, INDEXER_BUSY_TIMEOUT='0.1', INDEXER_LOCK_TIMEOUT='0.1')
        result = subprocess.run([sys.executable, '-m', 'local_codebase_indexer', '--root', str(tmp_path), 'search', 'first'],
                                env=env, text=True, capture_output=True, timeout=10)
        assert result.returncode == 4
        assert 'busy' in result.stderr.lower() and 'Traceback' not in result.stderr
    finally:
        t.conn.rollback(); t.indexer.close()


def test_process_lock_wait_is_bounded_and_releases_cleanly(tmp_path):
    (tmp_path / 'service.py').write_text('def first():\n    return 1\n')
    t = CodebaseTools(str(tmp_path)); t.refresh()
    env = dict(os.environ, INDEXER_LOCK_TIMEOUT='0.1')
    command = [sys.executable, '-m', 'local_codebase_indexer', '--root', str(tmp_path), 'index']
    try:
        with index_lock(t.indexer.db_path):
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=10)
            assert result.returncode == 4 and 'Traceback' not in result.stderr
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
    finally:
        t.indexer.close()


def test_two_indexing_processes_leave_a_complete_index(tmp_path):
    for i in range(10):
        (tmp_path / f'part{i}.py').write_text('\n'.join(f'def function_{i}_{j}():\n    return {j}\n' for j in range(20)))
    command = [sys.executable, '-m', 'local_codebase_indexer', '--root', str(tmp_path), 'index']
    children = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for child in children:
        out, err = child.communicate(timeout=20)
        assert child.returncode == 0, err
    t = CodebaseTools(str(tmp_path))
    try:
        status = t.index_status()
        assert status['state'] == 'fresh' and status['consistency']['ok'] and status['symbols'] == 200
    finally:
        t.indexer.close()


def test_interrupted_batch_is_detected_and_recovers(tmp_path):
    (tmp_path / 'service.py').write_text('def first():\n    return 1\n')
    t = CodebaseTools(str(tmp_path)); t.refresh()
    try:
        previous = db.get_meta(t.conn, 'index_revision')
        db.set_meta(t.conn, 'indexing', '1'); t.conn.commit()
        assert t.index_status()['state'] == 'degraded'
        stats = t.refresh()
        assert stats.updated == stats.scanned == 1
        assert db.get_meta(t.conn, 'indexing') is None
        assert db.get_meta(t.conn, 'index_revision') != previous
        assert t.index_status()['state'] == 'fresh'
    finally:
        t.indexer.close()


def test_malformed_synonym_config_logs_once_and_recovers(tmp_path, capsys):
    (tmp_path / 'service.py').write_text('def load_invoice():\n    return 1\n')
    t = CodebaseTools(str(tmp_path))
    try:
        config = tmp_path / '.ai_context' / 'synonyms.json'
        config.write_text('{"groups": null}')
        t.semantic_search('invoice')
        assert 'ignoring' in capsys.readouterr().err
        t.semantic_search('invoice')
        assert capsys.readouterr().err == ''
        config.write_text('{"groups": [["invoice", "發票"]]}', encoding='utf-8')
        assert any(item['term'] == 'invoice' for item in
                   t.semantic_search('發票', explain=True)['results'][0]['explanation']['matched_expansions'])
    finally:
        t.indexer.close()


def test_mcp_exposes_cursor_and_layer_explanations(noisy_project):
    from local_codebase_indexer.mcp_server import handle
    _, t = noisy_project
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': 'codebase_semantic_search',
                          'arguments': {'query': 'alpha beta', 'limit': 3, 'explain': True}}}
    response = handle(t, request)
    assert not response['result']['isError']
    text = response['result']['content'][0]['text']
    assert 'RRF contribution' in text
    cursor = text.split('More results: --cursor ', 1)[1].splitlines()[0]
    request['params']['arguments'] = {'query': 'alpha beta', 'cursor': cursor, 'limit': 3}
    assert not handle(t, request)['result']['isError']
    request['params']['arguments']['query'] = 'different'
    assert handle(t, request)['result']['isError']


def test_windows_lock_contends_before_touching_a_locked_byte(tmp_path, monkeypatch):
    """Model the first-run Windows race that intermittently failed real CI."""
    import builtins
    import errno
    import types
    from local_codebase_indexer import locking

    state = {'held': False, 'attempts': 0}
    class GuardedStream:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def write(self, value):
            if not state['held']:
                raise PermissionError(errno.EACCES, 'Another process owns this byte')
            return self.stream.write(value)

    def byte_lock(fd, operation, count):
        assert count == 1
        if operation == 1:
            state['attempts'] += 1
            if state['attempts'] == 1:
                raise PermissionError(errno.EACCES, 'Contended byte')
            state['held'] = True
        else:
            state['held'] = False

    monkeypatch.setattr(locking, 'os', types.SimpleNamespace(
        name='nt', path=os.path, makedirs=os.makedirs, SEEK_END=os.SEEK_END))
    monkeypatch.setitem(sys.modules, 'msvcrt', types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=byte_lock))
    monkeypatch.setattr(locking, 'open', lambda *a, **k: GuardedStream(builtins.open(*a, **k)), raising=False)
    with locking.index_lock(str(tmp_path / 'index.db')):
        assert state['held'] and state['attempts'] == 2
    assert not state['held']
