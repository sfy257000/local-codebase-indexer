import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import zipfile
import io

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_assets', ROOT / 'scripts' / 'release_assets.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def artifacts(tmp_path):
    root = tmp_path / 'source'
    (root / 'local_codebase_indexer').mkdir(parents=True)
    (root / 'local_codebase_indexer' / '__init__.py').write_text('__version__ = "1.2.3"\n')
    (root / 'server.json').write_text(json.dumps({'version': '1.2.3', 'packages': [{'version': '1.2.3'}]}))
    dist = tmp_path / 'dist'; dist.mkdir()
    with zipfile.ZipFile(dist / 'local_codebase_indexer-1.2.3-py3-none-any.whl', 'w') as archive:
        archive.writestr('local_codebase_indexer-1.2.3.dist-info/METADATA', 'Name: local-codebase-indexer\nVersion: 1.2.3\n')
        archive.writestr('local_codebase_indexer/__init__.py', '__version__ = "1.2.3"\n')
    def source_archive(extra=None):
        with tarfile.open(dist / 'local_codebase_indexer-1.2.3.tar.gz', 'w:gz') as archive:
            files = {'PKG-INFO': b'Name: local-codebase-indexer\nVersion: 1.2.3\n', 'bin/indexer': b'launcher',
                     'CHANGELOG.md': b'changes', 'local_codebase_indexer/__init__.py': b'__version__ = "1.2.3"\n'}
            files.update(extra or {})
            for name, data in files.items():
                member = tarfile.TarInfo('local_codebase_indexer-1.2.3/' + name); member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
    source_archive()
    return root, dist, source_archive


def test_release_preparation_validates_versions_and_checksums(artifacts):
    root, dist, _ = artifacts
    files = release.prepare_assets(root, dist, 'v1.2.3')
    assert len(files) == 3
    manifest = (dist / 'SHA256SUMS').read_text()
    for path in files[:2]:
        assert f'{release.checksum(path)}  {path.name}' in manifest
    with pytest.raises(ValueError, match='tag must match'):
        release.prepare_assets(root, dist, 'v1.2.4')
    (root / 'server.json').write_text('{"version":"9.9.9"}')
    with pytest.raises(ValueError, match='server.json'):
        release.prepare_assets(root, dist, 'v1.2.3')


def test_generated_indexes_cannot_be_published(artifacts):
    root, dist, write_source = artifacts
    write_source({'benchmarks/corpus/.ai_context/indexer.db': b'generated index'})
    with pytest.raises(ValueError, match='generated state'):
        release.prepare_assets(root, dist, 'v1.2.3')


def _remote(files):
    return {'tag_name': 'v1.2.3', 'draft': False,
            'assets': [{'name': path.name, 'state': 'uploaded', 'size': path.stat().st_size,
                        'digest': 'sha256:' + release.checksum(path)} for path in files]}


def test_repeated_upload_skips_verified_assets_without_network_writes(artifacts, monkeypatch):
    root, dist, _ = artifacts
    files = release.prepare_assets(root, dist, 'v1.2.3')
    monkeypatch.setattr(release, '_release', lambda *args: _remote(files))
    def no_write(*args, **kwargs):
        pytest.fail('Identical release assets must not be uploaded again')
    monkeypatch.setattr(release.subprocess, 'run', no_write)
    release.upload_assets('owner/repo', 'v1.2.3', files)


def test_existing_asset_conflicts_fail_without_overwriting(artifacts, monkeypatch):
    root, dist, _ = artifacts
    files = release.prepare_assets(root, dist, 'v1.2.3')
    remote = _remote(files); remote['assets'][0]['digest'] = 'sha256:wrong'
    monkeypatch.setattr(release, '_release', lambda *args: remote)
    with pytest.raises(ValueError, match='refusing to overwrite'):
        release.upload_assets('owner/repo', 'v1.2.3', files)


def test_missing_asset_uploads_are_verified_afterwards(artifacts, monkeypatch):
    root, dist, _ = artifacts
    files = release.prepare_assets(root, dist, 'v1.2.3')
    calls = []
    responses = iter([{'tag_name': 'v1.2.3', 'draft': False, 'assets': []}, _remote(files)])
    monkeypatch.setattr(release, '_release', lambda *args: next(responses))
    monkeypatch.setattr(release.subprocess, 'run', lambda command, **kw: calls.append(command))
    release.upload_assets('owner/repo', 'v1.2.3', files)
    assert len(calls) == 3 and all(call[:3] == ['gh', 'release', 'upload'] for call in calls)
    assert all('--clobber' not in command for command in calls)
