# -*- coding: utf-8 -*-
"""联系人静默同步器测试：去重 / 只插新人 / 兜底名 / 不更新 / 异常隔离。"""

import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.schema import initialize_db
from src.db.store import MessageStore
from src.db.contact_sync import ContactSyncer, _classify


class FakeClient:
    """模拟微信客户端：get_contacts_bulk 返回预置资料。"""

    def __init__(self, data=None, raise_exc=False):
        self._data = data or {}
        self.raise_exc = raise_exc

    def get_contacts_bulk(self, usernames):
        if self.raise_exc:
            raise RuntimeError("boom")
        return {u: self._data[u] for u in usernames if u in self._data}


@pytest.fixture
def store_ctx(tmp_path):
    """每个用例独立临时库，结束时确保关闭连接（避免 Windows 文件锁）。"""
    conn = initialize_db(str(tmp_path / "messages.db"))
    store = MessageStore(conn)
    yield store, conn
    conn.close()


def _seed_message(conn, sender_id, sender_name):
    conn.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, chat_id, sender_id, sender_name, content, msg_type, timestamp)
           VALUES (?, ?, ?, ?, 'x', 1, ?)""",
        (f"mid_{sender_id}_{sender_name}", "g@chatroom", sender_id, sender_name,
         int(time.time())),
    )
    conn.execute(
        """INSERT INTO user_last_message(chat_id, sender_id, sender_name, last_timestamp)
           VALUES ('g@chatroom', ?, ?, ?)
           ON CONFLICT(chat_id, sender_id) DO UPDATE SET sender_name=excluded.sender_name""",
        (sender_id, sender_name, int(time.time())),
    )
    conn.commit()


def _rows(conn):
    return {r["username"]: dict(r) for r in
            conn.execute("SELECT * FROM contacts_cache").fetchall()}


def _drain_once(syncer):
    """同步执行一次历史回填 + 排空。"""
    syncer._run_backfill()
    syncer._backfill_done = True
    syncer._drain_pending()


def test_classify():
    assert _classify("wxid_a") == "user"
    assert _classify("abc123") == "user"
    assert _classify("123@chatroom") == "chatroom"
    assert _classify("gh_x") == "official"
    assert _classify("a@openim") == "system"


def test_inserts_new_contacts_from_client(store_ctx):
    store, conn = store_ctx
    client = FakeClient({
        "wxid_a": {"alias": "alice", "nick_name": "爱丽丝",
                   "remark": "备注A", "display_name": "备注A",
                   "is_chatroom": False},
    })
    _seed_message(conn, "wxid_a", "备注A")
    syncer = ContactSyncer(store, client_provider=lambda: client, interval=1000)
    _drain_once(syncer)
    rows = _rows(conn)
    assert "wxid_a" in rows
    assert rows["wxid_a"]["alias"] == "alice"
    assert rows["wxid_a"]["remark"] == "备注A"
    assert rows["wxid_a"]["display_name"] == "备注A"


def test_fallback_to_message_name_when_not_in_contacts(store_ctx):
    store, conn = store_ctx
    _seed_message(conn, "wxid_b", "路人昵称")
    syncer = ContactSyncer(store, client_provider=lambda: FakeClient({}), interval=1000)
    _drain_once(syncer)
    rows = _rows(conn)
    assert rows["wxid_b"]["display_name"] == "路人昵称"
    assert rows["wxid_b"]["alias"] == ""


def test_chatroom_kind(store_ctx):
    store, conn = store_ctx
    _seed_message(conn, "g@chatroom", "某群")
    syncer = ContactSyncer(store, client_provider=lambda: FakeClient({}), interval=1000)
    _drain_once(syncer)
    assert _rows(conn)["g@chatroom"]["kind"] == "chatroom"


def test_note_only_adds_unknown_and_dedup(store_ctx):
    store, _ = store_ctx
    syncer = ContactSyncer(store, client_provider=lambda: None, interval=1000)
    syncer.note("")
    syncer.note(None)
    for _ in range(5):
        syncer.note("wxid_x")
    assert syncer._pending == {"wxid_x"}


def test_never_updates_existing_contact(store_ctx):
    store, conn = store_ctx
    client_v1 = FakeClient({"wxid_u": {"alias": "old", "nick_name": "旧昵称",
                                       "remark": "", "display_name": "旧昵称",
                                       "is_chatroom": False}})
    _seed_message(conn, "wxid_u", "旧昵称")
    syncer = ContactSyncer(store, client_provider=lambda: client_v1, interval=1000)
    _drain_once(syncer)
    first_updated = _rows(conn)["wxid_u"]["updated_at"]

    time.sleep(1.1)
    client_v2 = FakeClient({"wxid_u": {"alias": "new", "nick_name": "新昵称",
                                       "remark": "新备注", "display_name": "新备注",
                                       "is_chatroom": False}})
    syncer.attach_client_provider(lambda: client_v2)
    syncer.note("wxid_u")  # 已在 _known，连 pending 都不会进
    syncer._drain_pending()
    row = _rows(conn)["wxid_u"]
    assert row["alias"] == "old"
    assert row["nick_name"] == "旧昵称"
    assert row["updated_at"] == first_updated


def test_client_exception_is_isolated(store_ctx):
    store, conn = store_ctx
    _seed_message(conn, "wxid_e", "兜底名")
    syncer = ContactSyncer(store, client_provider=lambda: FakeClient(raise_exc=True),
                           interval=1000)
    _drain_once(syncer)  # 不抛异常即可
    assert _rows(conn)["wxid_e"]["display_name"] == "兜底名"


def test_missing_client_uses_fallback(store_ctx):
    store, conn = store_ctx
    _seed_message(conn, "wxid_n", "无客户端")
    syncer = ContactSyncer(store, client_provider=lambda: None, interval=1000)
    _drain_once(syncer)
    assert _rows(conn)["wxid_n"]["display_name"] == "无客户端"


def test_wait_for_client_ready_and_timeout():
    from src.db.contact_sync import _WAIT_CLIENT_PERIOD
    import src.db.contact_sync as cs
    store = type("S", (), {"load_cached_contact_ids": lambda self: set()})()
    # provider 始终返回可用 client → 立即就绪
    ready = type("C", (), {"get_contacts_bulk": lambda self, u: {}})()
    syn = ContactSyncer(store, client_provider=lambda: ready, interval=1000)
    syn._running = True
    assert syn._wait_for_client() is True
    syn.stop()
    # provider 返回 None → 超时返回 False（把超时/周期调小加速）
    orig_to, orig_pe = cs._WAIT_CLIENT_TIMEOUT, cs._WAIT_CLIENT_PERIOD
    cs._WAIT_CLIENT_TIMEOUT, cs._WAIT_CLIENT_PERIOD = 0.05, 0.01
    try:
        syn2 = ContactSyncer(store, client_provider=lambda: None, interval=1000)
        syn2._running = True
        assert syn2._wait_for_client() is False
    finally:
        cs._WAIT_CLIENT_TIMEOUT, cs._WAIT_CLIENT_PERIOD = orig_to, orig_pe


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
