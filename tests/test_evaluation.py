import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_codebase_indexer.cli import main
from local_codebase_indexer.evaluation import evaluate, save_report
from local_codebase_indexer.query import CodebaseTools


def dataset(file='service.py', symbol='load_invoice'):
    return {'version': 1, 'queries': [{'id': 'invoice', 'query': 'retrieve invoice',
                                      'relevant': [{'file': file, 'symbol': symbol}]}]}


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'service.py').write_text('def load_invoice():\n    return 1\n')
    t = CodebaseTools(str(tmp_path))
    yield tmp_path, t
    t.indexer.close()


def test_evaluate_counts_real_answers_and_enforces_budgets(project):
    _, t = project
    report = evaluate(t, dataset(), min_recall=1.0)
    assert report['passed'] and report['metrics']['queries'] == 1
    assert report['metrics']['recall_at_k'] == report['metrics']['mrr'] == 1.0
    assert report['metrics']['p95_ms'] >= 0 and report['metrics']['mean_estimated_tokens'] > 0
    assert report['rows'][0]['results'][0]['file'] == 'service.py'
    failure = evaluate(t, dataset(symbol='not_real'), min_recall=1.0, max_mean_tokens=0.0, max_p95_ms=0.0)
    assert not failure['passed'] and len(failure['failures']) == 3


def test_evaluation_baseline_comparisons_require_same_dataset(project):
    root, t = project
    baseline = evaluate(t, dataset())
    (root / 'service.py').write_text('def unrelated():\n    return 1\n')
    report = evaluate(t, dataset(), baseline=baseline)
    assert not report['passed'] and report['baseline_recall_delta'] == -1.0
    assert any('regressed' in reason for reason in report['failures'])
    with pytest.raises(ValueError, match='same queries'):
        evaluate(t, dataset(symbol='different'), baseline=baseline)
    with pytest.raises(ValueError, match='same queries'):
        evaluate(t, dataset(), limit=10, baseline=baseline)


@pytest.mark.parametrize('bad', [{}, {'version': 1, 'queries': []},
                                {'version': 1, 'queries': [{'id': 'x', 'query': 'x', 'relevant': []}]},
                                {'version': 1, 'queries': [{'id': 'x', 'query': '', 'relevant': [{'file': 'a.py'}]}]}])
def test_empty_or_invalid_evaluation_is_not_a_passing_run(project, bad):
    _, t = project
    with pytest.raises(ValueError):
        evaluate(t, bad)


def test_evaluation_cli_saves_report_and_returns_failure(project, capsys):
    root, t = project
    questions = root / '.ai_context' / 'eval.json'
    report_file = root / '.ai_context' / 'report.json'
    questions.write_text(json.dumps(dataset()))
    assert main(['--root', str(root), 'evaluate', '--queries', str(questions), '--min-recall', '1',
                 '--save', str(report_file), '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert json.loads(report_file.read_text()) == report
    assert main(['--root', str(root), 'evaluate', '--queries', str(questions), '--max-mean-tokens', '0']) == 3
    assert 'FAIL' in capsys.readouterr().out
    original = questions.read_text()
    assert main(['--root', str(root), 'evaluate', '--queries', str(questions), '--save', str(questions)]) == 1
    assert questions.read_text() == original


def test_evaluation_refuses_mixed_index_revisions(project, monkeypatch):
    root, t = project
    questions = dataset()
    questions['queries'].append({'id': 'second', 'query': 'invoice', 'relevant': [{'file': 'service.py'}]})
    original = t.semantic_search
    def changing_search(*args, **kwargs):
        result = original(*args, **kwargs)
        result['pagination']['index_revision'] = 'other-index'
        return result
    monkeypatch.setattr(t, 'semantic_search', changing_search)
    with pytest.raises(ValueError, match='changed during evaluation'):
        evaluate(t, questions)
