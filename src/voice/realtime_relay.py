# -*- coding: utf-8 -*-
"""Qwen-Audio-Realtime 全双工中继桥。

职责（不碰文字 Agent 的任何状态，只借用只读工具）：

    浏览器 (16k/24k PCM 二进制帧 + JSON 控制帧)
          │  /ws/voice  (本机 WebSocket)
          ▼
    RealtimeRelay  ── 事件翻译 + function_call 工具桥 ──
          │  wss://maas.qianwenaiapi.com/api-ws/v1/realtime
          ▼
      Qwen-Audio 端到端实时模型

协议要点（与 OpenAI Realtime 一致）：
- 上行麦克风：浏览器二进制 PCM16/16k 帧 → base64 包成 input_audio_buffer.append
- 下行语音：response.audio.delta(base64 PCM16/24k) → 解码成二进制帧推给浏览器
- 字幕/状态：其余事件转 JSON 文本帧透传
- 工具：response 中出现 function_call → 本地 ToolRegistry 执行 →
  conversation.item.create(function_call_output) → response.create 触发二轮

中继在独立 asyncio 事件循环线程中运行（http.server 是同步多线程模型）。
"""

import asyncio
import base64
import json
import logging
import threading
import time

from src.config import load_config
from src.voice.call_logger import VoiceCallLogger, new_call_id

logger = logging.getLogger(__name__)

REALTIME_WSS = "wss://maas.qianwenaiapi.com/api-ws/v1/realtime"

# 语音全双工只开放只读/无副作用工具。写操作（add_alert/create_cron 等）
# 一律不注册，避免语音误触发；需要配置时引导用户去文字 Agent / 网页。
VOICE_TOOL_WHITELIST = {
    "query_my_feed",
    "get_status",
    "list_digests",
    "list_alerts",
    "list_oa_groups",
    "list_oa_monitors",
    "list_tasks",
    "list_crons",
    "get_latest_oa_digest",
    "get_latest_group_digest",
    "search_chat_history",
    "search_oa_articles",
    "search_moments",
    "search_favorites",
    "search_oa_accounts",
}

VOICE_INSTRUCTIONS = (
    "你是摘星语音助手，运行在用户的个人微信机器人上，通过打电话的方式与用户交流。"
    "你可以调用工具查询用户的历史推送（公众号提醒、群聊摘要、定时榜单等）、"
    "聊天记录、公众号文章、朋友圈和收藏。\n"
    "规则：\n"
    "1. 需要事实信息时必须先调用工具，禁止编造；工具没有结果就如实说没找到。\n"
    "2. 回答口语化、简洁，先给结论；不要使用 Markdown、表格、星号和长列表。\n"
    "3. 日期用相对说法（今天、昨天），不要念完整的年月日数字。\n"
    "4. 单次回答控制在 300 字内，信息量大时说重点，等用户追问再展开。\n"
    "5. 你不能修改任何配置（不能加预警、建定时任务）；用户要求这类操作时，"
    "礼貌说明语音模式只支持查询，建议在微信对话或网页里操作。"
)

# 下行事件中不需要原样转发给浏览器的（中继自行处理音频/工具）
_AUDIO_DELTA = "response.audio.delta"
_FUNC_CALL_DONE = "response.function_call_arguments.done"


def realtime_configured() -> bool:
    """是否已启用端到端实时语音（需开关 + Key）。"""
    try:
        cfg = load_config()
        return bool(getattr(cfg, "voice_realtime_enabled", False)
                    and getattr(cfg, "voice_realtime_api_key", ""))
    except Exception:
        return False


