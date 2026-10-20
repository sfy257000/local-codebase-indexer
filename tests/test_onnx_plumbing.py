"""Verifies the ONNX embedder plumbing with a tiny synthetic model (not a quality test).

A real model (all-MiniLM-L6-v2 / bge-small-en-v1.5) cannot be downloaded in every environment, so this builds a
384-dim embedding-lookup ONNX graph plus a word-level tokenizer. It proves that input names, padding, mean pooling,
normalisation, index rebuild on embedder change and search all work; it says nothing about retrieval quality.
"""
import os
import sys

import numpy as np
import pytest

onnx = pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")
tokenizers = pytest.importorskip("tokenizers")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from indexer.embedder import DIM, OnnxEmbedder, get_embedder  # noqa: E402
from indexer.query import CodebaseTools  # noqa: E402


@pytest.fixture()
def model_dir(tmp_path):
    from onnx import TensorProto, helper, numpy_helper
    from tokenizers import Tokenizer, models, pre_tokenizers, trainers

    tok = Tokenizer(models.WordLevel(unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.train_from_iterator(
        ["lot status update scrap record login password printer barcode label"],
        trainers.WordLevelTrainer(special_tokens=["[PAD]", "[UNK]"], vocab_size=64),
    )
    tok.save(str(tmp_path / "tokenizer.json"))

    rng = np.random.default_rng(0)
    table = rng.normal(size=(tok.get_vocab_size(), DIM)).astype(np.float32)
    graph = helper.make_graph(
        [helper.make_node("Gather", ["table", "input_ids"], ["last_hidden_state"])],
        "tiny",
        [helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["b", "s"]),
         helper.make_tensor_value_info("attention_mask", TensorProto.INT64, ["b", "s"])],
        [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["b", "s", DIM])],
        initializer=[numpy_helper.from_array(table, "table")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(tmp_path / "model.onnx"))
    return str(tmp_path)


def test_embedder_shapes_padding_and_normalisation(model_dir):
    emb = OnnxEmbedder(model_dir)
    v = emb.embed(["lot status", "scrap record login password printer"])
    assert v.shape == (2, DIM) and v.dtype == np.float32
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-4)
    # padding must not change a sentence's vector
    alone = emb.embed(["lot status"])[0]
    assert np.allclose(alone, v[0], atol=1e-5)
    assert emb.embed([]).shape == (0, DIM)


def test_env_switch_rebuilds_index_and_search_runs(tmp_path, model_dir, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("def update_lot_status():\n    return 1\n", encoding="utf-8")
    (proj / "b.py").write_text("def print_label():\n    return 2\n", encoding="utf-8")

    t = CodebaseTools(str(proj))
    t.refresh()
    assert t.embedder.name == "hashing-384-v1"
    t.indexer.close()

    monkeypatch.setenv("INDEXER_ONNX_MODEL", model_dir)
    t = CodebaseTools(str(proj))
    assert t.embedder.name.startswith("onnx:")
    st = t.refresh()
    assert st.added == st.scanned  # embedding space changed: everything re-embedded
    res = t.semantic_search("update_lot_status", limit=1)
    assert res["results"][0]["file_path"] == "a.py"
    assert t.index_status()["embedder"].startswith("onnx:")
    t.indexer.close()


def test_missing_model_falls_back_to_hashing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INDEXER_ONNX_MODEL", str(tmp_path / "does-not-exist"))
    assert get_embedder().name == "hashing-384-v1"
    assert "ONNX embedder unavailable" in capsys.readouterr().err
