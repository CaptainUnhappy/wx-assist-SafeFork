"""微信 4.x 数据库读取层 —— 纯 Python 实现，替代 `wcdb_api.dll`。

数据结构（全部实测确认，2026-10-01）
------------------------------------

**会话** ``session/session.db``
  ``SessionTable``: username, type, unread_count, summary, draft, status,
  last_timestamp, sort_timestamp, last_msg_locald_id, last_msg_type,
  last_msg_sender, last_sender_display_name, is_hidden ...

**消息** ``message/message_N.db``
  - 表名 = ``Msg_`` + ``md5(username)``，**每个会话一张表**
  - 分片是**滚动归档**（不是哈希取模）：同一会话的表可能同时存在于多个
    分片中，读取时必须遍历所有分片再按时间合并
  - 关键字段：local_id, server_id, local_type, sort_seq(毫秒), real_sender_id,
    create_time(秒), status, message_content(bytes, zstd), packed_info_data
  - ``real_sender_id`` 映射到**本分片内** ``Name2Id.rowid`` → ``user_name``
    （实测群聊命中率 93.6%，未命中为已退群成员；不要用 session/contact 库的
    Name2Id，那两个是不同 ID 空间）

**联系人** ``contact/contact.db``
  - ``contact.id`` == ``name2id.rowid`` == ``username``
  - ``chat_room.id`` + ``chatroom_member.room_id``/``member_id``（member_id 即 contact.id）
  - ``chat_room_info_detail`` 群公告

**朋友圈** ``sns/sns.db``
  - ``SnsTimeLine``: tid, user_name, content(XML), pack_info_buf(媒体/互动打包)
  - ``SnsTopItem_1``: 折叠/已读状态

**收藏** ``favorite/favorite.db``
  - ``fav_db_item``: local_id, type, update_time, content, fromusr ...

**图片路径** ``hardlink/hardlink.db``
  - ``image_hardlink_info_v4``: md5 → dir1/dir2（目录 id）
  - ``dir2id``: 目录 id → 目录名

返回格式约定
------------
本模块的所有方法**输出结构与原 DLL 完全一致**（含 snake_case 与 camelCase
冗余键），因此上层 ``api_handlers``/``wcdb_backend``/``sns_client`` 等代码
无需任何改动。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from . import db_crypto

logger = logging.getLogger(__name__)

# 显示名映射（联系人全表）的缓存时长。联系人变更不频繁，而该映射被
# get_sessions / get_display_names / 朋友圈等多次使用，缓存可避免每次都
# 全表扫描 3 万余行。
_DISPLAY_MAP_TTL_SEC = 5.0

# 说明：本模块**不对结果做任何隐式截断**。取多少条完全由调用方（以及微信
# 数据本身）决定，与旧 DLL 实现的行为保持一致：
#   - get_sessions / get_contacts 旧实现忽略 limit，总是返回全部
#   - get_messages 旧实现把 limit 原样下推
# 目的是避免在读取层引入任何会改变业务结果的限制。


# ── 朋友圈 tid 的时间换算 ────────────────────────────────────────────
#
# SnsTimeLine.tid 是 WeChat 的雪花 id：tid = (unix 毫秒) << 23 | 序列号。
# 实测（2026-10-01，8 条真实样本）：
#     raw tid = -3428570417742081415   createTime = 1790305811
#     (raw & 2^64-1) / (createTime * 1000) == 8388608 == 2**23
# 因为 毫秒 << 23 ≈ 1.5e19 > 2**63，该列在 SQLite 里存成**负数**（有符号
# 64 位）。所以时间过滤必须把边界也换算成同样的有符号表示，直接用
# ``秒 * 1_000_000`` 之类的正数比较会**永远匹配不到**（表现为静默返回空）。
_TID_SHIFT = 23
_UINT64_MASK = 0xFFFFFFFFFFFFFFFF


def _tid_unsigned(seconds: int) -> int:
    """秒级时间戳 → tid 的无符号下界。"""
    return ((int(seconds) * 1000) << _TID_SHIFT) & _UINT64_MASK


def _to_signed64(value: int) -> int:
    """无符号 64 位 → SQLite 存储用的有符号表示。"""
    value &= _UINT64_MASK
    return value - (1 << 64) if value >= (1 << 63) else value


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _decompress_bytes(raw) -> str:
    """把 zstd 二进制（或已是文本）转成可读文本。

    ``message_content`` 在库里是 BLOB，魔数 0x28B52FFD；但也可能是纯文本
    XML。为兼容上层 ``content_codec.decompress_content`` 的既有行为，
    这里输出**十六进制字符串**（Zstd 时）或原文本。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (bytes, bytearray)):
        return str(raw)
    data = bytes(raw)
    if not data:
        return ""
    if data[:4] == b"\x28\xb5\x2f\xfd":
        # 保持与 DLL 一致的输出形态：hex 字符串，交给下游解压
        return data.hex()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.hex()


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class _LockedResult(list):
    """已取完的查询结果，兼容 ``fetchone`` / ``fetchall`` / 迭代 / ``description``。

    之所以一次性取完（而不是返回惰性 cursor），是因为锁必须在数据读完
    之前一直持有 —— 惰性 cursor 会在锁释放后继续访问连接。
    """

    #: 与原 cursor 对齐：非 SELECT 语句为 None，SELECT 为列描述列表。
    description = None

    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class _LockedConn:
    """底层连接的代理：每次执行都在读取器的锁内，并确保连接仍然有效。

    背景：源库（或 WAL）变化时需要重新解密并覆盖明文库，而 Windows 下
    被打开的文件无法覆盖，因此必须先关闭旧连接。若此时另一个线程正拿着
    那个连接查询，就会报 ``Cannot operate on a closed database``。
    代理把这层保护收敛到一处，调用方写法保持不变。
    """

    __slots__ = ("_owner", "_src")

    def __init__(self, owner: "WeChatDbReader", src: Path):
        self._owner = owner
        self._src = src

    def execute(self, sql: str, params=()):
        return self._owner._execute_locked(self._src, sql, params)