def build_voice_tool_schemas(registry) -> list[dict]:
    """从 ToolRegistry 提取白名单只读工具的 schema（含 MCP 包装层）。

    registry 可能是 MCPToolRegistry/ProxyRegistry——它们都实现了
    get_all_schemas()，MCP 工具名形如 server__tool，白名单按后缀放行
    联网/抓取类（bocha/fetch）。
    """
    if registry is None:
        return []
    try:
        all_schemas = registry.get_all_schemas()
    except Exception:
        logger.exception("[VOICE-RT] get_all_schemas failed")
        return []

    allowed_mcp_suffix = ("bocha_web_search",)  # 联网搜索以工具形式开放
    tools = []
    for sch in all_schemas:
        fn = sch.get("function", {})
        name = fn.get("name", "")
        if name in VOICE_TOOL_WHITELIST:
            tools.append({"type": "function", "function": fn})
        elif "__" in name and name.split("__", 1)[1] in allowed_mcp_suffix:
            tools.append({"type": "function", "function": fn})
    return tools


class RealtimeRelay:
    """一条浏览器通话 ↔ 一个千问 realtime 会话的中继。

    Args:
        browser_send: 同步回调 (payload:bytes)→None。传 bytes 表示二进制音频帧，
                      传 str 表示 JSON 文本帧。由 server 的 WS 写出层注入。
        browser_closed: 无参回调，返回 bool。浏览器连接是否已断开（用于停止上行 pump）。
        registry: ToolRegistry（含 MCP），用于执行 function_call；可为 None。
    """

    def __init__(self, browser_send, browser_closed, registry=None,
                 session_id: str | None = None):
        self._browser_send = browser_send
        self._browser_closed = browser_closed
        self._registry = registry
        self.sid = session_id or new_call_id()
        self._call = VoiceCallLogger(self.sid, "qwen")
        self._loop = None
        self._thread = None
        self._ws = None
        self._stop = threading.Event()
        # call_id → (name, arguments)
        self._pending_funcs: dict[str, tuple[str, str]] = {}
        self._tool_schemas: list[dict] = []

    # ── 生命周期（由 server WS 层调用，均为同步）─────────────────────

    def start(self) -> None:
        cfg = load_config()
        self._tool_schemas = build_voice_tool_schemas(self._registry)
        self._thread = threading.Thread(
            target=self._run_loop, name="voice-realtime", daemon=True,
        )
        self._thread.start()

    def send_audio(self, pcm_bytes: bytes) -> None:
        """浏览器上行麦克风 PCM16/16k。丢给事件循环编码后转发。"""
        if self._loop is None or not pcm_bytes:
            return
        asyncio.run_coroutine_threadsafe(
            self._upstream_audio(pcm_bytes), self._loop,
        )

    def send_text(self, message: str) -> None:
        """浏览器上行 JSON 控制帧（如手动取消/配置覆盖）。"""
        if self._loop is None or not message:
            return
        try:
            data = json.loads(message)
        except (ValueError, TypeError):
            return
        asyncio.run_coroutine_threadsafe(
            self._upstream_json(data), self._loop,
        )

    def close(self) -> None:
        self._stop.set()
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._async_close(), self._loop)

    # ── asyncio 内部 ────────────────────────────────────────────────

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._pipeline())
        except Exception:
            logger.exception("[VOICE-RT] sid=%s pipeline crashed", self.sid)
            self._call.log_close("failed", error="pipeline crashed")
            self._emit({"type": "realtime.error",
                        "message": "实时语音连接异常断开"})
        finally:
            self._loop.close()
            self._loop = None

    async def _pipeline(self) -> None:
        import websockets

        t0 = time.monotonic()
        cfg = load_config()
        self._call = VoiceCallLogger(
            self.sid, "qwen", voice=cfg.voice_realtime_voice,
            model=cfg.voice_realtime_model, tools_count=len(self._tool_schemas))
        url = f"{REALTIME_WSS}?model={cfg.voice_realtime_model}"
        headers = {
            "Authorization": f"Bearer {cfg.voice_realtime_api_key}",
        }
        logger.info("[VOICE-RT] sid=%s connecting model=%s tools=%d",
                    self.sid, cfg.voice_realtime_model, len(self._tool_schemas))

        try:
            async with websockets.connect(
                url, additional_headers=headers, max_size=None,
                ping_interval=20, ping_timeout=20, open_timeout=15,
            ) as ws:
                self._ws = ws
                await self._send_session_update(cfg)
                logger.info("[VOICE-RT] sid=%s session ready in %.0fms",
                            self.sid, (time.monotonic() - t0) * 1000)
                self._call.log_open()
                self._emit({"type": "realtime.ready"})

                # 浏览器断开检测任务
                async def watch_browser():
                    while not self._stop.is_set():
                        if self._browser_closed():
                            await ws.close()
                            return
                        await asyncio.sleep(0.3)

                await asyncio.gather(
                    self._downstream_pump(ws),
                    watch_browser(),
                )
                # gather 返回 = 会话结束：区分浏览器挂断与上游断开
                ended_by = "browser-hangup" if self._browser_closed() else "upstream-close"
                logger.info("[VOICE-RT] sid=%s session ended by=%s code=%s reason=%r duration=%.1fs",
                            self.sid, ended_by, getattr(ws, "close_code", None),
                            getattr(ws, "close_reason", ""), time.monotonic() - t0)
                self._call.log_close("ok", by=ended_by)
        except Exception as e:
            # 建连/握手/运行期上游异常：明确日志。是否通知前端降级按就绪状态区分，
            # 保持原语义不变：就绪前失败 → 降级 A 档；通话已开始后断开 → 仅提示。
            msg = str(e)
            logger.warning("[VOICE-RT] sid=%s upstream failed after %.1fs: %s",
                           self.sid, time.monotonic() - t0, msg[:200])
            if not self._call.ready:
                self._call.log_close("downgrade", error=msg[:200])
                hint = ("鉴权失败，请检查千问 API Key"
                        if ("401" in msg or "403" in msg or "Handshake" in msg)
                        else f"连接异常：{msg[:100]}")
                self._emit({"type": "realtime.unavailable",
                            "message": f"千问实时语音{hint}，已切换标准语音模式"})
            else:
                self._call.log_close("failed", error=msg[:200])
                self._emit({"type": "realtime.error",
                            "message": "实时语音连接异常断开"})

    async def _send_session_update(self, cfg) -> None:
        turn_mode = (getattr(cfg, "voice_realtime_turn_mode", "smart_turn")
                     or "smart_turn")
        if turn_mode == "server_vad":
            turn_detection = {"type": "server_vad",
                              "threshold": 0.5, "silence_duration_ms": 700}
        else:
            turn_detection = {"type": "smart_turn"}

        model = (cfg.voice_realtime_model or "").lower()
        voice = cfg.voice_realtime_voice or ""

        # 两个模型家族的会话结构不同（实测）：
        # - qwen-audio-realtime：voice 在顶层，PCM 格式在顶层
        # - qwen*-omni-*-realtime：音色走 audio.output.voice（Tina 等 Omni 音色），
        #   不接受顶层 voice/format，音频默认即 PCM
        is_omni = "omni" in model
        if is_omni:
            session = {
                "modalities": ["text", "audio"],
                "audio": {"output": {"voice": voice or "Tina"}},
                "instructions": VOICE_INSTRUCTIONS,
                "turn_detection": turn_detection,
            }
        else:
            session = {
                "modalities": ["text", "audio"],
                "voice": voice or "longanqian_v3.1",
                "instructions": VOICE_INSTRUCTIONS,
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "turn_detection": turn_detection,
            }
        if self._tool_schemas:
            session["tools"] = self._tool_schemas
        await self._ws_send({"type": "session.update", "session": session})

    async def _upstream_audio(self, pcm: bytes) -> None:
        if self._ws is None:
            return
        await self._ws_send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        })

    async def _upstream_json(self, data: dict) -> None:
        # 只放行安全的控制事件，其余忽略
        if data.get("type") == "response.cancel":
            await self._ws_send({"type": "response.cancel"})

    async def _downstream_pump(self, ws) -> None:
        async for raw in ws:
            if self._stop.is_set():
                break
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                continue
            await self._handle_downstream(event)

    async def _handle_downstream(self, event: dict) -> None:
        etype = event.get("type", "")

        # 1) 音频：解码成二进制帧，低延迟直推浏览器
        if etype == _AUDIO_DELTA:
            b64 = event.get("delta", "")
            if b64:
                try:
                    self._browser_send(base64.b64decode(b64))
                except Exception:
                    pass
            return

        # 2) 工具调用：在 reply 完成时执行并回灌
        if etype == "response.output_item.done":
            item = event.get("item", {}) or {}
            if item.get("type") == "function_call":
                await self._handle_function_call(item)
                # function_call 项不转发给前端（避免字幕噪声）
                return

        # 2.5) 语音链路关键事件留痕（识别结果 / AI 文本 / 服务端错误）+ 轮次聚合
        if etype == "conversation.item.input_audio_transcription.completed":
            txt = (event.get("transcript") or "").strip()
            if txt:
                self._call.log_user(txt)
                logger.info("[VOICE-RT] sid=%s user >> %s", self.sid, txt[:100])
        elif etype == "response.audio_transcript.done":
            txt = (event.get("transcript") or "").strip()
            if txt:
                self._call.log_assistant(txt)
                logger.info("[VOICE-RT] sid=%s assistant << %s", self.sid, txt[:100])
        elif etype == "error":
            logger.warning("[VOICE-RT] sid=%s server error event: %s",
                           self.sid,
                           json.dumps(event.get("error", event), ensure_ascii=False)[:200])

        # 3) 其余事件（字幕、轮次、speech_started 打断、done、error 等）透传
        self._emit(event)

    async def _handle_function_call(self, item: dict) -> None:
        call_id = item.get("call_id", "")
        name = item.get("name", "")
        arguments = item.get("arguments", "{}") or "{}"

        self._emit({"type": "voice.tool_start", "name": name})
        logger.info("[VOICE-RT] sid=%s function_call %s(%s)",
                    self.sid, name, arguments[:200])

        tool_t0 = time.monotonic()
        result = await asyncio.to_thread(self._execute_tool, name, arguments)
        tool_ms = (time.monotonic() - tool_t0) * 1000
        result = result or "（工具返回为空）"
        # 控制回灌体积，音频通话不需要超长内容
        if len(result) > 6000:
            result = result[:6000] + "\n…（内容过长已截断）"

        self._call.log_tool(name, arguments, result, tool_ms)
        self._emit({"type": "voice.tool_done", "name": name,
                    "preview": result[:160]})

        await self._ws_send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
        })
        # 触发二轮推理（让模型基于工具结果生成语音回答）
        await self._ws_send({
            "type": "response.create",
            "response": {"modalities": ["audio", "text"]},
        })

    def _execute_tool(self, name: str, arguments: str) -> str:
        """同步执行本地/MCP 工具（在工作线程中，不阻塞事件循环）。"""
        if self._registry is None:
            return "工具不可用：Agent 未启动"
        try:
            args = json.loads(arguments) if arguments else {}
            if not isinstance(args, dict):
                args = {}
        except (ValueError, TypeError):
            args = {}
        try:
            return self._registry.execute(name, args)
        except Exception as e:
            logger.warning("[VOICE-RT] sid=%s tool %s failed: %s", self.sid, name, e)
            return f"工具执行出错: {e}"

    async def _ws_send(self, data: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.debug("[VOICE-RT] upstream send failed", exc_info=True)

    async def _async_close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    def _emit(self, event: dict) -> None:
        """向浏览器发一条 JSON 文本帧。"""
        try:
            self._browser_send(json.dumps(event, ensure_ascii=False))
        except Exception:
            pass
