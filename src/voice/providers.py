"""语音 Provider：TTS（edge-tts 默认 / 千问实时流式 TTS / OpenAI 兼容云端）
+ STT（浏览器内置 或 OpenAI 兼容云端）。

设计：
- TTS 默认用 edge-tts（微软 Edge 浏览器朗读接口，免费、无需 Key、国内直连、
  音质自然），项目 requirements 中已有依赖。
- qwen 通道走千问平台 Realtime TTS WebSocket（qwen3-tts-flash-realtime），
  Key 复用 B 档 VOICE_REALTIME_API_KEY，返回 24k PCM，服务端封装 WAV。
- 云端 STT/TTS 走 OpenAI 兼容音频协议（/audio/transcriptions、/audio/speech），
  配置独立于文本 LLM（中转站通常只转发文本模型，音频需要单独的语音服务，
  如硅基流动 SenseVoice / CosyVoice）。
- STT 未配置云端时由前端回退浏览器内置语音识别。

所有函数都是同步的（HTTP 服务器每请求一线程，内部用临时 event loop 跑
async 接口）。
"""

import asyncio
import base64
import io
import json
import logging
import threading
import wave

import requests

from src.config import load_config

logger = logging.getLogger(__name__)

# 千问 Realtime（B 档对话 + A 档 qwen TTS 共用）
_QWEN_REALTIME_WSS = "wss://maas.qianwenaiapi.com/api-ws/v1/realtime"

# ── 音色列表（前端可选）────────────────────────────────────────────
EDGE_VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓 · 女声（自然亲切，默认）"},
    {"id": "zh-CN-YunxiNeural", "name": "云希 · 男声（沉稳阳光）"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊 · 女声（活泼甜美）"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬 · 男声（专业新闻）"},
    {"id": "zh-CN-XiaoxuanNeural", "name": "晓萱 · 女声（成熟知性）"},
    {"id": "zh-CN-YunjianNeural", "name": "云健 · 男声（浑厚磁性）"},
]

DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"

# edge-tts 在合成失败/超时时最多重试的次数（网络抖动时首句失败很影响体验）
_MAX_TTS_RETRIES = 2
_TTS_TIMEOUT_SEC = 20
_STT_TIMEOUT_SEC = 30

# edge-tts 并发合成上限：前端会按句预取（一句还没播完就开始合下一句），
# 所以不能再串行化，否则预取等于没有；但也不能无上限，避免同时打爆端点。
_MAX_TTS_CONCURRENCY = 3
_tts_gate = threading.BoundedSemaphore(_MAX_TTS_CONCURRENCY)


class VoiceProviderError(RuntimeError):
    """语音服务错误（消息可直接展示给用户）。"""


# ── edge-tts ────────────────────────────────────────────────────────

async def _edge_synthesize(text: str, voice: str, rate: str) -> bytes:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    chunks = []
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            chunks.append(chunk["data"])
    return b"".join(chunks)


def _run_async(coro) -> bytes:
    """在任意线程中运行一次 async 协程（每次新建/关闭 event loop）。"""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


def synthesize(text: str, voice: str = "", rate: str = "+0%") -> tuple[bytes, str]:
    """文本转语音，返回 (音频字节, content_type)。

    根据配置选择 edge / OpenAI 兼容 provider。
    """
    text = (text or "").strip()
    if not text:
        raise VoiceProviderError("合成内容为空")
    if len(text) > 1000:
        text = text[:1000]

    cfg = load_config()
    provider = (cfg.voice_tts_provider or "edge").lower()

    if provider == "openai":
        try:
            return _openai_synthesize(text, cfg), "audio/mpeg"
        except VoiceProviderError:
            raise
        except Exception as e:
            logger.warning("[VOICE] OpenAI TTS 失败，回退 edge-tts: %s", e)
            # 云端故障时降级免费通道，保证通话不中断
            return _edge_synthesize_safe(text, voice or cfg.voice_tts_voice, rate)

    if provider == "qwen":
        try:
            return _qwen_tts_synthesize(text, cfg), "audio/wav"
        except VoiceProviderError:
            raise
        except Exception as e:
            logger.warning("[VOICE] Qwen TTS 失败，回退 edge-tts: %s", e)
            return _edge_synthesize_safe(text, voice or cfg.voice_tts_voice, rate)

    return _edge_synthesize_safe(text, voice or cfg.voice_tts_voice, rate)


