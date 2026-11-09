"""Stateless search cursors bound to query settings and a complete index revision."""
from __future__ import annotations

import base64
import hashlib
import json

ALGORITHM = 'rrf-complete-v1'


def search_key(query: str, file_pattern: str | None, language: str | None,
               synonyms_version: str, embedder: str) -> str:
    payload = [ALGORITHM, query, file_pattern, language, synonyms_version, embedder]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode('utf-8')).hexdigest()


def encode_cursor(offset: int, revision: str, key: str) -> str:
    data = json.dumps({'v': 1, 'offset': offset, 'revision': revision, 'query': key}, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def decode_cursor(cursor: str, revision: str, key: str) -> int:
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
            raise ValueError()
        raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('v') != 1:
            raise ValueError()
        offset = data['offset']
        if type(offset) is not int or offset < 1:
            raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError('Invalid search cursor; start a new search.') from exc
    if data.get('revision') != revision or data.get('query') != key:
        raise ValueError('Search cursor no longer matches the index or query settings; start a new search.')
    return offset
