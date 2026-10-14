# -*- coding: utf-8 -*-
"""联系人资料静默同步器（纯旁路，不参与任何主流程）。

设计目标：用户完全无感、对消息轮询零影响、可承载大数据量。

- 热路径只有 :meth:`note`：新消息成功入库后调用一次，仅做一次内存集合
  去重（O(1)，不碰数据库、不抢锁）。
- 真正查联系人库 / 写本地库由独立 daemon 线程低频批量完成，与轮询节奏解耦。
- 只“插入首次出现的人”，已存在一律忽略（INSERT OR IGNORE），不更新、不删除，
  因此改名不会产生写、也不会改动历史。
- 联系人表查不到的路人，用消息里最近的显示名兜底，至少留一个名字。
- 任何异常都被吞掉并降级为空转，绝不向调用方抛出。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 守护线程一轮的间隔：稳态下绝大多数周期空转
_INTERVAL_SEC = 60.0
# 单批处理的候选人数（控制单次 SQLite/联系人库开销与 IN 列表长度）
_BATCH = 300
# 每批之间的让步睡眠，给主流程让路
_BATCH_SLEEP = 0.2
# 启动历史回填时，每批从消息表扫入待处理集合的人数
_BACKFILL_BATCH = 1000
# 首轮回填前等待“可查联系人的客户端”就绪的最长时间。
# 客户端在后端 start() 后才创建，过早回填会导致资料只能走消息兜底。
_WAIT_CLIENT_TIMEOUT = 30.0
_WAIT_CLIENT_PERIOD = 2.0


def _classify(username: str) -> str:
    """粗分联系人类型，仅用于缓存标记。"""
    u = username or ""
    if u.endswith("@chatroom"):
        return "chatroom"
    if u.startswith("gh_"):
        return "official"
    if "@" in u:
        return "system"
    return "user"


class ContactSyncer:
    def __init__(self,
                 store,
                 client_provider: Optional[Callable] = None,
                 interval: float = _INTERVAL_SEC):
        """
        Args:
            store: MessageStore（读写本地 contacts_cache）。
            client_provider: 无参回调，返回当前可用的微信客户端
                （需提供 get_contacts_bulk）。后端启动后才有实例，因此惰性获取。
            interval: 守护线程周期（测试可调小）。
        """
        self._store = store
        self._client_provider = client_provider
        self._interval = interval

        self._lock = threading.Lock()
        self._pending: set[str] = set()
        # 已沉淀的 username：启动时一次性载入，之后只增不删
        try:
            self._known: set[str] = set(store.load_cached_contact_ids())
        except Exception:
            self._known = set()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._backfill_done = False

    # ── 生命周期 ─────────────────────────────────────────────────────

    def attach_client_provider(self, provider: Callable) -> None:
        self._client_provider = provider

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="contact-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None

    # ── 热路径：只做内存去重，绝不抛异常 ─────────────────────────────

    def note(self, username: str) -> None:
        """新消息成功入库后调用。O(1)，不接触数据库。"""
        try:
            u = str(username or "").strip()
            if not u:
                return
            with self._lock:
                if u not in self._known:
                    self._pending.add(u)
        except Exception:
            # 旁路功能永远不影响消息主流程
            logger.debug("contact note 失败", exc_info=True)

    # ── 后台循环 ─────────────────────────────────────────────────────

    def _loop(self) -> None:
        # 首轮先等可查联系人的客户端就绪（带超时；无此能力的后端超时后走兜底）
        self._wait_for_client()
        while self._running:
            try:
                if not self._backfill_done:
                    self._run_backfill()
                    self._backfill_done = True
                self._drain_pending()
            except Exception:
                logger.debug("contact sync 轮次失败", exc_info=True)
            # 可被 stop 快速唤醒的等待
            self._interruptible_sleep(self._interval)

    def _wait_for_client(self) -> bool:
        """等待具备 get_contacts_bulk 能力的客户端出现。

        未挂载 provider（极少见）或等待超时 → 返回 False，调用方按“无联系人库、
        仅消息兜底”继续，保证其他后端不会被永久阻塞。
        """
        if self._client_provider is None:
            return False
        end = time.monotonic() + _WAIT_CLIENT_TIMEOUT
        while self._running:
            if self._safe_client() is not None:
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(_WAIT_CLIENT_PERIOD)
        return False

    def _interruptible_sleep(self, sec: float) -> None:
        end = time.monotonic() + sec
        while self._running and time.monotonic() < end:
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    def _take_pending(self, limit: int) -> list[str]:
        with self._lock:
            if not self._pending:
                return []
            items = list(self._pending)[:limit]
            for u in items:
                self._pending.discard(u)
        return items

    def _mark_known(self, usernames) -> None:
        with self._lock:
            for u in usernames:
                self._known.add(u)

    def _requeue(self, usernames) -> None:
        """本轮处理失败的放回，下轮重试（注意：不并入 _known）。"""
        with self._lock:
            for u in usernames:
                if u not in self._known:
                    self._pending.add(u)

    # ── 启动历史回填：消息表里出现过的人，分批入队（不一次性载入）──────

    def _run_backfill(self) -> None:
        try:
            buf: list[str] = []
            for u in self._store.iter_distinct_senders(_BACKFILL_BATCH):
                u = str(u or "").strip()
                if not u:
                    continue
                with self._lock:
                    if u not in self._known:
                        buf.append(u)
                        self._pending.add(u)
                if len(buf) >= _BACKFILL_BATCH:
                    # 边收集边消化，避免首个周期堆积过多
                    self._drain_pending()
                    buf = []
                    if not self._running:
                        return
        except Exception:
            logger.debug("contact 回填失败", exc_info=True)

    # ── 实际查库 + 落库 ──────────────────────────────────────────────

    def _drain_pending(self) -> None:
        # 不在此处判 _running：待处理集合取空即返回（不会空转）；
        # 节奏与启停由外层 _loop 控制，停止时最多多处理一个批次。
        client = self._safe_client()
        while True:
            batch = self._take_pending(_BATCH)
            if not batch:
                return

            records: list[tuple] = []
            now = int(time.time())

            # 1) 优先查微信联系人表（有该方法才查；其他后端拿不到就直接兜底）
            resolved: dict = {}
            if client is not None:
                try:
                    resolved = client.get_contacts_bulk(batch) or {}
                except Exception:
                    resolved = {}
                    logger.debug("get_contacts_bulk 异常", exc_info=True)

            # 2) 联系人表缺失的，用本地消息最近显示名兜底
            missing = [u for u in batch if u not in resolved]
            fallback = self._store.get_recent_sender_names(missing) if missing else {}

            for u in batch:
                info = resolved.get(u)
                if info:
                    records.append((
                        u,
                        info.get("alias", ""),
                        info.get("nick_name", ""),
                        info.get("remark", ""),
                        info.get("display_name", "") or u,
                        "chatroom" if info.get("is_chatroom") else _classify(u),
                        now,
                    ))
                else:
                    name = (fallback.get(u) or u).strip() or u
                    records.append((u, "", "", "", name, _classify(u), now))

            # 3) 只插新人（INSERT OR IGNORE，已存在不更新）
            try:
                self._store.insert_contacts_ignore(records)
            except Exception:
                # 落库异常：放回待处理，下轮重试
                self._requeue(batch)
                logger.debug("contacts 落库异常", exc_info=True)
                return

            # 成功（无论实际新增几条——主键已存在也视为处理完成）
            self._mark_known(batch)
            time.sleep(_BATCH_SLEEP)

    def _safe_client(self):
        provider = self._client_provider
        if provider is None:
            return None
        try:
            client = provider()
        except Exception:
            return None
        if client is None or not hasattr(client, "get_contacts_bulk"):
            return None
        return client
