"""MessageStore — all database read/write operations."""

import sqlite3
import threading
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class MessageStore:
    """Wraps all database operations for message persistence and querying.

    Thread safety: uses a threading.Lock to serialize all writes.
    SQLite with check_same_thread=False allows cross-thread reads,
    but concurrent writes cause 'cannot start a transaction within a transaction'.
    The lock ensures only one write transaction is active at a time.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._write_lock = threading.Lock()
        self._trigger_count = 0

    # ── Write operations ──────────────────────────────────────────

    def insert_message(self, msg: dict) -> bool:
        """Insert a message and update the user's last-message cursor.

        Args:
            msg: Standardized message dict with keys:
                message_id, chat_id, sender_id, sender_name,
                content, msg_type, timestamp

        Returns:
            True if inserted, False if duplicate (silently skipped).
        """
        # Coerce all fields to SQLite-safe types (defensive).
        message_id = str(msg["message_id"])
        chat_id = str(msg["chat_id"])
        sender_id = str(msg["sender_id"])
        sender_name = str(msg["sender_name"])
        content = str(msg.get("content", ""))
        msg_type = int(msg.get("msg_type", 1))
        timestamp = int(msg.get("timestamp", 0))

        with self._write_lock:
            try:
                self.conn.execute(
                    """INSERT OR IGNORE INTO messages
                       (message_id, chat_id, sender_id, sender_name,
                        content, msg_type, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (message_id, chat_id, sender_id, sender_name,
                     content, msg_type, timestamp),
                )
            except sqlite3.IntegrityError:
                return False
            except sqlite3.InterfaceError:
                # Connection in bad state — reopen
                self._recover_conn()
                return False

            try:
                self.conn.execute(
                    """INSERT INTO user_last_message
                       (chat_id, sender_id, sender_name, last_timestamp)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(chat_id, sender_id) DO UPDATE SET
                       sender_name = excluded.sender_name,
                       last_timestamp = excluded.last_timestamp""",
                    (
                        msg["chat_id"], msg["sender_id"],
                        msg["sender_name"], msg["timestamp"],
                    ),
                )
            except (sqlite3.InterfaceError, sqlite3.DatabaseError):
                # Best-effort: the message INSERT already succeeded
                pass

            try:
                self.conn.commit()
            except sqlite3.OperationalError as e:
                # commit 失败 = 消息未落库。保持原行为（不改变连接状态，
                # 不调 _recover_conn），仅记录 error；下次轮询可能重新
                # 读到该消息补入库。
                logger.error("insert_message commit failed, will be re-picked next poll (chat=%s): %s",
                             chat_id, e)

        return True

    def log_trigger(self, chat_id: str, requester_id: str,
                    trigger_msg_id: str) -> None:
        """Record a trigger event for deduplication.

        Periodically cleans old entries (every 100th trigger) and
        reclaims disk space (every 1000th trigger).
        """
        with self._write_lock:
            try:
                self.conn.execute(
                    """INSERT INTO trigger_log
                       (chat_id, requester_id, trigger_message_id)
                       VALUES (?, ?, ?)""",
                    (chat_id, requester_id, trigger_msg_id),
                )
                self.conn.commit()
            except sqlite3.DatabaseError:
                pass
        self._trigger_count += 1
        if self._trigger_count % 100 == 0:
            self.cleanup_old_triggers()
        if self._trigger_count % 1000 == 0:
            self._vacuum()

    def cleanup_old_triggers(self) -> int:
        """Delete trigger_log entries older than 7 days.

        Returns:
            Number of rows deleted.
        """
        cutoff = int(time.time()) - 7 * 86400
        with self._write_lock:
            try:
                cursor = self.conn.execute(
                    "DELETE FROM trigger_log WHERE processed_at < ?",
                    (cutoff,),
                )
                deleted = cursor.rowcount
                self.conn.commit()
            except sqlite3.DatabaseError:
                deleted = 0
        if deleted:
            logger.info("Cleaned up %d old trigger_log entries.", deleted)
        return deleted

    def _vacuum(self) -> None:
        """Reclaim disk space from deleted trigger_log rows."""
        logger.info("Running VACUUM to reclaim disk space.")
        with self._write_lock:
            try:
                self.conn.execute("PRAGMA optimize")
                self.conn.commit()
            except sqlite3.DatabaseError:
                pass

    def _recover_conn(self) -> None:
        """Attempt to recover a broken SQLite connection."""
        try:
            self.conn.rollback()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        # Re-open with the same DB path
        try:
            from .schema import DB_PATH
            self.conn = sqlite3.connect(
                str(DB_PATH), check_same_thread=False,
            )
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            logger.info("MessageStore: recovered database connection")
        except Exception as e:
            logger.error("MessageStore: failed to recover connection: %s", e)

    # ── Query operations ───────────────────────────────────────────

    def get_sender_display_name(self, sender_id: str) -> Optional[str]:
        """Return a previously seen display name for a wxid, or None.

        Used as a fallback when WCDB can't resolve the wxid.  Queries the
        messages table for any past message where sender_name differs from
        sender_id (i.e. was successfully resolved at some point).
        """
        row = self.conn.execute(
            """SELECT sender_name FROM messages
               WHERE sender_id = ? AND sender_name != sender_id
               ORDER BY rowid DESC LIMIT 1""",
            (sender_id,),
        ).fetchone()
        return row["sender_name"] if row else None

    # ── contacts_cache（静默旁路，见 src/db/contact_sync.py）────────

    def load_cached_contact_ids(self) -> set:
        """已沉淀过的 username 集合（启动时一次性载入内存去重）。"""
        try:
            rows = self.conn.execute(
                "SELECT username FROM contacts_cache").fetchall()
            return {r["username"] for r in rows}
        except sqlite3.Error:
            return set()

    def insert_contacts_ignore(self, records: list) -> int:
        """只插入“新人”，主键已存在则忽略（永不 update）。

        Args:
            records: [(username, alias, nick_name, remark, display_name, kind, ts), ...]
        Returns:
            实际新增行数。
        """
        if not records:
            return 0
        with self._write_lock:
            before = self.conn.total_changes
            try:
                self.conn.executemany(
                    """INSERT OR IGNORE INTO contacts_cache
                       (username, alias, nick_name, remark, display_name, kind, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    records,
                )
                self.conn.commit()
            except sqlite3.Error as e:
                logger.warning("contacts_cache 写入失败: %s", e)
                return 0
            return self.conn.total_changes - before

    def get_recent_sender_names(self, usernames: list) -> dict:
        """批量取一批 username 在消息里最近用过的显示名（联系人表缺失时兜底）。"""
        if not usernames:
            return {}
        out: dict[str, str] = {}
        try:
            placeholders = ",".join("?" * len(usernames))
            rows = self.conn.execute(
                f"""SELECT sender_id, sender_name FROM user_last_message
                    WHERE sender_id IN ({placeholders})
                      AND sender_name IS NOT NULL AND sender_name != ''""",
                tuple(usernames),
            ).fetchall()
            for r in rows:
                out[r["sender_id"]] = r["sender_name"]
        except sqlite3.Error:
            return out
        return out

    def iter_distinct_senders(self, batch: int = 1000):
        """流式产出消息表里出现过的全部去重 sender_id（启动回填用，不一次性载入）。"""
        try:
            cur = self.conn.execute(
                "SELECT DISTINCT sender_id FROM messages WHERE sender_id IS NOT NULL AND sender_id != ''")
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                for r in rows:
                    yield r["sender_id"]
        except sqlite3.Error:
            return

    def get_user_last_timestamp(self, chat_id: str,
                                sender_id: str) -> Optional[int]:
        """Get the Unix timestamp of a user's most recent message in a chat.

        Args:
            chat_id: The chatroom ID.
            sender_id: The user's WeChat ID.

        Returns:
            Unix timestamp (int), or None if the user has never posted.
        """
        row = self.conn.execute(
            """SELECT last_timestamp FROM user_last_message
               WHERE chat_id = ? AND sender_id = ?""",
            (chat_id, sender_id),
        ).fetchone()
        return row["last_timestamp"] if row else None

    def get_user_previous_timestamp(self, chat_id: str,
                                    sender_id: str,
                                    before_ts: int) -> Optional[int]:
        """Get the timestamp of a user's last message BEFORE the given time.

        Queries the messages table directly (not user_last_message cursor)
        so the current @bot trigger message is excluded.

        Walks backward through the user's recent messages.  Any message
        whose gap to the next message toward the trigger is ≤30 seconds
        is considered part of a "burst" and skipped.  The first message
        with a gap >30s becomes the summary boundary.  If all fetched
        messages are within a single burst, the oldest one is used.

        Args:
            chat_id: The chatroom ID.
            sender_id: The user's WeChat ID.
            before_ts: Upper bound (exclusive) — find messages before this.

        Returns:
            Unix timestamp of the boundary message, or None if the user
            has no prior messages at all.
        """
        # Fetch the user's most recent messages before the trigger.
        # LIMIT 30 covers the case where the user sent a rapid burst
        # of many messages — we walk backward through all of them.
        rows = self.conn.execute(
            """SELECT timestamp FROM messages
               WHERE chat_id = ? AND sender_id = ? AND timestamp < ?
               ORDER BY timestamp DESC
               LIMIT 30""",
            (chat_id, sender_id, before_ts),
        ).fetchall()

        if not rows:
            return None

        # Walk backward: skip messages that form a close chain (gap ≤30s)
        # ending at the @bot trigger.  When a user sends several messages
        # in quick succession then @mentions the bot, the entire burst is
        # treated as a setup preamble and skipped.  The first message that
        # has a >30s gap from the next message toward the trigger becomes
        # the summary boundary.
        prev_ts = before_ts
        skipped = 0
        for row in rows:
            gap = prev_ts - row["timestamp"]
            if gap > 30:
                if skipped > 0:
                    logger.info(
                        "Skipped %d close prior messages from sender_id=%s "
                        "(final gap=%ds). Using earlier message as boundary.",
                        skipped, sender_id, gap,
                    )
                return row["timestamp"]
            skipped += 1
            prev_ts = row["timestamp"]

        # All fetched messages are within the close chain — use the
        # oldest one as the best available boundary.
        logger.info(
            "All %d prior messages from sender_id=%s are within close chain. "
            "Using oldest as boundary.",
            len(rows), sender_id,
        )
        return rows[-1]["timestamp"]

    def get_messages_since(self, chat_id: str, since_ts: int,
                           until_ts: Optional[int] = None,
                           limit: int = 500) -> list[dict]:
        """Fetch messages from a chat in a time window.

        When the window holds more than `limit` messages the OLDEST ones are
        dropped, so a busy chat still yields its most recent activity.

        Args:
            chat_id: The chatroom ID.
            since_ts: Start of window (inclusive), Unix seconds.
            until_ts: End of window (inclusive). Defaults to now.
            limit: Maximum number of messages to return.

        Returns:
            List of message dicts, ordered by timestamp ascending.
        """
        if until_ts is None:
            until_ts = int(time.time())

        # Inner query takes the newest `limit` rows (uses idx_msg_chat_time,
        # which is keyed on (chat_id, timestamp DESC)); outer query restores
        # ascending order so callers can rely on chronological input.
        # message_id is an MD5 text key — only a deterministic tie-break for
        # messages sharing a timestamp, not a business ordering.
        rows = self.conn.execute(
            """SELECT message_id, chat_id, sender_id, sender_name,
                      content, msg_type, timestamp
               FROM (
                   SELECT message_id, chat_id, sender_id, sender_name,
                          content, msg_type, timestamp
                   FROM messages
                   WHERE chat_id = ? AND timestamp BETWEEN ? AND ?
                   ORDER BY timestamp DESC, message_id DESC
                   LIMIT ?
               )
               ORDER BY timestamp ASC, message_id ASC""",
            (chat_id, since_ts, until_ts, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def was_recently_triggered(self, chat_id: str,
                                window_sec: int) -> bool:
        """Check if a trigger was processed for this chat recently.

        Args:
            chat_id: The chatroom ID.
            window_sec: Lookback window in seconds.

        Returns:
            True if a trigger was processed within the window.
        """
        cutoff = int(time.time()) - window_sec
        row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM trigger_log
               WHERE chat_id = ? AND processed_at > ?""",
            (chat_id, cutoff),
        ).fetchone()
        return row["cnt"] > 0 if row else False

    # ── Group memory operations ────────────────────────────────────

    def get_group_memory(self, chat_id: str) -> dict | None:
        """Retrieve the memory record for a group.

        Returns:
            Dict with keys: chat_id, memory_text, message_count,
            last_message_id, last_consolidated, created_at, updated_at.
            None if no memory exists yet for this group.
        """
        row = self.conn.execute(
            """SELECT chat_id, memory_text, message_count,
                      last_message_id, last_consolidated,
                      created_at, updated_at
               FROM group_memory
               WHERE chat_id = ?""",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_group_memory(self, chat_id: str, memory_text: str,
                            message_count: int, last_message_id: str) -> None:
        """Insert or update a group's memory record.

        Args:
            chat_id: Group chat ID.
            memory_text: The consolidated first-person memory text.
            message_count: Total messages incorporated into this memory.
            last_message_id: The last message ID that was incorporated.
        """
        now = time.time()
        with self._write_lock:
            self.conn.execute(
                """INSERT INTO group_memory
                   (chat_id, memory_text, message_count, last_message_id,
                    last_consolidated, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                   memory_text = excluded.memory_text,
                   message_count = excluded.message_count,
                   last_message_id = excluded.last_message_id,
                   last_consolidated = excluded.last_consolidated,
                   updated_at = excluded.updated_at""",
                (chat_id, memory_text, message_count, last_message_id,
                 now, now, now),
            )
            try:
                self.conn.commit()
            except sqlite3.OperationalError as e:
                # commit 失败 = 群记忆未落库。保持原行为（不改变连接状态），
                # 仅记录 error。
                logger.error("upsert_group_memory commit failed (chat=%s): %s", chat_id, e)

    def get_new_message_count(self, chat_id: str,
                              since_message_id: str | None) -> int:
        """Count new messages in a chat since a given message ID.

        Args:
            chat_id: Group chat ID.
            since_message_id: The last incorporated message ID.
                              If None, count all messages.

        Returns:
            Number of messages after the given ID.
        """
        if since_message_id is None:
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        else:
            # Get the rowid of the last incorporated message, then count newer ones
            row = self.conn.execute(
                """SELECT COUNT(*) as cnt FROM messages
                   WHERE chat_id = ? AND rowid > (
                       SELECT COALESCE(
                           (SELECT rowid FROM messages WHERE message_id = ?), 0
                       )
                   )""",
                (chat_id, since_message_id),
            ).fetchone()
        return row["cnt"] if row else 0

    def get_messages_since_id(self, chat_id: str,
                              since_message_id: str | None,
                              limit: int = 200) -> list[dict]:
        """Fetch messages since a given message ID.

        Args:
            chat_id: Group chat ID.
            since_message_id: Last incorporated message ID. None = from beginning.
            limit: Max messages to return.

        Returns:
            List of message dicts in chronological order.
        """
        if since_message_id is None:
            rows = self.conn.execute(
                """SELECT message_id, chat_id, sender_id, sender_name,
                          content, msg_type, timestamp
                   FROM messages
                   WHERE chat_id = ?
                   ORDER BY timestamp ASC
                   LIMIT ?""",
                (chat_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT message_id, chat_id, sender_id, sender_name,
                          content, msg_type, timestamp
                   FROM messages
                   WHERE chat_id = ? AND rowid > (
                       SELECT COALESCE(
                           (SELECT rowid FROM messages WHERE message_id = ?), 0
                       )
                   )
                   ORDER BY timestamp ASC
                   LIMIT ?""",
                (chat_id, since_message_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
