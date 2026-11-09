"""Compare current and historical symbols; removed code retains its old callers and SQL accesses."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from collections import defaultdict

from . import __version__, dataflow
from . import db as dbmod
from .core import git
from .embedder import HashingEmbedder

_TYPES = {'class', 'interface', 'struct', 'record', 'enum', 'type', 'namespace'}


def _path(header: str) -> str | None:
    name = header[4:]
    if name.startswith('"'):
        name = json.loads(name)
    name = name.rstrip('\t')
    return name[2:] if name.startswith(('a/', 'b/')) else None


def _collect(tools, ranges: dict, snapshot: str) -> dict:
    changed, unmapped = {}, []
    for path, spans in sorted(ranges.items()):
        hit = False
        for a, b in spans:
            rows = tools.conn.execute(
                "SELECT * FROM symbols WHERE file_path=? AND start_line<=? AND end_line>=? "
                "AND symbol_type != 'namespace' ORDER BY start_line, end_line", (path, b, a)).fetchall()
            leaves = [r for r in rows if r['symbol_type'] not in _TYPES]
            # Exclude enclosing containers, but include every separate method in a hunk.
            leaves = [r for r in leaves if not any(
                q['id'] != r['id'] and r['start_line'] <= q['start_line'] and r['end_line'] >= q['end_line']
                and (r['start_line'], r['end_line']) != (q['start_line'], q['end_line']) for q in leaves)] or rows[:1]
            for r in leaves:
                hit = True
                c = changed.setdefault(r['id'], {
                    'id': r['id'], 'symbol': r['qualified_name'] or r['symbol_name'],
                    'type': r['symbol_type'], 'file_path': path, 'lines': [], 'snapshot': snapshot})
                c['lines'].append([max(a, r['start_line']), min(b, r['end_line'])])
        if not hit:
            unmapped.append(path)
    own = []
    for sid, c in changed.items():
        own.extend({'source': c['symbol'], 'target': r['target_name'], 'access': r['access'],
                    'op': r['op'], 'confidence': r['confidence'], 'file_path': r['file_path'],
                    'line': r['line'], 'snapshot': snapshot}
                   for r in tools.conn.execute("SELECT * FROM data_access WHERE source_id=? AND via != 'ef_raw'", (sid,)))
    entries = [{'source_id': c['id'], 'source_type': c['type'], 'access': 'change', 'confidence': 'confirmed'}
               for c in changed.values()]
    programs, roots = dataflow._programs(tools.conn, dataflow._Lookup(tools.conn), entries)
    for entry in programs + roots:
        entry['snapshot'] = snapshot
    return {'changed': list(changed.values()), 'unmapped_files': unmapped, 'tables': own,
            'programs': programs, 'no_known_caller': roots}


def _historical(tools, revision: str, ranges: dict) -> dict:
    from .query import CodebaseTools
    cache = os.path.join(tools.root, '.ai_context', 'impact', f'{revision}-{dbmod.INDEX_VERSION}-{__version__}.db')
    if os.path.isfile(cache):
        old = CodebaseTools(tools.root, auto_refresh=False, embedder=HashingEmbedder(), db_path=cache)
        try:
            if dbmod.get_meta(old.conn, 'last_indexed_at'):
                return _collect(old, ranges, 'before')
        finally:
            old.indexer.close()
    with tempfile.TemporaryDirectory(prefix='indexer-impact-') as tmp:
        snapshot = os.path.join(tmp, 'source')
        os.mkdir(snapshot)
        archive = os.path.join(tmp, 'base.tar')
        with open(archive, 'wb') as out:
            try:
                proc = subprocess.run(['git', '-C', tools.root, 'archive', '--format=tar', revision],
                                      stdout=out, stderr=subprocess.PIPE, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError('could not export the base revision for impact analysis') from exc
        if proc.returncode:
            raise ValueError('could not read the base revision for historical impact analysis')
        # Git symlinks and special files are not indexed; never extract them.
        with tarfile.open(archive) as tar:
            for member in tar:
                dest = os.path.abspath(os.path.join(snapshot, member.name))
                if os.path.commonpath([snapshot, dest]) != snapshot:
                    raise ValueError('invalid path in git archive')
                if member.isfile():
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with tar.extractfile(member) as src, open(dest, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
        old = CodebaseTools(snapshot, embedder=HashingEmbedder())
        try:
            stats = old.refresh()
            if stats.failed:
                raise ValueError('base revision could not be indexed completely: ' + '; '.join(stats.warnings[:3]))
            result = _collect(old, ranges, 'before')
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            # Publish only a complete baseline; readers never observe a partial build.
            with tempfile.NamedTemporaryFile(dir=os.path.dirname(cache), suffix='.db', delete=False) as out:
                completed = out.name
            try:
                dest = sqlite3.connect(completed)
                try:
                    old.conn.backup(dest)
                finally:
                    dest.close()
                os.replace(completed, cache)
            finally:
                if os.path.exists(completed):
                    os.unlink(completed)
            return result
        finally:
            old.indexer.close()


def analyze_impact(tools, base: str | None) -> dict:
    if base is not None and (not base.strip() or base.startswith('-')):
        raise ValueError('base must be a git revision or range')
    if tools.auto_refresh:
        tools.refresh()
    diff = git(tools.root, '-c', 'core.quotePath=false', 'diff', '--unified=0', '--no-color',
               '--no-ext-diff', '--no-renames', '--relative', base or 'HEAD', timeout=60)
    if diff is None:
        raise ValueError(f"git diff failed for {base or 'HEAD'} (not a git repository, or unknown revision)")
    before, after = defaultdict(list), defaultdict(list)
    deleted = []
    oldpath = newpath = None
    for line in diff.splitlines():
        if line.startswith('--- '):
            oldpath = _path(line)
        elif line.startswith('+++ '):
            newpath = _path(line)
            if newpath is None and oldpath:
                deleted.append(oldpath)
        elif line.startswith('@@'):
            m = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if m:
                oldstart, oldcount, newstart, newcount = (int(v) if v is not None else 1 for v in m.groups())
                if oldpath and oldcount:
                    before[oldpath].append((oldstart, oldstart + oldcount - 1))
                if newpath and newcount:
                    after[newpath].append((newstart, newstart + newcount - 1))
    if base is None:
        for path in (git(tools.root, 'ls-files', '-z', '--others', '--exclude-standard') or '').split('\0'):
            if path:
                after[path].append((1, 10**9))
    result = _collect(tools, after, 'current')
    if before:
        oldref = (base or 'HEAD').split('..', 1)[0]
        if base and '...' in base:
            left, right = base.split('...', 1)
            oldref = (git(tools.root, 'merge-base', left or 'HEAD', right or 'HEAD') or '').strip()
        revision = git(tools.root, 'rev-parse', '--verify', oldref + '^{commit}')
        if not revision:
            raise ValueError('cannot resolve base revision')
        old = _historical(tools, revision.strip(), before)
        keys = { (c['file_path'], c['symbol'], c['type']) for c in result['changed'] }
        result['changed'] += [c for c in old['changed'] if (c['file_path'], c['symbol'], c['type']) not in keys]
        # Keep old accesses too: removing a write or caller is itself an impact.
        for field in ('tables', 'programs', 'no_known_caller'):
            seen = {json.dumps({k:v for k,v in row.items() if k not in ('snapshot','line')}, sort_keys=True)
                    for row in result[field]}
            for row in old[field]:
                key = json.dumps({k:v for k,v in row.items() if k not in ('snapshot','line')}, sort_keys=True)
                if key not in seen:
                    result[field].append(row)
                    seen.add(key)
        result['unmapped_files'] = sorted(set(result['unmapped_files']) | set(old['unmapped_files']))
    result.update(base=base or 'HEAD (uncommitted)', removed_files=sorted(set(deleted)),
                  note='Current lines refer to the working tree; before lines refer to the base revision. Historical analysis excludes external extra roots.')
    return result