def _edge_synthesize_safe(text: str, voice: str, rate: str) -> tuple[bytes, str]:
    voice = voice or DEFAULT_EDGE_VOICE
    last_err = None
    # edge-tts 偶发连接重置；逐次重试。并发数由 _tts_gate 限制（不是串行）。
    for attempt in range(1, _MAX_TTS_RETRIES + 2):
        try:
            with _tts_gate:
                audio = _run_async(
                    asyncio.wait_for(
                        _edge_synthesize(text, voice, rate or "+0%"),
                        timeout=_TTS_TIMEOUT_SEC,
                    )
                )
            if audio:
                return audio, "audio/mpeg"
            last_err = "empty audio"
        except Exception as e:
            last_err = e
            logger.warning("[VOICE] edge-tts attempt %d failed: %s", attempt, e)
    raise VoiceProviderError(f"语音合成失败，请稍后重试（{last_err}）")


# ── OpenAI 兼容 TTS ─────────────────────────────────────────────────

def _openai_synthesize(text: str, cfg) -> bytes:
    base = (cfg.voice_tts_base_url or "").rstrip("/")
    if not base or not cfg.voice_tts_api_key:
        raise VoiceProviderError("未配置云端语音合成（VOICE_TTS_BASE_URL / KEY）")
    url = base + "/audio/speech"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {cfg.voice_tts_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg.voice_tts_model or "tts-1",
            "voice": _voice_to_openai(cfg.voice_tts_voice),
            "input": text,
            "response_format": "mp3",
        },
        timeout=_TTS_TIMEOUT_SEC,
    )
    if resp.status_code != 200:
        raise VoiceProviderError(f"云端 TTS 返回 {resp.status_code}")
    if "audio" not in resp.headers.get("Content-Type", ""):
        raise VoiceProviderError("云端 TTS 返回非音频数据")
    return resp.content


def _voice_to_openai(edge_voice: str) -> str:
    """edge 音色名到 OpenAI 标准 voice 的宽松映射（云端 provider 不认 edge 名）。"""
    mapping = {
        "zh-CN-YunxiNeural": "alloy",
        "zh-CN-YunyangNeural": "echo",
        "zh-CN-YunjianNeural": "onyx",
    }
    return mapping.get(edge_voice, "nova")


# ── 千问 Realtime 流式 TTS（A 档可选，复用 B 档 Key）────────────────

async def _qwen_tts_async(text: str, api_key: str, model: str, voice: str) -> bytes:
    """走 Realtime TTS WebSocket 合成一句，返回 24k/16bit 单声道 PCM。"""
    import websockets

    url = f"{_QWEN_REALTIME_WSS}?model={model}"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with websockets.connect(url, additional_headers=headers,
                                  open_timeout=_TTS_TIMEOUT_SEC,
                                  max_size=None) as ws:
        await ws.recv()  # session.created
        await ws.send(json.dumps({"type": "session.update", "session": {
            "voice": voice or "Cherry",
            "response_format": "pcm",
            "sample_rate": 24000,
            "mode": "commit",
        }}))
        # 等 session.updated（部分模型不回，超时忽略）
        try:
            while True:
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if evt.get("type") in ("session.updated", "error"):
                    break
        except asyncio.TimeoutError:
            pass

        await ws.send(json.dumps({
            "type": "input_text_buffer.append", "text": text,
        }))
        await ws.send(json.dumps({"type": "input_text_buffer.commit"}))

        chunks = []
        async for raw in ws:
            evt = json.loads(raw)
            etype = evt.get("type")
            if etype == "response.audio.delta":
                chunks.append(base64.b64decode(evt.get("delta", "")))
            elif etype == "response.audio.done":
                break
            elif etype == "error":
                msg = json.dumps(evt.get("error", {}), ensure_ascii=False)[:200]
                raise VoiceProviderError(f"千问 TTS 错误: {msg}")
        return b"".join(chunks)


