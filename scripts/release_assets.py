#!/usr/bin/env python3
"""Validate one built release and upload its assets idempotently using GitHub CLI.

Run locally without --upload to validate artifacts and write SHA256SUMS. GitHub
Actions supplies its built-in token only to the upload step.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from urllib.parse import quote


def version_from_source(data: bytes) -> str:
    tree = ast.parse(data.decode('utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
            version = ast.literal_eval(node.value)
            if isinstance(version, str):
                return version
    raise ValueError('Package source has no __version__ string')


def checksum(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def prepare_assets(root: Path, dist: Path, tag: str) -> list[Path]:
    version = version_from_source((root / 'local_codebase_indexer' / '__init__.py').read_bytes())
    if not re.fullmatch(r'v[0-9][A-Za-z0-9.+-]*', tag) or tag != 'v' + version:
        raise ValueError('Release tag must match the checked-out package version')
    server = json.loads((root / 'server.json').read_text(encoding='utf-8'))
    if server.get('version') != version or any(p.get('version') != version for p in server.get('packages', [])):
        raise ValueError('server.json versions must match the package version')
    wheel = dist / f'local_codebase_indexer-{version}-py3-none-any.whl'
    source = dist / f'local_codebase_indexer-{version}.tar.gz'
    if set(dist.glob('*.whl')) != {wheel} or set(dist.glob('*.tar.gz')) != {source}:
        raise ValueError('dist must contain exactly the wheel and source archive for this version')
    with zipfile.ZipFile(wheel) as archive:
        metadata = BytesParser().parsebytes(archive.read(f'local_codebase_indexer-{version}.dist-info/METADATA'))
        if metadata.get('Name', '').lower().replace('_', '-') != 'local-codebase-indexer' or metadata.get('Version') != version or version_from_source(archive.read('local_codebase_indexer/__init__.py')) != version:
            raise ValueError('Wheel version does not match the release tag')
    prefix = f'local_codebase_indexer-{version}/'
    with tarfile.open(source) as archive:
        names = archive.getnames()
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '.ai_context' in path.parts or '__pycache__' in path.parts:
                raise ValueError('Source archive contains generated state or an invalid path')
        for required in ('PKG-INFO', 'bin/indexer', 'CHANGELOG.md', 'local_codebase_indexer/__init__.py'):
            if prefix + required not in names:
                raise ValueError('Source archive is missing ' + required)
        with archive.extractfile(prefix + 'PKG-INFO') as stream:
            metadata = BytesParser().parsebytes(stream.read())
        with archive.extractfile(prefix + 'local_codebase_indexer/__init__.py') as stream:
            source_version = version_from_source(stream.read())
        if metadata.get('Name', '').lower().replace('_', '-') != 'local-codebase-indexer' or metadata.get('Version') != version or source_version != version:
            raise ValueError('Source archive version does not match the release tag')
    files = [wheel, source]
    manifest = dist / 'SHA256SUMS'
    manifest.write_text(''.join(f'{checksum(path)}  {path.name}\n' for path in files), encoding='utf-8')
    return files + [manifest]


def _release(repo: str, tag: str) -> dict:
    result = subprocess.run(['gh', 'api', f'repos/{repo}/releases/tags/{quote(tag, safe="")}'],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _matches(asset: dict, path: Path) -> bool:
    return (asset.get('state') == 'uploaded' and asset.get('size') == path.stat().st_size
            and asset.get('digest') == 'sha256:' + checksum(path))


def upload_assets(repo: str, tag: str, files: list[Path]) -> None:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Repository must be owner/name')
    release = _release(repo, tag)
    if release.get('draft') or release.get('tag_name') != tag:
        raise ValueError('Upload requires the existing published release for this tag')
    existing = {asset['name']: asset for asset in release['assets']}
    for path in files:
        if path.name in existing:
            if not _matches(existing[path.name], path):
                raise ValueError(f'Existing asset differs: {path.name}; refusing to overwrite it')
            print('Already verified: ' + path.name)
        else:
            subprocess.run(['gh', 'release', 'upload', tag, '--repo', repo, str(path)], check=True)
    actual = {asset['name']: asset for asset in _release(repo, tag)['assets']}
    for path in files:
        if path.name not in actual or not _matches(actual[path.name], path):
            raise ValueError('Uploaded asset did not pass checksum verification: ' + path.name)
    print(f'Uploaded and verified {len(files)} release assets')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--dist', type=Path, default=Path('dist'))
    parser.add_argument('--tag', required=True)
    parser.add_argument('--repo')
    parser.add_argument('--upload', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.upload and not args.repo:
            raise ValueError('--upload requires --repo')
        files = prepare_assets(args.root, args.dist, args.tag)
        if args.upload:
            upload_assets(args.repo, args.tag, files)
        else:
            print('Validated: ' + ', '.join(path.name for path in files))
    except (ValueError, OSError, KeyError, tarfile.TarError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
