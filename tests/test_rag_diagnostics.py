"""离线自检：临时索引写入、重开与检索。"""

import pytest

pytest.importorskip("numpy")
pytest.importorskip("chromadb")

from src.assistant.rag.diagnostics import run_check


def test_run_check_builds_and_reopens_temporary_index():
    result = run_check(vectors=40)
    assert result["ok"] is True
    assert result["stored"] == 40
    assert result["reopened"] == 40
    assert result["hits"] > 0
    assert result["elapsed"] >= 0
