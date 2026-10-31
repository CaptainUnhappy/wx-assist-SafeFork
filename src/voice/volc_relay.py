# -*- coding: utf-8 -*-
"""火山引擎豆包 Seeduplex 3.0 全双工中继（Realtime 事件协议，model 1.2.6.1）。

与千问 realtime_relay 对外接口完全一致（start/send_audio/send_text/close），
由 server 的 /ws/voice 按配置 VOICE_RT_PROVIDER 选择，浏览器侧无感知差异——
火山事件在本中继内归一化成千问/OpenAI 同名事件后再下发。

与千问的关键差异（均在本文件内消化）：
1. 鉴权：X-Api-Key 头（不是 Bearer）；
2. 开场：连上后客户端主动发 session.create；
3. 音频：入 16k/16bit PCM、出必须显式指定 pcm_s16le/24k（裸 "pcm" 是 32 位！）；
4. 节奏：上行必须严格 20ms/包（640 字节），由服务端 pacer 重切；麦克风静默
   时持续注入静音帧保活，否则服务端超时断连；
5. 工具结果：role="tool" + content 数组（千问是 function_call_output + output）；
6. 事件名：output_audio.delta / output_text.delta（归一化成 audio.* 给前端）。
"""

import asyncio
import base64
import json
import logging
import threading
import time

from src.config import load_config
from src.voice.call_logger import VoiceCallLogger, new_call_id
from src.voice.realtime_relay import (
    VOICE_INSTRUCTIONS,
    build_voice_tool_schemas,
)

logger = logging.getLogger(__name__)

VOLC_WSS = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
VOLC_MODEL = "1.2.6.1"

# 20ms @16k/16bit 单声道
FRAME_BYTES = 640
FRAME_INTERVAL = 0.02
SILENCE_FRAME = b"\x00" * FRAME_BYTES

# 工具执行超时：超过就放弃等待并回一句超时提示，避免整通电话卡在"思考中"
TOOL_TIMEOUT_SEC = 20

# 火山主打音色（S2S 全双工保证效果的 4 个 2.0 音色 + 常用款）
VOLC_VOICES = [
    {"id": "zh_female_vv_uranus_bigtts", "name": "Vivi 2.0 · 女声（默认）"},
    {"id": "zh_female_xiaohe_uranus_bigtts", "name": "小何 2.0 · 女声"},
    {"id": "zh_male_m191_uranus_bigtts", "name": "云舟 2.0 · 男声"},
    {"id": "zh_male_taocheng_uranus_bigtts", "name": "小天 2.0 · 男声"},
]


def volc_configured() -> bool:
    try:
        cfg = load_config()
        return bool(getattr(cfg, "voice_volc_enabled", False)
                    and getattr(cfg, "voice_volc_api_key", ""))
    except Exception:
        return False


