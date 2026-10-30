"""索引重置标记：请求、启动执行、成功后清理。"""

from src import rag_reset as reset


def _make_index(data_dir):
    (data_dir / "chroma").mkdir(parents=True)
    (data_dir / "rag_state.json").write_text("{}", encoding="utf-8")
    (data_dir / "last_indexed.json").write_text("{}", encoding="utf-8")


def test_request_then_perform_moves_index_and_clears_flag(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _make_index(data)

    assert reset.is_pending(data) is False
    reset.request_reset(data)
    assert reset.is_pending(data) is True

    info = reset.perform_pending_reset(data)
    assert info is not None
    assert len(info["moved"]) == 3
    assert not (data / "chroma").exists()
    assert list(data.glob("chroma.broken-*"))
    assert reset.is_pending(data) is False

    removed = reset.cleanup_broken(data)
    assert removed
    assert not list(data.glob("chroma.broken-*"))


def test_perform_without_request_does_nothing(tmp_path):
    assert reset.perform_pending_reset(tmp_path / "data") is None
