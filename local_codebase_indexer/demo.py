"""A bundled, synthetic first-success example; no database or .NET runtime needed."""
from __future__ import annotations

from importlib import resources
from pathlib import Path
import platform
import re
import tempfile
import time

from . import __version__
from .embedder import HashingEmbedder
from .query import CodebaseTools, render_data_access

TABLE = 'Sales_OrderHeader'


def _copy_sample(destination: Path) -> None:
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('Demo output must be a new or empty directory; existing files are never overwritten')
    destination.mkdir(parents=True, exist_ok=True)
    source = resources.files('local_codebase_indexer').joinpath('demo_project')
    def copy(directory, target):
        for item in sorted(directory.iterdir(), key=lambda item: item.name):
            if item.name in ('.ai_context', '__pycache__'):
                continue
            path = target / item.name
            if item.is_dir():
                path.mkdir()
                copy(item, path)
            elif item.is_file():
                path.write_bytes(item.read_bytes())
    copy(source, destination)


def _analyze(root: Path, retained: bool) -> dict:
    tools = CodebaseTools(str(root), embedder=HashingEmbedder())
    try:
        start = time.perf_counter()
        stats = tools.refresh()
        index_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        access = tools.data_access(TABLE)
        query_ms = (time.perf_counter() - start) * 1000
        relations = tools.symbol_relations('OrderRepository.Approve', 'upstream')
        direct = []
        # Literal baseline is deliberately narrow: exact table mentions in code/SQL,
        # not an estimate of what a person could infer by reading further files.
        pattern = re.compile(r'\b' + re.escape(TABLE) + r'\b', re.I)
        for path in sorted(root.rglob('*')):
            if path.is_file() and path.suffix.lower() in ('.cs', '.sql', '.cshtml') and '.ai_context' not in path.parts:
                if pattern.search(path.read_text(encoding='utf-8')):
                    direct.append(path.relative_to(root).as_posix())
        writers = access.get('writers', [])
        programs = access.get('programs', [])
        checks = {
            'stored_procedure_writer': any(w['source'].endswith('usp_ApproveOrder') for w in writers),
            'indirect_csharp_writer': any(w['source'].endswith('OrderRepository.Approve') and w['via_chain'] for w in writers),
            'controller_action': any(p['program'] == 'Orders' and 'Approve' in p['actions'] for p in programs),
            'razor_page': any('Views/Orders/Details.cshtml' in p.get('pages', []) for p in programs),
        }
        return {'schema_version': 1, 'sample_kind': 'synthetic', 'package_version': __version__,
                'scenario': f'Trace {TABLE} to C# code, a controller, and a Razor page',
                'environment': {'python': platform.python_version(), 'platform': platform.system(), 'embedder': tools.embedder.name},
                'sample_directory': str(root.resolve()) if retained else None,
                'index': stats.as_dict(), 'timing_ms': {'index': round(index_ms, 3), 'data_access': round(query_ms, 3)},
                'literal_reference_files': direct, 'data_access': access, 'relations': relations,
                'checks': checks, 'passed': not stats.failed and all(checks.values()),
                'limitations': ['This is a synthetic fixture, not a production case or a general recall benchmark.',
                                'Literal search counts exact mentions; it does not measure manual investigation time.',
                                'Timings are machine-dependent. Confidence levels describe static analysis, not runtime proof.']}
    finally:
        tools.indexer.close()


def run_demo(output: str | None = None) -> dict:
    if output is not None:
        root = Path(output).expanduser().absolute()
        _copy_sample(root)
        return _analyze(root, True)
    with tempfile.TemporaryDirectory(prefix='local-indexer-demo-') as directory:
        root = Path(directory)
        _copy_sample(root)
        return _analyze(root, False)


def render_demo(result: dict) -> str:
    lines = ['Local Codebase Indexer: SQL Server -> C# -> page',
             'Bundled synthetic sample. No SQL Server, .NET runtime or API key required.',
             f"Indexed {result['index']['scanned']} files; {result['index']['failed']} failed.", '',
             render_data_access(result['data_access'], full=True), '',
             f"Verified {sum(result['checks'].values())}/{len(result['checks'])} expected relationships: " +
             ('PASS' if result['passed'] else 'FAIL'),
             f"Exact table mentions: {len(result['literal_reference_files'])} files; controller/page reachability is computed separately.",
             f"Local timing: index {result['timing_ms']['index']:.1f} ms; data-access {result['timing_ms']['data_access']:.1f} ms.",
             'This fixture does not establish accuracy or time savings on your project.']
    if result['sample_directory']:
        lines.append('Sample saved to: ' + result['sample_directory'])
    lines += ['', 'Try your project:', '  local-codebase-indexer --root "/path/to/project" data-access YourTable']
    return '\n'.join(lines)
