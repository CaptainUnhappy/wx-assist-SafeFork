"""数据库读取层的回归测试。

历史说明
--------
本文件原先测试 ``wcdb_api.dll`` 的 JSON 读取错误分类（``_read_gbk_string_ex``
截断检测、``_call_json_inner`` 异常分级）。该 DLL 已不再使用——读取层改为
纯 Python 实现（见 ``src/wechat/db_crypto.py`` 与 ``src/wechat/db_reader.py``），
因此相关用例一并移除，替换为解密层的单元测试。

覆盖：
- ``db_crypto`` 的密钥解析与页加解密往返。
- 群组解析失败时 ``WcdbBackend.start`` 不再让整个服务崩溃。
"""
from unittest.mock import Mock

import pytest

from src.wechat import db_crypto
from src.wechat.wcdb_backend import WcdbBackend


# ── 解密层：密钥解析 ──────────────────────────────────────────────

def test_parse_key_accepts_64_hex_chars():
    key = "ab" * 32
    assert db_crypto.parse_key(key) == bytes.fromhex(key)


def test_parse_key_rejects_wrong_length():
    with pytest.raises(db_crypto.DecryptError):
        db_crypto.parse_key("abcd")


def test_parse_key_rejects_non_hex():
    with pytest.raises(db_crypto.DecryptError):
        db_crypto.parse_key("zz" * 32)


# ── 解密层：页加解密往返 ──────────────────────────────────────────

def test_encrypt_decrypt_page_roundtrip():
    """加密后再解密应还原出相同的明文页。"""
    enc_key = b"\x11" * 32
    salt = b"\x5a" * 16
    plain = bytearray(b"\x00" * db_crypto.PAGE_SIZE)
    plain[:16] = db_crypto.SQLITE_HEADER
    plain[16:64] = bytes(range(48))

    encrypted = db_crypto.encrypt_page(bytes(plain), enc_key, page_no=1, salt=salt)
    assert len(encrypted) == db_crypto.PAGE_SIZE
    assert encrypted[:16] == salt              # 加密页头部是 salt，不是文件头
    assert encrypted != bytes(plain)

    decrypted = db_crypto.decrypt_page(encrypted, enc_key, page_no=1)
    assert decrypted[:16] == db_crypto.SQLITE_HEADER
    assert decrypted[16:64] == bytes(range(48))


def test_decrypt_page_shorter_input_is_padded():
    """不足一页的输入应被补齐后解密，不抛异常。"""
    enc_key = b"\x22" * 32
    result = db_crypto.decrypt_page(b"\x00" * 100, enc_key, page_no=2)
    assert len(result) == db_crypto.PAGE_SIZE


def test_verify_key_returns_false_for_wrong_key(tmp_path):
    """密钥错误时首页 HMAC 校验必须失败。"""
    real_key = bytes.fromhex("cd" * 32)
    wrong_key = bytes.fromhex("ef" * 32)
    salt = b"\x01" * 16

    enc_key, mac_key = db_crypto.derive_keys(real_key, salt)
    plain = bytearray(b"\x00" * db_crypto.PAGE_SIZE)
    plain[:16] = db_crypto.SQLITE_HEADER
    page = db_crypto.encrypt_page(
        bytes(plain), enc_key, page_no=1, salt=salt, mac_key=mac_key
    )

    db_file = tmp_path / "sample.db"
    db_file.write_bytes(page)

    assert db_crypto.verify_key(real_key, db_file) is True
    assert db_crypto.verify_key(wrong_key, db_file) is False


def test_is_plaintext_db_detects_sqlite_header(tmp_path):
    plain = tmp_path / "plain.db"
    plain.write_bytes(db_crypto.SQLITE_HEADER + b"\x00" * 64)
    assert db_crypto.is_plaintext_db(plain) is True

    other = tmp_path / "enc.db"
    other.write_bytes(b"\x9f\x4a" * 32)
    assert db_crypto.is_plaintext_db(other) is False


# ── 启动层：群组解析失败不再让服务崩溃 ────────────────────────────

def test_start_survives_group_resolution_failure(monkeypatch):
    pushed: list[str] = []
    monkeypatch.setattr(
        WcdbBackend,
        "_push_start_error",
        staticmethod(lambda message: pushed.append(message)),
    )
    client = Mock()
    client._config = {}
    client.get_sessions.side_effect = ValueError(
        "WCDB query result too large (truncated at 500000 bytes)"
    )
    monkeypatch.setattr(
        "src.wechat.wcdb_backend.WcdbNativeClient", lambda *a, **k: client
    )

    backend = WcdbBackend(groups=["测试群"])
    backend.start(Mock())  # 不应抛出

    assert backend._talker_ids == {}
    assert pushed, "启动失败原因应推送到运行状态页"
