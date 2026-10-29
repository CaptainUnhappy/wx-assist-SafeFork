"""RAG 进度快照的单元测试。"""

import time

from src.assistant import rag_status


def test_progress_lifecycle_and_percentages():
    rag_status.reset(phase="loading", device="cpu", batch_size=32)
    rag_status.job("msg", label="聊天记录", total=100, completed=0, state="running")
    rag_status.job("msg", delta=40)

    snap = rag_status.snapshot()
    assert snap["phase"] == "loading"
    assert snap["batch_size"] == 32
    assert snap["jobs"]["msg"]["label"] == "聊天记录"
    assert snap["jobs"]["msg"]["completed"] == 40
    assert snap["jobs"]["msg"]["percent"] == 40.0
    assert snap["jobs"]["msg"]["state"] == "running"

    rag_status.job("msg", completed=100, state="done")
    snap = rag_status.snapshot()
    assert snap["jobs"]["msg"]["percent"] == 100.0
    assert snap["jobs"]["msg"]["state"] == "done"

    rag_status.update(phase="error", error="boom")
    snap = rag_status.snapshot()
    assert snap["phase"] == "error"
    assert snap["error"] == "boom"


def test_snapshot_is_isolated_from_internal_state():
    rag_status.reset(phase="indexing")
    rag_status.job("oa", label="公众号文章", total=10, completed=3)

    snap = rag_status.snapshot()
    snap["jobs"]["oa"]["completed"] = 999
    snap["phase"] = "tampered"

    fresh = rag_status.snapshot()
    assert fresh["jobs"]["oa"]["completed"] == 3
    assert fresh["phase"] == "indexing"


def test_percent_is_clamped_and_timer_freezes_when_done():
    rag_status.reset(phase="indexing")
    rag_status.job("msg", total=10, completed=25)
    assert rag_status.snapshot()["jobs"]["msg"]["percent"] == 100.0

    rag_status.update(phase="ready")
    first = rag_status.snapshot()["elapsed"]
    time.sleep(1.1)
    assert rag_status.snapshot()["elapsed"] == first


def test_disabled_reset_clears_jobs_and_timer():
    rag_status.reset(phase="indexing")
    rag_status.job("sns", label="朋友圈", total=5, completed=5, state="done")
    rag_status.reset(phase="disabled")

    snap = rag_status.snapshot()
    assert snap["phase"] == "disabled"
    assert snap["elapsed"] == 0
    assert snap["jobs"]["sns"]["completed"] == 0
    assert snap["jobs"]["sns"]["state"] == "idle"