def _pcm_to_wav(pcm: bytes, sample_rate: int = 24000) -> bytes:
    """单声道 16bit PCM → WAV 容器（浏览器 Audio 元素可直接播放）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _qwen_tts_synthesize(text: str, cfg) -> bytes:
    api_key = (cfg.voice_realtime_api_key or "").strip()
    if not api_key:
        raise VoiceProviderError("未配置千问 API Key（与实时全双工共用一个 Key）")
    model = cfg.voice_tts_qwen_model or "qwen3-tts-flash-realtime"
    voice = cfg.voice_tts_qwen_voice or "Cherry"
    pcm = _run_async(asyncio.wait_for(
        _qwen_tts_async(text, api_key, model, voice),
        timeout=_TTS_TIMEOUT_SEC,
    ))
    if not pcm:
        raise VoiceProviderError("千问 TTS 返回空音频")
    return _pcm_to_wav(pcm, 24000)


# ── OpenAI 兼容 STT ─────────────────────────────────────────────────

def transcribe(audio_bytes: bytes, content_type: str = "audio/wav",
               filename: str = "audio.wav", language: str = "zh") -> dict:
    """语音转文字（OpenAI 兼容 /audio/transcriptions）。

    返回 {"ok": True, "text": ...} 或 {"ok": False, "error": ...}。
    """
    cfg = load_config()
    base = (cfg.voice_stt_base_url or "").rstrip("/")
    if not base or not cfg.voice_stt_api_key:
        return {"ok": False, "error": "not_configured"}

    url = base + "/audio/transcriptions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {cfg.voice_stt_api_key}"},
            files={"file": (filename, audio_bytes, content_type or "audio/wav")},
            data={"model": cfg.voice_stt_model or "whisper-1",
                  "language": language or "zh"},
            timeout=_STT_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        logger.warning("[VOICE] STT request failed: %s", e)
        return {"ok": False, "error": f"语音识别服务连接失败: {e}"}

    if resp.status_code != 200:
        logger.warning("[VOICE] STT HTTP %d: %s",
                       resp.status_code, resp.text[:200])
        return {"ok": False, "error": f"语音识别服务返回 {resp.status_code}"}

    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": "语音识别服务返回格式异常"}

    text = (data.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "没有识别到语音内容"}
    return {"ok": True, "text": text}


# ── 配置状态（给 /api/voice/config）─────────────────────────────────

def voice_status() -> dict:
    """返回语音能力状态，供前端决定走哪条通道。"""
    cfg = load_config()
    try:
        import edge_tts  # noqa: F401
        edge_ok = True
    except Exception:
        edge_ok = False

    # STT：browser 永远可用（取决于浏览器）；openai 需配置完整
    stt_provider = (cfg.voice_stt_provider or "browser").lower()
    if stt_provider == "openai" and not (cfg.voice_stt_base_url
                                         and cfg.voice_stt_api_key):
        stt_provider = "browser"

    # TTS 实际可用性归一
    tts_provider = (cfg.voice_tts_provider or "edge").lower()
    if tts_provider == "openai" and not (cfg.voice_tts_base_url
                                         and cfg.voice_tts_api_key):
        tts_provider = "edge"
    if tts_provider == "qwen" and not cfg.voice_realtime_api_key:
        tts_provider = "edge"
    if tts_provider == "edge" and not edge_ok:
        tts_provider = "unavailable"

    standard_enabled = bool(getattr(cfg, "voice_standard_enabled", True))
    return {
        "ok": True,
        "standard_enabled": standard_enabled,
        "stt_provider": stt_provider,                       # browser | openai
        "stt_cloud": stt_provider == "openai",             # 兼容旧前端判断
        "stt_model": cfg.voice_stt_model,
        "tts_provider": tts_provider,                      # edge | qwen | openai
        "tts_voice": cfg.voice_tts_voice or DEFAULT_EDGE_VOICE,
        "tts_qwen_model": cfg.voice_tts_qwen_model,
        "tts_qwen_voice": cfg.voice_tts_qwen_voice,
        "voices": EDGE_VOICES,
    }
