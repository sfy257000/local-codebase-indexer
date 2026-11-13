"""The public benchmark must score gains and context denominators correctly."""
import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tool_comparison', ROOT / 'benchmarks' / 'compare_tools.py')
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def test_file_scoring_deduplicates_candidates_and_counts_rank():
    result = comparison.file_scores(['noise', 'answer', 'answer', 'other'], {'answer', 'missing'})
    assert result['recall5'] == 0.5
    assert result['rr5'] == 0.5
    assert result['top_files'] == ['noise', 'answer', 'other']
    with pytest.raises(ValueError):
        comparison.file_scores([], set())


def test_context_reduction_keeps_losses_and_missing_denominators():
    assert comparison.reduction_percent(120, 100) == pytest.approx(-20)
    assert comparison.reduction_percent(0, 0) is None


def test_summary_uses_percentage_points_and_total_characters():
    rows = []
    for baseline_recall, recall, output, full in [(0, 1, 10, 100), (1, 0.5, 200, 200)]:
        rows.append({'rg_bm25': {'recall5': baseline_recall, 'rr5': 1, 'query_ms': [1]},
                     'indexer': {'recall5': recall, 'rr5': 1, 'query_ms': [2],
                                 'output_chars': output, 'same_files_full_read_chars': full}})
    result = comparison.summarize(rows)
    assert result['recall_gain_pp'] == 25
    assert result['context']['reduction_pct'] == pytest.approx(30)  # not the 45% average of ratios


def test_keyword_terms_split_identifiers_without_domain_answers():
    words = comparison.terms('MES_Lot and SQLParser 密碼查詢')
    assert {'mes_lot', 'mes', 'lot', 'sqlparser', 'sql', 'parser', '密碼', '碼查', '查詢'} <= set(words)
    assert 'and' not in words
    assert comparison.terms('scrap') == ['scrap']


def test_evidence_paths_do_not_come_from_freeform_text():
    result = comparison.evidence_files({'definitions': [{'file_path': 'table.sql'}],
        'writers': [{'file_path': 'repo.cs', 'evidence': 'not-a-path.cs'}],
        'programs': [{'file_path': 'controller.cs', 'pages': ['page.cshtml']}]})
    assert set(result) == {'table.sql', 'repo.cs', 'controller.cs', 'page.cshtml'}


@pytest.mark.skipif(not shutil.which('rg'), reason='ripgrep is optional for product installs; comparison CI installs it')
def test_ripgrep_bm25_uses_only_shared_files_and_handles_no_matches(tmp_path):
    files = {'short.cs': 'needle', 'long.cs': 'needle ' + 'padding ' * 100}
    for path, text in files.items():
        (tmp_path / path).write_text(text, encoding='utf-8')
    (tmp_path / 'outside.cs').write_text('needle ' * 100, encoding='utf-8')
    baseline = comparison.RipgrepBM25(tmp_path, files, shutil.which('rg'))
    assert baseline.search('needle') == ['short.cs', 'long.cs']
    assert baseline.search('absent') == []


def test_baseline_command_errors_cannot_be_reported_as_zero_recall(tmp_path, monkeypatch):
    baseline = comparison.RipgrepBM25(tmp_path, {'a.py': 'needle'}, 'rg')
    monkeypatch.setattr(comparison.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess([], 2, '', 'broken'))
    with pytest.raises(RuntimeError, match='broken'):
        baseline.search('needle')
