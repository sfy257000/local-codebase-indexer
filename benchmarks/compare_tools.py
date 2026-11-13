#!/usr/bin/env python3
"""Matched file-retrieval comparison against ripgrep + BM25, not a Claude agent test.

Queries/gold come unchanged from the existing suites. Both methods get the same
files/query; relevance is scored at file level for both (symbol suffixes ignored).
The wrapper gives rg a deterministic ranking it does not natively provide.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from run_benchmark import prepare_corpus  # noqa: E402

from local_codebase_indexer import __version__  # noqa: E402
from local_codebase_indexer import db as dbmod  # noqa: E402
from local_codebase_indexer.demo import _copy_sample  # noqa: E402
from local_codebase_indexer.embedder import HashingEmbedder  # noqa: E402
from local_codebase_indexer.query import CodebaseTools, render_search  # noqa: E402
from local_codebase_indexer.textdecode import decode_bytes  # noqa: E402

# Generic query stop words, not domain synonyms or terms learned from gold.
STOP = set('a an the of to in is are for and or where do we how what which that this with on it be does can find show me by at as from into i my our there when who so up'.split())
WORDS = re.compile(r'[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+')


def lexemes(text: str) -> list[str]:
    result = []
    for word in re.findall(r'[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]+|\d+', text):
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            candidates = [word[i:i + 2] for i in range(len(word) - 1)] or [word]
        else:
            candidates = [word.lower(), *[part.lower() for piece in word.split('_') for part in WORDS.findall(piece)]]
        # Deduplicate within one identifier occurrence, not across the document.
        result.extend(candidate for candidate in dict.fromkeys(candidates) if len(candidate) > 1 and candidate not in STOP)
    return result


def terms(text: str) -> list[str]:
    return list(dict.fromkeys(lexemes(text)))


def file_scores(paths: list[str], gold: set[str], k: int = 5) -> dict:
    if not gold:
        raise ValueError('Gold files must not be empty')
    ranked = list(dict.fromkeys(paths))[:k]
    found = gold.intersection(ranked)
    first = next((i for i, path in enumerate(ranked, 1) if path in gold), None)
    return {'recall5': len(found) / len(gold), 'rr5': 1 / first if first else 0.0,
            'found_files': sorted(found), 'top_files': ranked}


def reduction_percent(actual: int, reference: int) -> float | None:
    return 100 * (1 - actual / reference) if reference else None


class RipgrepBM25:
    """Actual rg candidate files + BM25 (k1=1.2, b=.75); no gold-aware rewriting."""
    def __init__(self, root: Path, files: dict[str, str], executable: str):
        self.root, self.files, self.executable = root, files, executable
        self.counts = {path: Counter(lexemes(text)) for path, text in files.items()}
        self.lengths = {path: sum(counter.values()) for path, counter in self.counts.items()}
        self.frequency = Counter(word for counter in self.counts.values() for word in counter)
        self.mean_length = statistics.mean(self.lengths.values()) or 1

    def search(self, query: str) -> list[str]:
        words = terms(query)
        if not words:
            return []
        pattern = '|'.join(re.escape(word) for word in sorted(words, key=lambda word: (-len(word), word)))
        command = [self.executable, '--no-config', '--files-with-matches', '--text', '--ignore-case', '-e', pattern,
                   '--', *sorted(self.files)]
        proc = subprocess.run(command, cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=60)
        if proc.returncode not in (0, 1):
            raise RuntimeError('ripgrep failed: ' + proc.stderr)
        scores = {}
        for line in proc.stdout.splitlines():
            path = line.replace('\\', '/')
            score = 0.0
            norm = 1.2 * (1 - 0.75 + 0.75 * self.lengths[path] / self.mean_length)
            for word in words:
                count = self.counts[path][word]
                if count:
                    idf = math.log(1 + (len(self.files) - self.frequency[word] + 0.5) / (self.frequency[word] + 0.5))
                    score += idf * count * 2.2 / (count + norm)
            if score:
                scores[path] = score
        return sorted(scores, key=lambda path: (-scores[path], path))[:5]


def _mean(rows: list[dict], method: str, key: str) -> float:
    return statistics.mean(row[method][key] for row in rows)


def summarize(rows: list[dict]) -> dict:
    result = {}
    for method in ('rg_bm25', 'indexer'):
        result[method] = {'file_recall5': _mean(rows, method, 'recall5'),
                          'file_mrr5': _mean(rows, method, 'rr5'),
                          'median_query_ms': statistics.median(value for row in rows for value in row[method]['query_ms'])}
    result['recall_gain_pp'] = 100 * (result['indexer']['file_recall5'] - result['rg_bm25']['file_recall5'])
    snippets = sum(row['indexer']['output_chars'] for row in rows)
    whole = sum(row['indexer']['same_files_full_read_chars'] for row in rows)
    result['context'] = {'returned_chars': snippets, 'same_files_full_read_chars': whole,
                         'reduction_pct': reduction_percent(snippets, whole)}
    return result


def compare_suite(name: str, repeats: int, workdir: str, rg: str) -> tuple[dict, list[dict]]:
    query_file = HERE / ('queries_stdlib.json' if name == 'stdlib' else 'queries_synthetic.json')
    dataset = json.loads(query_file.read_text(encoding='utf-8'))
    root = Path(prepare_corpus(dataset, name, name == 'synthetic-noisy', workdir))
    shutil.rmtree(root / '.ai_context', ignore_errors=True)
    tools = CodebaseTools(str(root), auto_refresh=False, embedder=HashingEmbedder())
    try:
        start = time.perf_counter()
        stats = tools.refresh()
        index_ms = (time.perf_counter() - start) * 1000
        if stats.failed:
            raise RuntimeError('Indexing failed: ' + '; '.join(stats.warnings))
        files = {row[0]: decode_bytes((root / row[0]).read_bytes()) for row in tools.conn.execute('SELECT file_path FROM files')}
        corpus_hash = hashlib.sha256()
        for path in sorted(files):
            corpus_hash.update(path.encode('utf-8') + b'\0' + files[path].encode('utf-8') + b'\0')
        start = time.perf_counter()
        baseline = RipgrepBM25(root, files, rg)
        baseline_setup_ms = (time.perf_counter() - start) * 1000
        rows = []
        for number, item in enumerate(dataset['queries'], 1):
            query = item['query']
            gold = {answer['file'] for answer in item['relevant']}
            if not gold <= files.keys():
                raise ValueError('Gold files missing from shared corpus')
            # Warm both methods; alternate measured order to reduce cache/order bias.
            baseline.search(query)
            tools.semantic_search(query, limit=5)
            rg_times, indexer_times = [], []
            ranked = []
            response = {}
            for trial in range(repeats):
                for method in (('rg', 'indexer') if trial % 2 == 0 else ('indexer', 'rg')):
                    start = time.perf_counter()
                    if method == 'rg':
                        ranked = baseline.search(query)
                        rg_times.append(round((time.perf_counter() - start) * 1000, 3))
                    else:
                        response = tools.semantic_search(query, limit=5)
                        indexer_times.append(round((time.perf_counter() - start) * 1000, 3))
            selected = [hit['file_path'] for hit in response['results']]
            rg_score = file_scores(ranked, gold)
            indexer_score = file_scores(selected, gold)
            # Context comparison holds the selected files fixed. This reference
            # is whole-file reading, not rg's tiny filename output or rg -C.
            whole_chars = sum(len(path) + 1 + len(files[path]) + 1 for path in set(selected))
            rows.append({'suite': name, 'query_number': number, 'category': item['category'],
                         'query': query, 'gold_files': sorted(gold), 'keyword_terms': terms(query),
                         'rg_bm25': {**rg_score, 'query_ms': rg_times},
                         'indexer': {**indexer_score, 'query_ms': indexer_times,
                                     'output_chars': len(render_search(response)), 'same_files_full_read_chars': whole_chars}})
        return {'queries': len(rows), 'files': len(files), 'chunks': stats.chunks,
                'dataset_sha256': hashlib.sha256(query_file.read_bytes()).hexdigest(),
                'corpus_sha256': corpus_hash.hexdigest(), 'initial_index_ms': index_ms,
                'rg_bm25_setup_ms': baseline_setup_ms, **summarize(rows)}, rows
    finally:
        tools.indexer.close()



def evidence_files(response: dict) -> list[str]:
    paths = set()
    for section in ('definitions', 'readers', 'writers', 'executed_by', 'mapped_by', 'schema_changes',
                    'possible', 'programs', 'no_known_caller'):
        for entry in response.get(section, []):
            if entry.get('file_path'):
                paths.add(entry['file_path'])
            paths.update(entry.get('pages', []))
    return sorted(paths)


def compare_navigation(workdir: str, repeats: int, rg: str) -> dict:
    task_file = HERE / 'navigation_task.json'
    task = json.loads(task_file.read_text(encoding='utf-8'))
    root = Path(workdir) / 'navigation'
    _copy_sample(root)
    gold = set(task['required_files'])
    command = [rg, '--no-config', '-l', '-i', '-w', '-F', '-e', task['target'],
               '-g', '*.cs', '-g', '*.sql', '-g', '*.cshtml', '--', str(root)]
    rg_times = []
    rg_paths = []
    for trial in range(repeats + 1):
        start = time.perf_counter()
        proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=60)
        if proc.returncode not in (0, 1):
            raise RuntimeError(proc.stderr)
        if trial:
            rg_times.append(round((time.perf_counter() - start) * 1000, 3))
        rg_paths = sorted(Path(line).relative_to(root).as_posix() for line in proc.stdout.splitlines())
    command = [sys.executable, '-m', 'local_codebase_indexer', '--root', str(root),
               'data-access', task['target'], '--json']
    cli_times = []
    child_env = dict(os.environ)
    child_env.pop('INDEXER_ONNX_MODEL', None)  # Fix the measured embedder even with ambient optional settings.
    response = {}
    for trial in range(repeats + 1):
        start = time.perf_counter()
        proc = subprocess.run(command, cwd=HERE.parent, env=child_env, capture_output=True, text=True, encoding='utf-8', timeout=60, check=True)
        elapsed = round((time.perf_counter() - start) * 1000, 3)
        if trial:
            cli_times.append(elapsed)
        else:
            initial_cli_ms = elapsed
        response = json.loads(proc.stdout)
    paths = evidence_files(response)
    rg_coverage = len(gold.intersection(rg_paths)) / len(gold)
    indexer_coverage = len(gold.intersection(paths)) / len(gold)
    return {'sample_kind': 'bundled synthetic fixture; five required evidence files',
            'task_sha256': hashlib.sha256(task_file.read_bytes()).hexdigest(),
            'target': task['target'], 'required_files': sorted(gold),
            'literal_rg': {'files': rg_paths, 'coverage': rg_coverage, 'cli_ms': rg_times},
            'indexer': {'files': paths, 'coverage': indexer_coverage, 'initial_cli_ms': initial_cli_ms,
                        'warm_cli_ms': cli_times, 'program_confidence': [p['confidence'] for p in response['programs']]},
            'coverage_gain_pp': 100 * (indexer_coverage - rg_coverage),
            'limitation': 'One table-name grep call, not a multi-step agent or expert-grep workflow; five task-specific required files, not all potentially relevant source.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=['synthetic', 'synthetic-noisy', 'stdlib', 'all'], default='all')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--navigation', action='store_true', help='also measure the source-evidence task and actual CLI latency')
    parser.add_argument('--save', type=Path, required=True)
    parser.add_argument('--details', type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    if args.save.resolve() == args.details.resolve():
        parser.error('--save and --details must be different files')
    rg = shutil.which('rg')
    if not rg:
        parser.error('Install ripgrep (rg) to run this optional comparison')
    source_hash = hashlib.sha256()
    for path in sorted((HERE.parent / 'local_codebase_indexer').glob('*.py')):
        source_hash.update(path.name.encode() + b'\0' + path.read_bytes())
    report = {'schema_version': 1, 'indexer_version': __version__, 'index_format': dbmod.INDEX_VERSION, 'indexer_source_sha256': source_hash.hexdigest(),
              'sqlite_version': sqlite3.sqlite_version, 'recorded_at_utc': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(), 'platform': platform.platform(),
              'rg_version': subprocess.check_output([rg, '--version'], text=True).splitlines()[0],
              'repeats': args.repeats, 'indexer': 'HashingEmbedder; default 5 chunks; frozen corpus; warm process',
              'baseline': 'rg case-insensitive OR candidates + BM25 token counts (k1=1.2, b=0.75); top 5 files',
              'units': {'quality': 'macro file Recall@5 and file MRR@5; symbols ignored for both methods',
                        'latency': 'warm indexer method excludes transport/startup; baseline includes rg subprocess + ranking',
                        'context': 'exact Unicode character counts vs full reads of the same indexer-selected files; not model tokens'},
              'limitations': ['Not Claude, Serena, or Claude Context end-to-end runs.',
                              'Synthetic queries/corpus/domain synonyms were authored by this project; absolute quality is optimistic.',
                              'Noisy suite repeats the same 42 questions; stdlib is real Python source with 12 authored questions.',
                              'Timings depend on the machine and are not comparable to CLI/model/agent latency.'],
              'suites': {}}
    details = []
    with tempfile.TemporaryDirectory(prefix='indexer-tool-comparison-') as workdir:
        for name in ['synthetic', 'synthetic-noisy', 'stdlib']:
            if args.suite not in (name, 'all'):
                continue
            summary, rows = compare_suite(name, args.repeats, workdir, rg)
            report['suites'][name] = summary
            details.extend(rows)
            print(name, json.dumps(summary, ensure_ascii=False))
        if args.navigation:
            report['navigation'] = compare_navigation(workdir, args.repeats, rg)
            print('navigation', json.dumps(report['navigation'], ensure_ascii=False))
    args.save.parent.mkdir(parents=True, exist_ok=True)
    args.details.parent.mkdir(parents=True, exist_ok=True)
    args.save.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    args.details.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in details), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
