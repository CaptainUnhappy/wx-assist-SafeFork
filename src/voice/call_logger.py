# -*- coding: utf-8 -*-
"""实时语音通话结构化日志（层次对齐 src/utils/llm_logger.py）。

一通通话（一次 /ws/voice 连接）分配一个 sid，输出三类行到 bot.log：

1. ``[VOICE-CALL] sid=... OPEN provider=... voice=... model=... tools=N``
   通话开始（实时会话 ready 之后）。
2. ``[VOICE-DETAIL] {...JSON...}``
   一轮对话的完整闭环：用户转写 + 工具调用（参数/返回预览/耗时）+ AI 回复。
3. ``[VOICE-CALL] sid=... CLOSED status=ok|downgrade|failed by=... duration=... turns=N``
   通话结束。

前端 LogViewer 按 sid 归组，把一通通话渲染成一张可展开的通话卡片；
普通 ``[VOICE-*]`` 散点行（connecting/ready/ended 等）作为卡片内的时间线。

与 llm_logger 相同的约束：
- 只写 bot.log（前端 /api/logs 的数据源），沿用 root logger；
- 不记录任何 API Key；
- 长文本截断，避免单行撑爆日志。
"""

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 截断阈值（字符）
_MAX_USER = 200
_MAX_ASSISTANT = 500
_MAX_TOOL_ARGS = 300
_MAX_TOOL_RESULT = 300
_MAX_ERROR = 200

_counter = 0
_counter_lock = threading.Lock()


def new_call_id() -> str:
    """生成通话 ID：voice_YYYYMMDD_HHMMSS_NNNN（线程安全）。"""
    global _counter
    with _counter_lock:
        _counter += 1
        return f"voice_{time.strftime('%Y%m%d_%H%M%S')}_{_counter:04d}"


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"…({len(text)}字)"


class VoiceCallLogger:
    """一通实时通话的轮次聚合器，由 relay 在事件回调中驱动。"""

    def __init__(self, sid: str, provider: str, *,
                 voice: str = "", model: str = "", tools_count: int = 0):
        self.sid = sid
        self.provider = provider
        self.voice = voice or ""
        self.model = model or ""
        self.tools_count = tools_count
        self._t0 = time.monotonic()
        self.ready = False  # 是否已完成实时握手（区分 ready 前降级 / ready 后失败）
        self._turn_no = 0
        self._cur: dict | None = None  # 未闭合的一轮
        self._close_written = False

    # ── 通话头尾 ─────────────────────────────────────────────────────

    def log_open(self) -> None:
        """实时会话 ready：通话正式开始。"""
        self.ready = True
        self._t0 = time.monotonic()
        logger.info(
            "[VOICE-CALL] sid=%s OPEN provider=%s voice=%s model=%s tools=%d",
            self.sid, self.provider, self.voice or "-", self.model or "-",
            self.tools_count,
        )

    def log_close(self, status: str = "ok", by: str = "", error: str = "") -> None:
        """通话结束。status: ok | downgrade | failed。只写一次。"""
        if self._close_written:
            return
        self._close_written = True
        # 残留未闭合轮次（用户说了但没等到 AI 答完就挂断/断开）
        if self._cur is not None:
            self._flush(interrupted=True)
        duration = time.monotonic() - self._t0 if self.ready else 0.0
        tail = f" by={by}" if by else ""
        if status != "ok" and error:
            tail += f" error={_clip(error, _MAX_ERROR)}"
        logger.info(
            "[VOICE-CALL] sid=%s CLOSED status=%s%s duration=%.1fs turns=%d",
            self.sid, status, tail, duration, self._turn_no,
        )

    # ── 轮次事件（由两个 relay 在归一化前的原始事件点驱动）──────────────

    def log_user(self, text: str) -> None:
        """用户一句话转写完成 → 开启新一轮。"""
        text = (text or "").strip()
        # 上一轮没有正常闭合（未收到 assistant.done），先按中断落盘
        if self._cur is not None:
            self._flush(interrupted=True)
        self._turn_no += 1
        self._cur = {
            "turn": self._turn_no,
            "user": _clip(text, _MAX_USER),
            "assistant": "",
            "tools": [],
            "t0": time.monotonic(),
        }

    def log_tool(self, name: str, arguments: str, result: str,
                 elapsed_ms: float, ok: bool = True) -> None:
        """一次工具调用完成。若当前无轮次（服务端未回传用户转写）则兜底建轮。"""
        if self._cur is None:
            self._turn_no += 1
            self._cur = {
                "turn": self._turn_no, "user": "", "assistant": "",
                "tools": [], "t0": time.monotonic(),
            }
        self._cur["tools"].append({
            "name": name,
            "args": _clip(arguments, _MAX_TOOL_ARGS),
            "result": _clip(result, _MAX_TOOL_RESULT),
            "ms": int(elapsed_ms),
            "ok": bool(ok),
        })

    def log_assistant(self, text: str) -> None:
        """AI 一轮语音文本完成 → 闭合当前轮。"""
        if self._cur is None:
            # 无用户转写、无工具的兜底轮（服务端主动开口）
            self._turn_no += 1
            self._cur = {
                "turn": self._turn_no, "user": "", "assistant": "",
                "tools": [], "t0": time.monotonic(),
            }
        self._cur["assistant"] = _clip((text or "").strip(), _MAX_ASSISTANT)
        self._flush(interrupted=False)

    # ── 内部 ─────────────────────────────────────────────────────────

    def _flush(self, *, interrupted: bool) -> None:
        cur = self._cur
        if cur is None:
            return
        self._cur = None
        payload = {
            "sid": self.sid,
            "provider": self.provider,
            "turn": cur["turn"],
            "user": cur["user"],
            "assistant": cur["assistant"],
            "tools": cur["tools"],
            "latency_ms": int((time.monotonic() - cur["t0"]) * 1000),
            "interrupted": bool(interrupted),
        }
        logger.info("[VOICE-DETAIL] %s",
                    json.dumps(payload, ensure_ascii=False))
