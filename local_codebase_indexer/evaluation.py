"""Evaluate user-authored navigation questions against an actual project index."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
import time

from . import __version__
from . import db as dbmod


def validate_dataset(dataset: dict) -> list[dict]:
    if not isinstance(dataset, dict) or dataset.get('version') != 1:
        raise ValueError('Evaluation dataset must be an object with version=1')
    queries = dataset.get('queries')
    if not isinstance(queries, list) or not queries:
        raise ValueError('Evaluation dataset must contain at least one query')
    seen = set()
    for i, item in enumerate(queries):
        if not isinstance(item, dict) or not isinstance(item.get('query'), str) or not item['query'].strip():
            raise ValueError(f'queries[{i}] requires a nonempty query')
        if not isinstance(item.get('id'), str) or not item['id'].strip() or item['id'] in seen:
            raise ValueError(f'queries[{i}] requires a unique nonempty id')
        seen.add(item['id'])
        relevant = item.get('relevant')
        if not isinstance(relevant, list) or not relevant:
            raise ValueError(f'queries[{i}] requires at least one relevant answer')
        gold = set()
        for answer in relevant:
            if not isinstance(answer, dict) or not isinstance(answer.get('file'), str) or not answer['file'].strip():
                raise ValueError(f'queries[{i}] answers require a file path')
            if 'symbol' in answer and (not isinstance(answer['symbol'], str) or not answer['symbol'].strip()):
                raise ValueError(f'queries[{i}] answer symbols must be nonempty strings')
            key = (answer['file'], answer.get('symbol'))
            if key in gold:
                raise ValueError(f'queries[{i}] contains duplicate answers')
            gold.add(key)
        for key in ('file_pattern', 'language'):
            if key in item and (not isinstance(item[key], str) or not item[key].strip()):
                raise ValueError(f'queries[{i}].{key} must be a nonempty string')
    return queries


def _matches(hit: dict, answer: dict) -> bool:
    return hit['file_path'] == answer['file'].replace('\\', '/') and (
        'symbol' not in answer or bool(hit['symbol'] and hit['symbol'].endswith(answer['symbol'])))


def evaluate(tools, dataset: dict, *, limit: int = 5, baseline: dict | None = None,
             min_recall: float | None = None, max_p95_ms: float | None = None,
             max_mean_tokens: float | None = None, max_recall_drop: float = 0.0) -> dict:
    from .query import render_search
    queries = validate_dataset(dataset)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Evaluation limit must be between 1 and 50')
    for name, value in (('min_recall', min_recall), ('max_p95_ms', max_p95_ms),
                        ('max_mean_tokens', max_mean_tokens), ('max_recall_drop', max_recall_drop)):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(f'{name} must be finite and nonnegative')
    if min_recall is not None and min_recall > 1 or max_recall_drop > 1:
        raise ValueError('Recall thresholds must be between 0 and 1')
    fingerprint = hashlib.sha256(json.dumps({'queries': queries, 'limit': limit},
                                           sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if baseline is not None:
        if not isinstance(baseline, dict) or baseline.get('schema_version') != 1 or baseline.get('dataset_sha256') != fingerprint:
            raise ValueError('Baseline must use the same queries, answers, filters, and result limit')
        old_recall = baseline.get('metrics', {}).get('recall_at_k')
        if not isinstance(old_recall, (int, float)) or not math.isfinite(old_recall) or not 0 <= old_recall <= 1:
            raise ValueError('Baseline is missing a valid recall_at_k metric')
    # Indexing time is reported separately from steady-state search latency.
    start = time.perf_counter()
    tools.refresh()
    index_ms = (time.perf_counter() - start) * 1000
    revision = dbmod.get_meta(tools.conn, 'index_revision')
    settings = tools.expander.version
    rows = []
    for question in queries:
        start = time.perf_counter()
        response = tools.semantic_search(question['query'], limit=limit,
                                         file_pattern=question.get('file_pattern'), language=question.get('language'))
        elapsed = (time.perf_counter() - start) * 1000
        if response['pagination']['index_revision'] != revision or tools.expander.version != settings:
            raise ValueError('Index or synonyms changed during evaluation; rerun with a stable project')
        ranks = [next((i for i, hit in enumerate(response['results'], 1) if _matches(hit, answer)), None)
                 for answer in question['relevant']]
        first = min((r for r in ranks if r is not None), default=None)
        rows.append({'id': question['id'], 'query': question['query'],
                     'recall_at_k': sum(r is not None for r in ranks) / len(ranks),
                     'reciprocal_rank': 1 / first if first else 0.0, 'first_relevant_rank': first,
                     'latency_ms': round(elapsed, 3),
                     'estimated_tokens': math.ceil(len(render_search(response)) / 4),
                     'results': [{'file': hit['file_path'], 'symbol': hit['symbol']} for hit in response['results']]})
    latencies = sorted(row['latency_ms'] for row in rows)
    metrics = {'queries': len(rows), 'limit': limit,
               'recall_at_k': statistics.mean(row['recall_at_k'] for row in rows),
               'hit_rate_at_k': statistics.mean(row['first_relevant_rank'] is not None for row in rows),
               'mrr': statistics.mean(row['reciprocal_rank'] for row in rows),
               'p50_ms': statistics.median(latencies),
               'p95_ms': latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)],
               'mean_estimated_tokens': statistics.mean(row['estimated_tokens'] for row in rows)}
    failures = []
    if min_recall is not None and metrics['recall_at_k'] < min_recall:
        failures.append(f"recall_at_k {metrics['recall_at_k']:.4f} is below {min_recall:.4f}")
    if max_p95_ms is not None and metrics['p95_ms'] > max_p95_ms:
        failures.append(f"p95_ms {metrics['p95_ms']:.3f} exceeds {max_p95_ms:.3f}")
    if max_mean_tokens is not None and metrics['mean_estimated_tokens'] > max_mean_tokens:
        failures.append(f"mean_estimated_tokens {metrics['mean_estimated_tokens']:.1f} exceeds {max_mean_tokens:.1f}")
    delta = None
    if baseline is not None:
        delta = metrics['recall_at_k'] - old_recall
        if delta + 1e-12 < -max_recall_drop:
            failures.append(f'recall_at_k regressed by {-delta:.4f}, exceeding {max_recall_drop:.4f}')
    return {'schema_version': 1, 'package_version': __version__, 'dataset_sha256': fingerprint,
            'root': tools.root, 'index_revision': revision, 'embedder': tools.embedder.name,
            'synonyms_version': settings, 'index_ms': round(index_ms, 3),
            'metrics': metrics, 'rows': rows, 'baseline_recall_delta': delta,
            'passed': not failures, 'failures': failures,
            'note': 'Tokens are estimated as ceil(rendered characters / 4), not model-specific tokenizer counts.'}


def save_report(report: dict, path: str) -> None:
    dest = Path(path).absolute()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=dest.parent, delete=False) as stream:
        temporary = stream.name
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    try:
        os.replace(temporary, dest)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_evaluation(report: dict) -> str:
    m = report['metrics']
    lines = [f"Evaluation: {m['queries']} queries, top {m['limit']}",
             f"Recall@{m['limit']}: {m['recall_at_k']:.3f}; MRR: {m['mrr']:.3f}",
             f"Latency p50/p95: {m['p50_ms']:.1f}/{m['p95_ms']:.1f} ms",
             f"Mean estimated output tokens: {m['mean_estimated_tokens']:.1f}",
             'PASS' if report['passed'] else 'FAIL']
    lines.extend('  ' + failure for failure in report['failures'])
    lines.append(report['note'])
    return '\n'.join(lines)