class WeChatDbReader:
    """微信本地数据库读取器（纯 Python）。"""

    def __init__(self, account_dir: str | Path, key_hex: str,
                 cache_dir: Optional[str | Path] = None):
        self._account_dir = Path(account_dir)
        self._db_storage = self._account_dir / "db_storage"
        self._key_hex = key_hex
        self._cache_dir = Path(cache_dir) if cache_dir else (
            self._account_dir / ".wxacc_read_cache"
        )
        self._cache = db_crypto.DecryptCache(self._cache_dir, key_hex)
        self._lock = threading.RLock()
        # 分片表名索引缓存：{db_path: {表名, ...}}
        self._table_index: dict[str, set] = {}
        self._conns: dict[str, sqlite3.Connection] = {}
        self._my_wxid = ""
        # 显示名映射缓存：(取数时刻, {username: 显示名})
        self._display_map: Optional[tuple[float, dict]] = None

    # ── 路径与连接 ──────────────────────────────────────────────────

    @property
    def account_dir(self) -> str:
        return str(self._account_dir)

    @property
    def favorite_db_path(self) -> str:
        return str(self._db_storage / "favorite" / "favorite.db")

    @property
    def sns_db_path(self) -> str:
        return str(self._db_storage / "sns" / "sns.db")

    @property
    def key_hex(self) -> str:
        return self._key_hex

    def set_my_wxid(self, wxid: str) -> None:
        """记录自己的 wxid（用于判断消息是否是自己发的）。"""
        self._my_wxid = (wxid or "").strip()

    def _plain_path(self, src: Path) -> Path:
        """取明文库路径；若源库已变化，先关掉旧连接再重新解密。

        Windows 下被打开的文件无法被覆盖，所以重新解密前必须释放连接，
        否则 `os.replace` 会抛 PermissionError。
        """
        with self._lock:
            if self._cache.needs_refresh(src):
                stale_key = str(self._cache.cache_dir / src.name)
                stale = self._conns.pop(stale_key, None)
                if stale is not None:
                    try:
                        stale.close()
                    except Exception:
                        pass
                self._table_index.pop(str(src), None)
            return self._cache.ensure(src)

    def _connect_raw(self, src: Path) -> sqlite3.Connection:
        """获取（必要时创建）底层 sqlite3 连接。必须在锁内调用。"""
        plain = self._plain_path(src)
        cache_key = str(plain)
        conn = self._conns.get(cache_key)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(plain), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._conns[cache_key] = conn
        return conn

    def _connect(self, src: Path) -> _LockedConn:
        """返回带锁的连接代理（调用方写法与原 ``sqlite3.Connection`` 一致）。"""
        return _LockedConn(self, src)

    def _execute_locked(self, src: Path, sql: str, params=()) -> _LockedResult:
        """在锁内取连接、执行、取完结果。

        锁覆盖到结果取出为止，且执行前会重新确认连接有效（源库变化时
        可能已被重建），因此并发查询与后台重解密不会互相踩踏。
        """
        with self._lock:
            conn = self._connect_raw(src)
            cursor = conn.execute(sql, params)
            result = _LockedResult(cursor.fetchall())
            # 保留列描述，供 exec_query 区分 SELECT 与非 SELECT
            result.description = cursor.description
            return result

    def refresh(self, src: Optional[Path] = None) -> None:
        """让某库（或全部）的缓存失效，下次查询会重新解密。"""
        with self._lock:
            for key in list(self._conns):
                try:
                    self._conns[key].close()
                except Exception:
                    pass
                self._conns.pop(key, None)
            self._table_index.clear()
            self._display_map = None
            self._cache.invalidate(src)

    def close(self) -> None:
        """关闭所有连接（解密缓存文件保留，便于下次快速打开）。"""
        with self._lock:
            for conn in self._conns.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self._conns.clear()
            self._table_index.clear()
            self._display_map = None

    # ── 库定位 ──────────────────────────────────────────────────────

    def _session_db(self) -> Optional[Path]:
        p = self._db_storage / "session" / "session.db"
        return p if p.exists() else None

    def _contact_db(self) -> Optional[Path]:
        p = self._db_storage / "contact" / "contact.db"
        return p if p.exists() else None

    def _sns_db(self) -> Optional[Path]:
        p = self._db_storage / "sns" / "sns.db"
        return p if p.exists() else None

    def _favorite_db(self) -> Optional[Path]:
        p = self._db_storage / "favorite" / "favorite.db"
        return p if p.exists() else None

    def _hardlink_db(self) -> Optional[Path]:
        p = self._db_storage / "hardlink" / "hardlink.db"
        return p if p.exists() else None

    def _message_dbs(self) -> list[Path]:
        """所有消息分片库。

        微信把消息分在两类库里，两者结构完全一致（``Msg_<md5(username)>``
        加各自的 ``Name2Id``），必须都纳入：

          - ``message_N.db``      私聊 / 群聊
          - ``biz_message_N.db``  公众号（gh_*）

        漏掉后者会导致**所有公众号会话读不到任何消息**（OA 监控/摘要在内）。
        排除 FTS 与 resource 这类辅助库。
        """
        msg_dir = self._db_storage / "message"
        if not msg_dir.exists():
            return []
        result: list[Path] = []
        for pattern in ("message_*.db", "biz_message_*.db"):
            for p in sorted(msg_dir.glob(pattern)):
                low = p.name.lower()
                # message_fts.db 是 FTS 全文索引（表名 message_fts_v4_*），
                # 不是消息分片。注意文件名用下划线（message_fts），不是连字符，
                # 别写成 "-fts" —— 那样过滤不掉，会白解密 189 MB。
                if "fts" in low or "resource" in low:
                    continue
                result.append(p)
        return sorted(set(result))

    def _voice_dbs(self) -> list[Path]:
        """语音库：``media_N.db``，内含 ``VoiceInfo`` 表。"""
        msg_dir = self._db_storage / "message"
        if not msg_dir.exists():
            return []
        return sorted(msg_dir.glob("media_*.db"))

    def _tables_of(self, src: Path) -> set:
        """某个库里的表名集合（缓存）。"""
        key = str(src)
        cached = self._table_index.get(key)
        if cached is not None:
            return cached
        conn = self._connect(src)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self._table_index[key] = names
        return names

    # ── 会话 ────────────────────────────────────────────────────────

    def get_sessions(self, limit: int = 500) -> list[dict]:
        """会话列表（按最近活跃排序）。

        注意 ``limit`` 的语义：**旧 DLL 实现忽略该参数、总是返回全部会话**，
        上层的群解析、防撤回会话枚举都依赖"拿到全部会话"。因此这里同样
        不按它截断，返回全部会话。
        """
        src = self._session_db()
        if src is None:
            logger.warning("session.db 不存在: %s", self._db_storage)
            return []
        conn = self._connect(src)
        try:
            rows = conn.execute(
                "SELECT username, type, unread_count, summary, draft, status, "
                "       last_timestamp, sort_timestamp, last_msg_locald_id, "
                "       last_msg_type, last_msg_sub_type, last_msg_sender, "
                "       last_sender_display_name, is_hidden "
                "FROM SessionTable "
                "WHERE username IS NOT NULL AND username <> '' "
                "ORDER BY COALESCE(sort_timestamp, last_timestamp, 0) DESC"
            ).fetchall()
        except sqlite3.Error as exc:
            logger.error("读取 SessionTable 失败: %s", exc)
            return []

        name_map = self._contact_display_map()
        sessions = []
        for row in rows:
            username = str(row["username"] or "")
            if not username:
                continue
            display = name_map.get(username, "")
            last_ts = _to_int(row["last_timestamp"])
            sessions.append({
                # 会话标识
                "username": username,
                # 显示名（多候选，保持与 DLL 输出一致）
                "displayName": display,
                "displayname": display,
                "nickname": display,
                "display_name": display,
                # 未读与时间（DLL 用 nTime，这里同时给出）
                "unread_count": _to_int(row["unread_count"]),
                "nTime": last_ts,
                "last_timestamp": last_ts,
                "sort_timestamp": _to_int(row["sort_timestamp"], last_ts),
                # 摘要与最后一条消息
                "summary": str(row["summary"] or ""),
                "draft": str(row["draft"] or ""),
                "last_msg_local_id": _to_int(row["last_msg_locald_id"]),
                "last_msg_type": _to_int(row["last_msg_type"]),
                "last_msg_sub_type": _to_int(row["last_msg_sub_type"]),
                "last_msg_sender": str(row["last_msg_sender"] or ""),
                "last_sender_display_name": str(row["last_sender_display_name"] or ""),
                "type": _to_int(row["type"]),
                "status": _to_int(row["status"]),
                "is_hidden": _to_int(row["is_hidden"]),
            })
        logger.info("Got %d sessions", len(sessions))
        return sessions

    # ── 消息 ────────────────────────────────────────────────────────

    def _sender_map(self, src: Path) -> dict:
        """某个消息分片的 real_sender_id 映射表（rowid -> user_name）。"""
        try:
            conn = self._connect(src)
            return {r[0]: r[1] for r in conn.execute(
                "SELECT rowid, user_name FROM Name2Id"
            )}
        except sqlite3.Error as exc:
            logger.debug("读取 %s 的 Name2Id 失败: %s", src.name, exc)
            return {}

    def _message_sources(self, talker: str) -> list[tuple[Path, str]]:
        """找出包含该会话消息表的所有分片，返回 [(分片路径, 表名), ...]。"""
        table = f"Msg_{_md5_hex(talker)}"
        result = []
        for src in self._message_dbs():
            try:
                if table in self._tables_of(src):
                    result.append((src, table))
            except Exception as exc:            # noqa: BLE001
                logger.debug("检查 %s 失败: %s", src.name, exc)
        return result

    def get_messages(self, talker: str, limit: int = 200,
                     offset: int = 0) -> list[dict]:
        """读取指定会话的消息（跨分片合并，按时间升序返回）。"""
        if not talker:
            return []
        sources = self._message_sources(talker)
        if not sources:
            logger.debug("没有找到 %s 的消息表（Msg_%s）", talker, _md5_hex(talker))
            return []

        # 完全按调用方给的 limit 执行，不做任何额外截断（与旧实现一致：limit
        # 原样下推给数据层）。limit <= 0 视为"不限"。
        want = int(limit) if limit and int(limit) > 0 else 0
        # 每个分片取"最近的 limit+offset 条"，避免全表扫描（群里可达数万条）；
        # SQLite 中 LIMIT -1 表示不设上限。
        per_shard = (want + max(0, int(offset))) if want else -1

        collected: list[dict] = []
        for src, table in sources:
            sender_map = self._sender_map(src)
            conn = self._connect(src)
            try:
                # 按 local_id 倒序取（local_id 是主键、有索引，且与时间正相关）。
                # 千万不能按 create_time 排序：该列无索引，群里上万条消息会
                # 触发全表扫描 + 排序，单次查询可达数秒。
                rows = conn.execute(
                    f"SELECT local_id, server_id, local_type, sort_seq, "
                    f"       real_sender_id, create_time, status, "
                    f"       message_content, compress_content, packed_info_data, "
                    f"       origin_source, source "
                    f"FROM [{table}] "
                    f"ORDER BY local_id DESC LIMIT ?",
                    (per_shard,),
                ).fetchall()
            except sqlite3.Error as exc:
                logger.warning("读取 %s.%s 失败: %s", src.name, table, exc)
                continue

            for row in rows:
                raw_content = row["message_content"]
                if isinstance(raw_content, (bytes, bytearray)) and not raw_content:
                    raw_content = row["compress_content"]
                content = _decompress_bytes(raw_content)

                sender_id = _to_int(row["real_sender_id"])
                sender = sender_map.get(sender_id, "")
                create_time = _to_int(row["create_time"])
                local_type = _to_int(row["local_type"])
                server_id = row["server_id"]

                packed = row["packed_info_data"]
                packed_hex = bytes(packed).hex() if isinstance(packed, (bytes, bytearray)) else ""

                collected.append({
                    "local_id": _to_int(row["local_id"]),
                    "localId": _to_int(row["local_id"]),
                    "server_id": server_id,
                    "svrId": server_id,
                    "serverId": server_id,
                    "local_type": local_type,
                    "localType": local_type,
                    "msg_type": local_type,
                    "create_time": create_time,
                    "createTime": create_time,
                    "timestamp": create_time,
                    "sort_seq": _to_int(row["sort_seq"]),
                    "real_sender_id": sender_id,
                    "sender_username": sender,
                    "senderUsername": sender,
                    "sender": sender,
                    "message_content": content,
                    "content": content,
                    "packed_info_data": packed_hex,
                    "status": _to_int(row["status"]),
                    "origin_source": _to_int(row["origin_source"]),
                    "is_send": 1 if (self._my_wxid and sender == self._my_wxid) else 0,
                })

        # 合并分片后按时间排序，取最近 limit 条（再按 offset 回退）
        collected.sort(key=lambda m: (m["create_time"], m["local_id"]))
        if offset > 0:
            collected = collected[: max(0, len(collected) - offset)]
        if want and len(collected) > want:
            collected = collected[-want:]
        return collected

    # ── 联系人 / 显示名 / 头像 ──────────────────────────────────────

    def _contact_display_map(self) -> dict:
        """username -> 显示名（remark 优先于 nick_name）。

        结果带 TTL 缓存：该映射要扫描 contact 全表（3 万余行），而
        get_sessions / get_display_names / 朋友圈都会用到它。
        """
        now = time.monotonic()
        cached = self._display_map
        if cached is not None and (now - cached[0]) < _DISPLAY_MAP_TTL_SEC:
            return cached[1]

        src = self._contact_db()
        if src is None:
            return {}
        try:
            conn = self._connect(src)
            result = {}
            for row in conn.execute(
                "SELECT username, remark, nick_name, alias FROM contact "
                "WHERE username IS NOT NULL"
            ):
                username = str(row["username"] or "")
                if not username:
                    continue
                for candidate in (row["remark"], row["nick_name"], row["alias"]):
                    text = str(candidate or "").strip()
                    if text:
                        result[username] = text
                        break
                else:
                    result.setdefault(username, username)
            self._display_map = (now, result)
            return result
        except sqlite3.Error as exc:
            logger.error("读取 contact 表失败: %s", exc)
            return {}

    def get_display_names(self, usernames: Iterable[str]) -> dict:
        """wxid -> 显示名。"""
        targets = [str(u) for u in (usernames or []) if u]
        if not targets:
            return {}
        full = self._contact_display_map()
        result = {u: full[u] for u in targets if u in full}
        # 群里成员可能不在 contact 表，回填为自身，避免上层拿到空值
        for u in targets:
            result.setdefault(u, "")
        return result

    def get_avatar_urls(self, usernames: Iterable[str]) -> dict:
        """wxid -> 头像 URL。"""
        targets = [str(u) for u in (usernames or []) if u]
        if not targets:
            return {}
        src = self._contact_db()
        if src is None:
            return {}
        result = {}
        try:
            conn = self._connect(src)
            placeholders = ",".join("?" * len(targets))
            for row in conn.execute(
                f"SELECT username, big_head_url, small_head_url FROM contact "
                f"WHERE username IN ({placeholders})",
                targets,
            ):
                username = str(row["username"] or "")
                url = str(row["big_head_url"] or row["small_head_url"] or "").strip()
                if username and url:
                    result[username] = url
        except sqlite3.Error as exc:
            logger.error("读取头像失败: %s", exc)
        return result

    def get_contacts(self, keyword: str = "", limit: int = 1000) -> list[dict]:
        """联系人列表（可按关键词过滤）。

        与旧 DLL 实现保持一致：``limit`` 不用于截断（旧实现忽略该参数，
        总是返回全部联系人）。带 ``keyword`` 时由 SQL 先过滤，不会拉全表。
        """
        src = self._contact_db()
        if src is None:
            return []
        try:
            conn = self._connect(src)
            if keyword:
                rows = conn.execute(
                    "SELECT username, nick_name, remark, alias, big_head_url, "
                    "       small_head_url, local_type "
                    "FROM contact "
                    "WHERE (nick_name LIKE ? OR remark LIKE ? OR alias LIKE ? "
                    "       OR username LIKE ?) AND username IS NOT NULL",
                    (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT username, nick_name, remark, alias, big_head_url, "
                    "       small_head_url, local_type "
                    "FROM contact WHERE username IS NOT NULL"
                ).fetchall()
        except sqlite3.Error as exc:
            logger.error("读取联系人失败: %s", exc)
            return []

        result = []
        for row in rows:
            username = str(row["username"] or "")
            if not username:
                continue
            nick = str(row["nick_name"] or "").strip()
            remark = str(row["remark"] or "").strip()
            display = remark or nick or username
            result.append({
                "username": username,
                "userName": username,
                "nickname": display,
                "nickName": nick,
                "displayName": display,
                "remark": remark,
                "alias": str(row["alias"] or ""),
                "big_head_url": str(row["big_head_url"] or ""),
                "small_head_url": str(row["small_head_url"] or ""),
                "local_type": _to_int(row["local_type"]),
            })
        return result

    def get_contact_status(self, usernames: list[str]) -> dict:
        """置顶/折叠/静音状态（读 contact.flag 位）。

        上层目前主要直接走 ``exec_query`` 取 flag，这里保留同样能力。
        """
        targets = [str(u) for u in (usernames or []) if u]
        if not targets:
            return {}
        src = self._contact_db()
        if src is None:
            return {}
        result = {}
        try:
            conn = self._connect(src)
            placeholders = ",".join("?" * len(targets))
            for row in conn.execute(
                f"SELECT username, flag FROM contact WHERE username IN ({placeholders})",
                targets,
            ):
                flag = _to_int(row["flag"])
                result[str(row["username"])] = {
                    "isFolded": bool(flag & (1 << 28)),
                    "isMuted": bool(flag & (1 << 9)),
                    "flag": flag,
                }
        except sqlite3.Error as exc:
            logger.error("读取 contact.flag 失败: %s", exc)
        return result

    def resolve_nickname(self, wxid: str) -> str:
        """单个 wxid 的显示名（缺失时回退为 wxid 本身）。"""
        if not wxid:
            return ""
        names = self.get_display_names([wxid])
        return names.get(wxid) or wxid

    # ── 群成员 ──────────────────────────────────────────────────────

    def get_group_members(self, chatroom_id: str) -> list[dict]:
        """群成员列表。

        ``chat_room.id`` → ``chatroom_member.room_id``，
        ``chatroom_member.member_id`` → ``contact.id``。
        """
        if not chatroom_id:
            return []
        csrc = self._contact_db()
        if csrc is None:
            return []
        try:
            conn = self._connect(csrc)
            room = conn.execute(
                "SELECT id FROM chat_room WHERE username=?", (chatroom_id,)
            ).fetchone()
            if room is None:
                logger.debug("群 %s 不在 chat_room 表中", chatroom_id)
                return []
            room_id = room["id"]
            rows = conn.execute(
                "SELECT c.username, c.nick_name, c.remark, c.big_head_url, "
                "       c.small_head_url "
                "FROM chatroom_member cm "
                "JOIN contact c ON c.id = cm.member_id "
                "WHERE cm.room_id = ?",
                (room_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.error("读取群成员失败: %s", exc)
            return []

        members = []
        for row in rows:
            member_id = str(row["username"] or "")
            if not member_id:
                continue
            nick = str(row["nick_name"] or "").strip()
            remark = str(row["remark"] or "").strip()
            display = remark or nick or member_id
            members.append({
                "wxid": member_id,
                "username": member_id,
                "userName": member_id,
                "nickname": display,
                "nickName": nick,
                "groupNickName": display,
                "remark": remark,
                "avatarUrl": str(row["big_head_url"] or row["small_head_url"] or ""),
            })
        return members

    # ── 语音 ────────────────────────────────────────────────────────

    def get_voice_data(self, session_id: str, create_time: int = 0,
                       local_id: int = 0, svr_id: int = 0,
                       candidates=None) -> dict:
        """取语音数据，返回 ``{"success": bool, "hex": str}``。

        **音频数据不在消息表里**：消息表中 ``local_type=34`` 的
        ``message_content`` 只是元数据 XML（几百字节）。真正的 SILK 音频
        （以 ``\\x02#!SILK_V3`` 开头）存在 ``media_N.db`` 的
        ``VoiceInfo.voice_data`` 列，其中 ``chat_name_id`` 是**该库 Name2Id
        的 rowid**。

        匹配优先级：``svr_id`` → ``(会话, local_id)`` → ``(会话, create_time)``。
        """
        voice_dbs = self._voice_dbs()
        if not voice_dbs:
            return {"success": False, "error": "找不到语音库 media_N.db"}
        if not session_id and not svr_id:
            return {"success": False, "error": "缺少会话 ID 与 svr_id"}

        for src in voice_dbs:
            conn = self._connect(src)
            try:
                # ① svr_id 最可靠（全局唯一）
                if svr_id:
                    rows = conn.execute(
                        "SELECT voice_data FROM VoiceInfo WHERE svr_id=? LIMIT 1",
                        (_to_int(svr_id),),
                    ).fetchall()
                    if rows and rows[0][0]:
                        return {"success": True, "hex": bytes(rows[0][0]).hex()}

                # ② 退而用会话 + local_id / create_time
                if session_id:
                    id_rows = conn.execute(
                        "SELECT rowid FROM Name2Id WHERE user_name=? LIMIT 1",
                        (session_id,),
                    ).fetchall()
                    if not id_rows:
                        continue
                    chat_id = id_rows[0][0]

                    rows = []
                    if local_id:
                        rows = conn.execute(
                            "SELECT voice_data FROM VoiceInfo "
                            "WHERE chat_name_id=? AND local_id=? LIMIT 1",
                            (chat_id, _to_int(local_id)),
                        ).fetchall()
                    if (not rows) and create_time:
                        rows = conn.execute(
                            "SELECT voice_data FROM VoiceInfo "
                            "WHERE chat_name_id=? AND create_time=? "
                            "ORDER BY local_id DESC LIMIT 1",
                            (chat_id, _to_int(create_time)),
                        ).fetchall()
                    if rows and rows[0][0]:
                        return {"success": True, "hex": bytes(rows[0][0]).hex()}
            except sqlite3.Error as exc:
                logger.debug("读取 %s 的 VoiceInfo 失败: %s", src.name, exc)
                continue

        return {"success": False, "error": "未找到该语音消息"}

    # ── 通用 SQL（兼容旧的 exec_query 契约）─────────────────────────

    def exec_query(self, kind: str, db_path: str = "", sql: str = "") -> list[dict]:
        """通用 SQL 查询，返回行 dict 列表。

        ``kind``:
          - ``contact``  → 查 contact.db（忽略 db_path）
          - ``message``  → 查 ``db_path`` 指定的库（如 favorite.db）
          - ``fav``      → 查收藏库
        """
        if not sql:
            return []
        kind = (kind or "").strip().lower()

        if kind == "contact":
            src = self._contact_db()
        elif kind == "fav":
            src = self._favorite_db()
        else:
            src = Path(db_path) if db_path else None
            if src is not None and not src.exists():
                logger.debug("exec_query: 库不存在 %s", src)
                return []

        if src is None:
            return []

        try:
            conn = self._connect(src)
            cursor = conn.execute(sql)
            if cursor.description is None:
                return []
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            logger.warning("exec_query 失败 (%s): %s | SQL=%s", kind, exc, sql[:160])
            return []

    # ── 收藏 ────────────────────────────────────────────────────────

    def get_favorites(self, limit: int = 200, offset: int = 0) -> list[dict]:
        """收藏列表（表名 ``fav_db_item``）。"""
        return self.exec_query(
            "message", self.favorite_db_path,
            f"SELECT * FROM fav_db_item ORDER BY update_time DESC "
            f"LIMIT {int(limit)} OFFSET {int(offset)}",
        )

    # ── 图片路径解析（替代 wcdb_resolve_image_hardlink）─────────────

    def resolve_image_hardlink(self, md5: str, account_dir: str = "") -> str:
        """由图片 md5 定位 ``.dat`` 的绝对路径。

        ``image_hardlink_info_v4.dir1/dir2`` 是目录 id，需经 ``dir2id`` 还原；
        最终路径形如::

            <account_dir>/msg/attach/<dir1>/<yyyy-MM>/<Img|Thumb>/<file_name>
        """
        if not md5:
            return ""
        src = self._hardlink_db()
        if src is None:
            return ""
        try:
            conn = self._connect(src)
            row = conn.execute(
                "SELECT dir1, dir2, file_name FROM image_hardlink_info_v4 "
                "WHERE md5=? LIMIT 1",
                (md5,),
            ).fetchone()
            if row is None:
                return ""
            dir1 = self._dir_name(conn, row["dir1"])
            dir2 = self._dir_name(conn, row["dir2"])
            file_name = str(row["file_name"] or "")
        except sqlite3.Error as exc:
            logger.debug("解析图片 hardlink 失败: %s", exc)
            return ""

        base = Path(account_dir) if account_dir else self._account_dir
        # 实测布局（2026-10-01，用 hardlink.file_name 全盘核对）：
        #   <account>/msg/attach/<dir1>/<dir2>/Img/<file_name>        原图
        #   <account>/msg/attach/<dir1>/<dir2>/Img/<stem>_t.dat       缩略图
        # 注意 **Img/ 这一层不能省**，且 dir1/dir2 都要经 dir2id 还原
        # （dir2id 是一张全局 id→名字 表，dir1 与 dir2 都查它）。
        stem = file_name[:-4] if file_name.lower().endswith(".dat") else file_name
        attach = base / "msg" / "attach"
        candidates = (
            attach / dir1 / dir2 / "Img" / file_name,
            attach / dir1 / dir2 / "Img" / f"{stem}_t.dat",
            attach / dir1 / dir2 / file_name,
            attach / dir1 / file_name,
        )
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        # 文件不存在也返回推算路径，交由调用方处理
        return str(candidates[0])

    def _dir_name(self, conn: sqlite3.Connection, dir_id) -> str:
        """把 ``dir1``/``dir2`` 的数字 id 还原成目录名。"""
        if dir_id is None:
            return ""
        if isinstance(dir_id, str):
            return dir_id
        try:
            row = conn.execute(
                "SELECT username FROM dir2id WHERE rowid=?", (dir_id,)
            ).fetchone()
            if row is not None:
                return str(row[0] or "")
        except sqlite3.Error:
            pass
        return str(dir_id)

    # ── 朋友圈 ──────────────────────────────────────────────────────

    def get_sns_usernames(self) -> list[str]:
        """发过朋友圈的 wxid 列表。"""
        src = self._sns_db()
        if src is None:
            return []
        try:
            conn = self._connect(src)
            rows = conn.execute(
                "SELECT DISTINCT user_name FROM SnsTimeLine "
                "WHERE user_name IS NOT NULL AND user_name <> ''"
            ).fetchall()
            return [str(r[0]) for r in rows]
        except sqlite3.Error as exc:
            logger.error("读取朋友圈用户失败: %s", exc)
            return []

    def get_sns_timeline(self, limit: int = 20, offset: int = 0,
                         usernames=None, keyword: str = "",
                         start_time: int = 0, end_time: int = 0) -> list[dict]:
        """朋友圈时间线。

        输出字段与 DLL 保持一致（``id``/``tid`` 同时给出，``likes``/``comments``
        必须是 list），媒体与互动信息由 :mod:`sns_parser` 解析。
        """
        src = self._sns_db()
        if src is None:
            return []
        try:
            conn = self._connect(src)
            conditions = ["user_name IS NOT NULL", "user_name <> ''"]
            params: list = []
            if usernames:
                placeholders = ",".join("?" * len(usernames))
                conditions.append(f"user_name IN ({placeholders})")
                params.extend([str(u) for u in usernames])
            if start_time:
                conditions.append("tid >= ?")
                params.append(_to_signed64(_tid_unsigned(start_time)))
            if end_time:
                # 上界取"该秒最后一毫秒"的 tid（+1 秒再减 1 个序列号位），
                # 保证整秒的帖子都被包含；换算全程在无符号域进行，最后才转
                # 成有符号——否则负数相减会反向。
                conditions.append("tid <= ?")
                params.append(_to_signed64(_tid_unsigned(int(end_time) + 1) - 1))
            where = " AND ".join(conditions)
            # pack_info_buf 列声明为 TEXT 但实际存二进制，必须 CAST 成 BLOB
            # 否则 sqlite3 会尝试按 UTF-8 解码并抛错。
            rows = conn.execute(
                f"SELECT tid, user_name, content, "
                f"       CAST(pack_info_buf AS BLOB) AS pack_info_buf "
                f"FROM SnsTimeLine "
                f"WHERE {where} ORDER BY tid DESC LIMIT ? OFFSET ?",
                (*params, int(limit), int(offset)),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.error("读取朋友圈失败: %s", exc)
            return []

        from . import sns_parser

        name_map = self._contact_display_map()
        posts = []
        for row in rows:
            tid = _to_int(row["tid"])
            username = str(row["user_name"] or "")
            raw_content = row["content"]
            if isinstance(raw_content, (bytes, bytearray)):
                content_xml = bytes(raw_content).decode("utf-8", "replace")
            else:
                content_xml = str(raw_content or "")
            packed = row["pack_info_buf"]

            post = sns_parser.parse_timeline_row(
                tid=tid,
                username=username,
                content=content_xml,
                pack_info_buf=bytes(packed) if isinstance(packed, (bytes, bytearray)) else b"",
                display_name=name_map.get(username, ""),
            )
            if keyword and keyword not in (post.get("contentDesc") or ""):
                continue
            posts.append(post)
        return posts
