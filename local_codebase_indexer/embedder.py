"""Semantic layer: local embedding backends (384 dimensions, no cloud calls).

Two backends:

* ``HashingEmbedder`` (default): dependency-light signed feature hashing over
  identifier sub-words, whole identifiers, bigrams and CJK character bigrams.
  It is a lexical-semantic approximation, deterministic and fast, but it does
  not understand synonyms.
* ``OnnxEmbedder``: real transformer embeddings (all-MiniLM-L6-v2 or
  bge-small-en-v1.5 exported to ONNX). Enabled by setting ``INDEXER_ONNX_MODEL``
  to a directory containing ``model.onnx`` and ``tokenizer.json``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import zlib
from typing import Sequence

import numpy as np

DIM = 384

_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+|[一-鿿]+")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STOP = {
    "the", "a", "an", "of", "to", "in", "is", "for", "and", "or", "if", "else", "return",
    "var", "new", "this", "public", "private", "protected", "static", "void", "int", "string",
    "bool", "true", "false", "null", "self", "def", "class", "import", "from", "using", "get", "set",
}


def split_identifier(token: str) -> list[str]:
    return [p.lower() for p in _WORD_RE.findall(token.replace("_", " "))]


def tokenize(text: str) -> list[str]:
    """Words for lexical features: whole identifiers plus their camelCase/snake_case parts."""
    out: list[str] = []
    for ident in _IDENT_RE.findall(text):
        parts = split_identifier(ident)
        if len(parts) > 1:
            out.append(ident.lower())
        out.extend(parts)
    for run in re.findall(r"[一-鿿]+", text):
        out.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return out


class HashingEmbedder:
    name = "hashing-384-v1"
    dim = DIM

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(DIM, dtype=np.float32)
        toks = [t for t in tokenize(text) if t not in _STOP and len(t) > 1]
        if not toks:
            return v
        counts: dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        for i in range(len(toks) - 1):
            bg = toks[i] + " " + toks[i + 1]
            counts[bg] = counts.get(bg, 0) + 1
        for feat, c in counts.items():
            h = zlib.crc32(feat.encode("utf-8"))
            idx = h % DIM
            sign = 1.0 if (h >> 16) & 1 else -1.0
            v[idx] += sign * (1.0 + math.log(c))
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, DIM), np.float32)


def _model_files(model_dir: str):
    for directory, dirs, files in os.walk(model_dir):
        dirs[:] = sorted(d for d in dirs if d not in ('.git', '.cache', '__pycache__', '.ai_context', '.venv', 'venv'))
        for filename in sorted(files):
            path = os.path.join(directory, filename)
            if os.path.isfile(path):
                yield path


def _model_state(model_dir: str) -> tuple:
    state = []
    for path in _model_files(model_dir):
        stat = os.stat(path)
        state.append((os.path.relpath(path, model_dir), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
    return tuple(state)


class OnnxEmbedder:
    dim = DIM

    def __init__(self, model_dir: str):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.model_dir = os.path.abspath(model_dir)
        self._state = _model_state(self.model_dir)
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tok.enable_truncation(max_length=256)
        self.tok.enable_padding()
        self.session = ort.InferenceSession(
            os.path.join(model_dir, "model.onnx"), providers=["CPUExecutionProvider"]
        )
        self.input_names = {i.name for i in self.session.get_inputs()}
        # bge models pool the [CLS] token and expect an instruction prefix on queries; MiniLM-style models mean-pool
        self.pooling = os.environ.get("INDEXER_ONNX_POOLING", "mean").lower()
        if self.pooling not in ("mean", "cls"):
            raise ValueError("INDEXER_ONNX_POOLING must be 'mean' or 'cls'")
        self.query_prefix = os.environ.get("INDEXER_ONNX_QUERY_PREFIX", "")
        out_dim = self.session.get_outputs()[0].shape[-1]
        if isinstance(out_dim, int):
            self.dim = out_dim
        # Include tokenizer and external weight files, not just the directory name.
        digest = hashlib.sha256()
        for path in _model_files(self.model_dir):
            relative = os.path.relpath(path, self.model_dir).replace(os.sep, '/')
            digest.update(relative.encode('utf-8') + b'\0')
            content = hashlib.sha256()
            with open(path, 'rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    content.update(block)
            digest.update(content.digest())
        digest.update(json.dumps({'pooling': self.pooling, 'dim': self.dim,
                                  'query_prefix': self.query_prefix}, sort_keys=True).encode())
        self.fingerprint = digest.hexdigest()
        self.name = f"onnx:{os.path.basename(os.path.abspath(model_dir))}:{self.fingerprint}:{self.pooling}"
        self.assert_unchanged()

    def assert_unchanged(self) -> None:
        try:
            unchanged = _model_state(self.model_dir) == self._state
        except OSError:
            unchanged = False
        if not unchanged:
            raise ValueError('ONNX model files changed while loaded; restart the server to load the new model and rebuild its index.')

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([self.query_prefix + text])[0]

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        self.assert_unchanged()
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        out = []
        for i in range(0, len(texts), 16):
            enc = self.tok.encode_batch(list(texts[i:i + 16]))
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, {k: v for k, v in feeds.items() if k in self.input_names})[0]
            if self.pooling == "cls":
                pooled = hidden[:, 0, :]
            else:
                m = mask[..., None].astype(np.float32)
                pooled = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
            pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
            out.append(pooled.astype(np.float32))
        return np.vstack(out)


def get_embedder():
    model_dir = os.environ.get("INDEXER_ONNX_MODEL")
    if model_dir:
        try:
            return OnnxEmbedder(model_dir)
        except Exception as exc:  # fall back, but say so
            import sys
            print(f"[indexer] ONNX embedder unavailable ({exc}); using hashing embedder", file=sys.stderr)
    return HashingEmbedder()
