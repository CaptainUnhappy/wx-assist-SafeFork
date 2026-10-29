"""离线重建脚本：切换、回滚与恢复。"""

import json
import sqlite3
import time

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("chromadb")

from scripts.rebuild_rag import _recover, rebuild


class FakeEmbedder:
    def warmup(self):
        pass

    def encode(self, texts):
        return np.ones((len(texts), 512), dtype=np.float32)


def _make_messages(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE messages(chat_id, sender_name, content, created_at)")
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", [
        ("chat", "sender", "重建测试消息", int(time.time())),
        ("chat", "sender", "第二条消息", int(time.time())),
        ("chat", "sender", "第三条消息", int(time.time())),
    ])
    conn.commit()
    conn.close()


def test_rebuild_replaces_index_and_keeps_previous(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _make_messages(data / "messages.db")
    (data / "chroma").mkdir()
    (data / "chroma" / "old.txt").write_text("old-index", encoding="utf-8")
    (data / "rag_state.json").write_text('{"old": true}', encoding="utf-8")

    result = rebuild(data, days=30, skip_cache=True, embedder_factory=FakeEmbedder)

    assert result["ok"] is True
    assert result["chunks"] >= 1
    assert (data / "chroma").exists()
    previous = list(data.glob("chroma.previous-*"))
    assert previous and (previous[0] / "old.txt").read_text(encoding="utf-8") == "old-index"
    assert json.loads((data / "rag_rebuild.json").read_text(encoding="utf-8"))["state"] == "done"


def test_recover_restores_previous_when_target_missing(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    previous = data / "chroma.previous-20260101-000000"
    previous.mkdir()
    (previous / "old.txt").write_text("old", encoding="utf-8")
    (data / "rag_rebuild.json").write_text(json.dumps({
        "state": "switching",
        "stamp": "20260101-000000",
        "backup": {"chroma": str(previous)},
    }), encoding="utf-8")

    _recover(data)

    assert (data / "chroma" / "old.txt").read_text(encoding="utf-8") == "old"
    assert not previous.exists()
    assert json.loads((data / "rag_rebuild.json").read_text(encoding="utf-8"))["state"] == "recovered"
