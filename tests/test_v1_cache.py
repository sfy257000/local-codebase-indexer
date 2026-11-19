import pytest

from local_codebase_indexer.query import CodebaseTools
from local_codebase_indexer.vector_cache import VectorCache


def test_cache_invalidates_after_other_process_update_and_respects_filters(tmp_path):
    path = tmp_path / "one.py"
    path.write_text('def alpha():\n    return "needle"\n')
    t = CodebaseTools(str(tmp_path), auto_refresh=False)
    other = None
    try:
        t.refresh()
        original = t.semantic_search("needle")
        assert t.semantic_search("needle")["results"] == original["results"]
        assert t._vectors.hits > 0
        assert not t.semantic_search("needle", file_pattern="absent*")["results"]
        path.write_text('def changed():\n    return "newneedle"\n')
        other = CodebaseTools(str(tmp_path), auto_refresh=False)
        other.refresh()
        result = t.semantic_search("newneedle")
        assert any(r["symbol"] == "changed" for r in result["results"])
        assert all(r["symbol"] != "alpha" for r in result["results"])
    finally:
        t.indexer.close()
        if other:
            other.indexer.close()


def test_disabled_cache_has_identical_rankings(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text(
        'def one():\n    return "value"\ndef two():\n    return "other"\n'
    )
    t = CodebaseTools(str(tmp_path), auto_refresh=False)
    try:
        t.refresh()
        on = t.semantic_search("value")["results"]
        monkeypatch.setenv("INDEXER_VECTOR_CACHE_MB", "0")
        t._vectors = VectorCache()
        assert t.semantic_search("value")["results"] == on
        assert t._vectors.value is None
    finally:
        t.indexer.close()


@pytest.mark.parametrize("budget", ["-1", "nan", "inf", "invalid"])
def test_invalid_cache_budget_is_rejected(monkeypatch, budget):
    monkeypatch.setenv("INDEXER_VECTOR_CACHE_MB", budget)
    with pytest.raises(ValueError):
        VectorCache()


def test_lexical_search_avoids_vectors_and_cursor_cannot_switch_modes(
    tmp_path, monkeypatch
):
    for i in range(8):
        (tmp_path / f"f{i}.py").write_text(f'def needle_{i}():\n    return "needle"\n')
    t = CodebaseTools(str(tmp_path), auto_refresh=False)
    try:
        t.refresh()
        monkeypatch.setattr(
            t._vectors,
            "get",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("vector read")),
        )
        first = t.semantic_search("needle", limit=1, mode="lexical")
        assert first["mode"] == "lexical" and first["results"]
        assert not any("vector" in r["matched_by"] for r in first["results"])
        with pytest.raises(ValueError, match="cursor"):
            t.semantic_search(
                "needle", cursor=first["pagination"]["next_cursor"], mode="hybrid"
            )
        assert t.semantic_search(
            "needle", limit=1, cursor=first["pagination"]["next_cursor"], mode="lexical"
        )["results"]
    finally:
        t.indexer.close()