def _flatten_tools(tools: list[dict]) -> list[dict]:
    """OpenAI 嵌套式 [{type:function,function:{...}}] → 火山扁平式
    [{type:function,name,description,parameters}]。火山两种都可能兼容，
    扁平式是其官方 demo 用法，保险起见做一次转换。"""
    out = []
    for t in tools or []:
        fn = t.get("function", t)
        out.append({
            "type": "function",
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


class VolcRelay:
    """浏览器通话 ↔ 火山 Seeduplex 会话的中继。接口同 RealtimeRelay。"""

    def __init__(self, browser_send, browser_closed, registry=None,
                 session_id: str | None = None):
        self._browser_send = browser_send
        self._browser_closed = browser_closed
        self._registry = registry
        self.sid = session_id or new_call_id()
        self._call = VoiceCallLogger(self.sid, "volc")
        self._loop = None
        self._thread = None
        self._ws = None
        self._stop = threading.Event()
        self._tool_schemas: list[dict] = []
        # 上行音频缓冲（pacer 重切成 20ms 帧）
        self._audio_q: asyncio.Queue = None  # type: ignore
        self._pacer_task = None
        # 工具结果回传后，等模型开口的计时起点（拿到首个音频分片时打日志）
        self._await_model_since: float | None = None

    # ── 同步生命周期 ─────────────────────────────────────────────────

    def start(self) -> None:
        self._tool_schemas = build_voice_tool_schemas(self._registry)
        self._thread = threading.Thread(
            target=self._run_loop, name="voice-volc", daemon=True)
        self._thread.start()

    def send_audio(self, pcm_bytes: bytes) -> None:
        if self._loop is None or not pcm_bytes:
            return
        asyncio.run_coroutine_threadsafe(self._audio_q.put(pcm_bytes), self._loop)

    def send_text(self, message: str) -> None:
        if self._loop is None or not message:
            return
        try:
            data = json.loads(message)
        except (ValueError, TypeError):
            return
        if data.get("type") == "response.cancel":
            asyncio.run_coroutine_threadsafe(
                self._ws_send({"type": "response.cancel"}), self._loop)

    def close(self) -> None:
        self._stop.set()
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._async_close(), self._loop)

    # ── asyncio ──────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._pipeline())
        except Exception:
            logger.exception("[VOICE-VOLC] sid=%s pipeline crashed", self.sid)
            self._call.log_close("failed", error="pipeline crashed")
            self._emit({"type": "realtime.error", "message": "火山实时语音连接异常断开"})
        finally:
            self._loop.close()
            self._loop = None

    async def _pipeline(self) -> None:
        import websockets

        t0 = time.monotonic()
        cfg = load_config()
        self._call = VoiceCallLogger(
            self.sid, "volc", voice=cfg.voice_volc_voice,
            model=VOLC_MODEL, tools_count=len(self._tool_schemas))
        headers = {"X-Api-Key": cfg.voice_volc_api_key}
        logger.info("[VOICE-VOLC] sid=%s connecting tools=%d voice=%s",
                    self.sid, len(self._tool_schemas), cfg.voice_volc_voice)

        self._audio_q = asyncio.Queue(maxsize=200)

        try:
            ctx = websockets.connect(
                VOLC_WSS, additional_headers=headers, max_size=None,
                ping_interval=20, ping_timeout=20, open_timeout=15,
            )
        except Exception as e:
            logger.warning("[VOICE-VOLC] sid=%s connect init failed: %s", self.sid, e)
            self._call.log_close("failed", error=str(e))
            self._emit({"type": "realtime.unavailable",
                        "message": "火山实时语音连接失败，已切换标准语音模式"})
            return

        try:
            async with ctx as ws:
                self._ws = ws
                await self._send_session_create(cfg)

                # 等 session.created 再通知浏览器就绪（同时兼容首包即错误）
                async for raw in ws:
                    evt = self._loads(raw)
                    if evt is None:
                        continue
                    etype = evt.get("type", "")
                    if etype == "session.created":
                        logger.info("[VOICE-VOLC] sid=%s session ready in %.0fms",
                                    self.sid, (time.monotonic() - t0) * 1000)
                        self._call.log_open()
                        self._emit({"type": "realtime.ready"})
                        break
                    if etype == "error":
                        err_text = self._err_text(evt)
                        logger.warning("[VOICE-VOLC] sid=%s server error before ready: %s",
                                       self.sid, err_text)
                        self._call.log_close("downgrade", error=err_text)
                        self._emit({"type": "realtime.unavailable",
                                    "message": f"火山实时语音不可用（{err_text}），已切换标准语音模式"})
                        return
                else:
                    logger.warning("[VOICE-VOLC] sid=%s closed before session.created", self.sid)
                    self._call.log_close("downgrade", error="closed before session.created")
                    self._emit({"type": "realtime.unavailable",
                                "message": "火山实时语音服务未响应，已切换标准语音模式"})
                    return

                # 启动 20ms 上行节奏器
                self._pacer_task = asyncio.create_task(self._audio_pacer())

                await asyncio.gather(
                    self._downstream_pump(ws),
                    self._watch_browser(ws),
                )
                # gather 返回 = 会话结束：区分浏览器挂断与上游断开
                ended_by = "browser-hangup" if self._browser_closed() else "upstream-close"
                logger.info("[VOICE-VOLC] sid=%s session ended by=%s code=%s reason=%r duration=%.1fs",
                            self.sid, ended_by, getattr(ws, "close_code", None),
                            getattr(ws, "close_reason", ""), time.monotonic() - t0)
                self._call.log_close("ok", by=ended_by)
        except Exception as e:
            # 建连/握手/运行期上游异常：明确日志。是否通知前端降级按就绪状态区分，
            # 保持原语义不变：就绪前失败 → 降级 A 档；通话已开始后断开 → 仅提示。
            msg = str(e)
            logger.warning("[VOICE-VOLC] sid=%s upstream failed after %.1fs: %s",
                           self.sid, time.monotonic() - t0, msg[:200])
            if not self._call.ready:
                self._call.log_close("downgrade", error=msg[:200])
                hint = ("鉴权失败，请检查火山 API Key"
                        if ("401" in msg or "403" in msg) else f"连接异常：{msg[:100]}")
                self._emit({"type": "realtime.unavailable",
                            "message": f"火山实时语音{hint}，已切换标准语音模式"})
            else:
                self._call.log_close("failed", error=msg[:200])
                self._emit({"type": "realtime.error",
                            "message": "火山实时语音连接异常断开"})

    async def _send_session_create(self, cfg) -> None:
        session = {
            "model": VOLC_MODEL,
            "instructions": VOICE_INSTRUCTIONS,
            "audio": {
                "input": {"format": {"type": "pcm", "rate": 16000}},
                # 输出必须 pcm_s16le（裸 pcm 在火山是 32 位，前端按 Int16 播会炸）
                "output": {
                    "format": {"type": "pcm_s16le", "rate": 24000},
                    "voice": cfg.voice_volc_voice or VOLC_VOICES[0]["id"],
                },
            },
        }
        tools = _flatten_tools(self._tool_schemas)
        if tools:
            session["tools"] = tools
        await self._ws_send({"type": "session.create", "session": session})

    async def _audio_pacer(self) -> None:
        """把浏览器不规则的上行帧重切成严格 20ms/640B；无数据时发静音保活。"""
        carry = bytearray()
        while not self._stop.is_set():
            # 取走当前队列里所有待发数据
            drained = False
            while not self._audio_q.empty():
                try:
                    carry.extend(self._audio_q.get_nowait())
                    drained = True
                except asyncio.QueueEmpty:
                    break
            # 每个 tick 发且只发一个 640B 帧
            if len(carry) >= FRAME_BYTES:
                frame = bytes(carry[:FRAME_BYTES])
                del carry[:FRAME_BYTES]
            else:
                # 数据不足一帧：麦克风停顿/抖动，补静音（同时承担保活职责）
                frame = SILENCE_FRAME
                if carry:
                    # 零头拼到静音前，避免丢采样造成爆音
                    frame = bytes(carry) + SILENCE_FRAME[len(carry):]
                    carry.clear()
            await self._send_audio_frame(frame)
            await asyncio.sleep(FRAME_INTERVAL)

    async def _send_audio_frame(self, frame: bytes) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(frame).decode("ascii"),
            }))
        except Exception:
            pass

    async def _watch_browser(self, ws) -> None:
        while not self._stop.is_set():
            if self._browser_closed():
                await ws.close()
                return
            await asyncio.sleep(0.3)

    async def _downstream_pump(self, ws) -> None:
        async for raw in ws:
            if self._stop.is_set():
                break
            evt = self._loads(raw)
            if evt is not None:
                await self._handle_event(evt)

    async def _handle_event(self, evt: dict) -> None:
        etype = evt.get("type", "")

        # 音频：解成二进制帧（与千问中继一致，前端无感）
        if etype == "response.output_audio.delta":
            b64 = evt.get("delta", "")
            if b64:
                if self._await_model_since is not None:
                    logger.info(
                        "[VOICE-VOLC] sid=%s first audio after tool in %.0fms",
                        self.sid,
                        (time.monotonic() - self._await_model_since) * 1000)
                    self._await_model_since = None
                try:
                    self._browser_send(base64.b64decode(b64))
                except Exception:
                    pass
            return

        # 轮次聚合（通话卡片数据来源，在归一化之前记录原始事件）
        if etype == "conversation.item.input_audio_transcription.completed":
            utxt = (evt.get("transcript") or "").strip()
            if utxt:
                self._call.log_user(utxt)
        elif etype == "response.output_text.done":
            atxt = (evt.get("text") or "").strip()
            if atxt:
                self._call.log_assistant(atxt)

        # 工具调用（火山一次可能下发 items 数组，支持并行）
        if etype == "response.function_call_arguments.done":
            await self._handle_function_calls(evt)
            return

        # 事件名归一化后透传给浏览器
        normalized = self._normalize(evt)
        if normalized is not None:
            self._emit(normalized)

    def _normalize(self, evt: dict):
        t = evt.get("type", "")
        # 用户开始说话 → 触发前端 barge-in 立即停播
        if t == "conversation.item.input_audio_transcription.started":
            return {"type": "input_audio_buffer.speech_started"}
        # ASR 字幕：delta/completed 火山与千问同名，直接透传
        if t in ("conversation.item.input_audio_transcription.delta",
                 "conversation.item.input_audio_transcription.completed"):
            if t.endswith("completed"):
                txt = (evt.get("transcript") or "").strip()
                if txt:
                    logger.info("[VOICE-VOLC] sid=%s user >> %s", self.sid, txt[:100])
            return evt
        # AI 字幕：output_text.* → audio_transcript.*
        if t == "response.output_text.delta":
            return {"type": "response.audio_transcript.delta",
                    "delta": evt.get("delta", "")}
        if t == "response.output_text.done":
            txt = (evt.get("text") or "").strip()
            if txt:
                logger.info("[VOICE-VOLC] sid=%s assistant << %s", self.sid, txt[:100])
            return {"type": "response.audio_transcript.done",
                    "transcript": evt.get("text", "")}
        # 运行期错误事件：留痕后原样透传
        if t == "error":
            logger.warning("[VOICE-VOLC] sid=%s server error event: %s",
                           self.sid,
                           json.dumps(evt.get("error", evt), ensure_ascii=False)[:200])
            return evt
        # 轮次/会话事件原样透传
        # response.created：模型开始产出，前端据此从"聆听中"切到"思考中"
        if t in ("response.created", "response.done", "session.updated",
                 "input_audio_buffer.committed"):
            return evt
        return None

    async def _handle_function_calls(self, evt: dict) -> None:
        # 火山：{"type":..., "items":[{call_id,name,arguments}, ...]}
        # 兼容单对象形态
        items = evt.get("items")
        if not items:
            single = {k: evt.get(k) for k in ("call_id", "name", "arguments")
                      if evt.get(k) is not None}
            items = [single] if single.get("call_id") else []

        tool_results: list[dict] = []
        for call in items:
            call_id = call.get("call_id", "")
            name = call.get("name", "")
            arguments = call.get("arguments", "{}") or "{}"
            self._emit({"type": "voice.tool_start", "name": name})
            logger.info("[VOICE-VOLC] sid=%s tool START name=%s args=%s",
                        self.sid, name, arguments[:200])
            tool_t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._execute_tool, name, arguments),
                    timeout=TOOL_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                result = (f"工具执行超时（超过 {TOOL_TIMEOUT_SEC} 秒），"
                          f"请换个说法或稍后再试。")
                logger.warning("[VOICE-VOLC] sid=%s tool TIMEOUT name=%s after %.0fs",
                               self.sid, name, TOOL_TIMEOUT_SEC)
            except Exception as e:                       # noqa: BLE001
                result = f"工具执行出错: {e}"
                logger.warning("[VOICE-VOLC] sid=%s tool ERROR name=%s: %s",
                               self.sid, name, e)
            tool_ms = (time.monotonic() - tool_t0) * 1000
            result = result or "（工具返回为空）"
            if len(result) > 6000:
                result = result[:6000] + "\n…（内容过长已截断）"
            logger.info("[VOICE-VOLC] sid=%s tool DONE name=%s %.0fms result_len=%d",
                        self.sid, name, tool_ms, len(result))
            self._call.log_tool(name, arguments, result, tool_ms)
            self._emit({"type": "voice.tool_done", "name": name,
                        "preview": result[:160]})

            # 火山工具结果项：role=tool + content 数组，按 call_id 配对。
            # content.type 必须是 input_text（不是 text）。
            tool_results.append({
                "call_id": call_id,
                "role": "tool",
                "content": [{"type": "input_text", "text": result}],
            })

        # 火山协议要求把所有函数结果聚合到同一个事件里、放进 items 数组
        # （并行调用要一次性回传，全部回传完模型才继续生成）。
        await self._ws_send({
            "type": "conversation.item.create",
            "items": tool_results,
        })
        # 火山收到工具结果后由服务端自动续答；不需要（也不支持）response.create。
        # 从这里开始等模型开口（拿到首个音频分片时会计时打日志）
        self._await_model_since = time.monotonic()
        logger.info("[VOICE-VOLC] sid=%s tool results sent items=%d, waiting for model audio",
                    self.sid, len(tool_results))

    def _execute_tool(self, name: str, arguments: str) -> str:
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
            logger.warning("[VOICE-VOLC] sid=%s tool %s failed: %s", self.sid, name, e)
            return f"工具执行出错: {e}"

    async def _ws_send(self, data: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.debug("[VOICE-VOLC] upstream send failed", exc_info=True)

    async def _async_close(self) -> None:
        if self._pacer_task is not None:
            self._pacer_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    @staticmethod
    def _loads(raw):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _err_text(evt: dict) -> str:
        err = evt.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:200]
        return str(err or "火山语音服务返回错误")[:200]

    def _emit(self, event: dict) -> None:
        try:
            self._browser_send(json.dumps(event, ensure_ascii=False))
        except Exception:
            pass
