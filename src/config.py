"""Configuration loading from .env file."""

import logging
import msvcrt
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def _decode_wechat_groups(raw: str) -> str:
    """Decode URL-encoded group names from .env WECHAT_GROUPS value.

    We store each group name URL-encoded (via encodeURIComponent / urllib.parse.quote)
    so that commas, equals signs, and newlines in real group names don't break the
    .env format or our comma-separated delimiter.  This function reverses that encoding
    with a fallback: if decoding a chunk doesn't change it (or raises), the original
    is kept — for backward compatibility with old unencoded .env files.
    """
    if not raw or raw.strip() == "*":
        return raw.strip() if raw else "*"
    decoded = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            d = unquote(chunk)
            decoded.append(d)
        except Exception:
            decoded.append(chunk)
    return ",".join(decoded) if decoded else "*"

def _resolve_project_root() -> Path:
    app_home = os.getenv("WEBOT_APP_HOME", "").strip()
    if app_home:
        return Path(app_home).expanduser().resolve()

    # In a PyInstaller EXE, __file__ resolves inside the temp extraction dir.
    # We want writable data (.env, data/*) in the EXE directory, not in the temp dir.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _resolve_project_root()


def write_env_atomic(env_path: Path, updates: dict[str, str]) -> None:
    """Thread- and process-safe atomic update of .env key=value pairs.

    Uses a file lock (.env.lock) via msvcrt (Windows) to prevent concurrent
    writes from overwriting each other.  Reads the existing file, applies
    the updates dict, and writes back atomically via os.replace().

    Args:
        env_path: Path to the .env file.
        updates: Dict of key->value pairs to set or add.
    """
    lock_path = env_path.with_suffix(".lock")

    # Create the .env file if it doesn't exist
    if not env_path.exists():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("", encoding="utf-8")

    lock_fd = open(lock_path, "w")
    try:
        # Acquire exclusive lock (blocking with short retry)
        for _ in range(10):
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                import time
                time.sleep(0.05)
        else:
            # Could not acquire lock after 10 tries — proceed anyway
            # (better to risk a rare race than to hang forever)
            logger.warning("Could not acquire .env.lock after 10 attempts, proceeding without lock")

        # Read existing content
        lines = []
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            # 读失败继续写会用空内容覆盖原文件，丢掉已有配置。
            # 中止保存，保留原文件，记录原因（调用方忽略返回值也不受影响）。
            logger.warning("write_env_atomic: cannot read %s, aborting save to avoid data loss: %s",
                           env_path, e)
            return

        # Apply updates
        updated_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            updated = False
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in updates:
                    new_lines.append(f"{k}={updates[k]}")
                    updated_keys.add(k)
                    updated = True
            if not updated:
                new_lines.append(line)

        for k, v in updates.items():
            if k not in updated_keys:
                new_lines.append(f"{k}={v}")

        # Atomic write via temp file
        tmp = env_path.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        os.replace(tmp, env_path)

    finally:
        try:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        lock_fd.close()


def find_env_file() -> Path | None:
    """Find the .env file using a consistent search order.

    Order: WEBOT_ENV_FILE override → EXE directory (frozen) → project root → current working directory.
    Returns the Path if found, or None if no .env exists anywhere.
    """
    explicit_env = os.getenv("WEBOT_ENV_FILE", "").strip()
    if explicit_env:
        explicit_path = Path(explicit_env).expanduser()
        if explicit_path.exists():
            return explicit_path

    locations = [
        PROJECT_ROOT / ".env",
        Path.cwd() / ".env",
    ]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        locations.insert(0, exe_dir / ".env")

    for loc in locations:
        if loc.exists():
            return loc

    # ── Auto-create .env from .env.example if missing ──────────────
    # Pick the first writable target location (same order as locations)
    target = locations[0] if locations else PROJECT_ROOT / ".env"

    # Find .env.example source
    example = None
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        example = Path(sys._MEIPASS) / ".env.example"
    if not example or not example.exists():
        example = PROJECT_ROOT / ".env.example"
    if not example.exists():
        example = Path.cwd() / ".env.example"

    if example.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(example, target)
            logger.info(".env not found, auto-created from %s → %s", example, target)
            return target
        except Exception as exc:
            logger.warning("Failed to auto-create .env from %s: %s", example, exc)

    return None


_env_path = find_env_file()

if _env_path:
    load_dotenv(_env_path)
else:
    load_dotenv()

# Log which .env was loaded (helpful for debugging EXE packaging issues)
if _env_path:
    logger.info("Loaded .env from: %s", _env_path)
else:
    _search_locations = [
        PROJECT_ROOT / ".env",
        Path.cwd() / ".env",
    ]
    if getattr(sys, "frozen", False):
        _search_locations.insert(0, Path(sys.executable).resolve().parent / ".env")
    logger.warning(
        ".env not found in any search path (%s). Using defaults.",
        ", ".join(str(p) for p in _search_locations),
    )


@dataclass
class BotConfig:
    """All configuration for the WeChat summarizer bot."""

    # === AI Provider (unified config) ===
    # Base URL of the AI provider (e.g. https://api.deepseek.com).
    ai_provider_base_url: str = ""
    ai_provider_api_key: str = ""
    # "auto" | "anthropic" | "openai" | "custom"
    ai_provider_type: str = "openai"
    ai_provider_model: str = ""
    # JSON string for extra_body, e.g. '{"thinking":{"type":"disabled"}}'
    ai_provider_extra_body: str = ""

    # === Voice 标准模式（A 档：STT + LLM + TTS 拼装，全双工不可用时的降级/省钱通道）===
    # A 档总开关：与 B 档独立。两者都开时 B 优先，B 连接失败自动降级 A。
    voice_standard_enabled: bool = True
    # 识别通道：browser（浏览器内置识别，免费，Edge 国内可用）| openai（云端 /audio/transcriptions）
    voice_stt_provider: str = "browser"
    # 云端语音识别（OpenAI 兼容 /audio/transcriptions），stt_provider=openai 时生效。
    voice_stt_base_url: str = ""
    voice_stt_api_key: str = ""
    voice_stt_model: str = "whisper-1"
    # 合成通道：edge（免费，默认）| qwen（千问实时流式 TTS，复用 B 档 Key）| openai（/audio/speech）
    voice_tts_provider: str = "edge"
    voice_tts_base_url: str = ""
    voice_tts_api_key: str = ""
    voice_tts_model: str = "tts-1"
    # edge-tts 音色，如 zh-CN-XiaoxiaoNeural / zh-CN-YunxiNeural
    voice_tts_voice: str = "zh-CN-XiaoxiaoNeural"
    # 千问流式 TTS（A 档 tts_provider=qwen 时使用，WebSocket，Key 复用 VOICE_REALTIME_API_KEY）
    voice_tts_qwen_model: str = "qwen3-tts-flash-realtime"
    voice_tts_qwen_voice: str = "Cherry"

    # === Voice Realtime（B 档：端到端全双工通话）===
    # 语音助手总开关：关闭后网页不再显示麦克风入口（与 A/B 通道开关独立）。
    voice_assistant_enabled: bool = True
    # B 档服务商：qwen（千问 Realtime）| volc（火山豆包 Seeduplex 3.0 全双工）
    voice_rt_provider: str = "qwen"
    # ── 千问 ──
    # B 档：填了 Key 并开启走端到端全双工（主通道）；未开启或连不上降级 A 档。
    # 平台：https://platform.qianwenai.com  WebSocket 地址在代码内固定。
    voice_realtime_enabled: bool = False
    voice_realtime_api_key: str = ""
    voice_realtime_model: str = "qwen3.8-omni-flash-realtime"
    # 音色随模型家族不同：omni 系列用 Tina/Cindy 等，qwen-audio 系列用 longanqian_v3.1 等
    voice_realtime_voice: str = "Tina"
    # server_vad（声学停顿断句）| smart_turn（语义断句，嗯/啊不打断，推荐）
    voice_realtime_turn_mode: str = "smart_turn"
    # ── 火山豆包 Seeduplex 3.0（model 固定 1.2.6.1，X-Api-Key 鉴权）──
    voice_volc_enabled: bool = False
    voice_volc_api_key: str = ""
    # 主打音色：Vivi2.0/小何2.0/云舟2.0/小天2.0
    voice_volc_voice: str = "zh_female_vv_uranus_bigtts"

    # === WeChat Backend ===
    wechat_backend: str = "wcdb"
    # Custom WeChat data directory. Leave empty to auto-detect from Documents.
    # Set to the parent directory containing wxid_* folders (e.g. D:\WeChatData).
    wechat_data_dir: str = ""

    # === Bot Identity ===
    # Admin wxid (can manage nicknames and bot settings)
    admin_wxid: str = ""

    # === Database ===
    db_path: str = "data/messages.db"

    # === Restricted Features ===
    # Master switch for sensitive features: message anti-revoke, SNS delete protection.
    # When false, these features are completely disabled and hidden from the UI.
    enable_restricted_features: bool = False

    # === Memory Consolidation ===
    # When enabled, automatically consolidates group chat messages into a
    # "memory diary" via AI (every 50 msgs or 1 hour).  This memory is then
    # injected into @mention chat and proactive chat prompts as context.
    memory_consolidation_enabled: bool = False

    # === Tuning ===
    poll_interval_sec: float = 1.0
    chunk_size: int = 400
    agent_max_steps: int = 8

    # === Logging ===
    log_level: str = "INFO"
    log_file: str = "data/bot.log"


def _validate_config(kwargs: dict) -> None:
    """Validate numeric config values.  Prints clear errors and exits on bad values."""
    errors: list[str] = []

    # poll_interval_sec
    poll_interval_sec = kwargs.get("poll_interval_sec", 1.0)
    if poll_interval_sec < 0.1:
        errors.append(
            f"POLL_INTERVAL_SEC must be >= 0.1, got {poll_interval_sec}"
        )

    # chunk_size
    chunk_size = kwargs.get("chunk_size", 400)
    if not (10 <= chunk_size <= 1000):
        errors.append(
            f"CHUNK_SIZE must be between 10 and 1000, got {chunk_size}"
        )

    # max_retries (if present in config)
    max_retries = kwargs.get("max_retries")
    if max_retries is not None:
        if not (1 <= max_retries <= 10):
            errors.append(
                f"MAX_RETRIES must be between 1 and 10, got {max_retries}"
            )

    if errors:
        msg = "配置值无效:\n" + "\n".join(f"  - {err}" for err in errors)
        raise RuntimeError(msg)


def load_config() -> BotConfig:
    """Load configuration from environment variables.

    Returns a validated BotConfig instance.
    Raises RuntimeError if required configuration is missing.
    """
    # Validate required AI provider config.
    # If no AI config is present, allow startup without keys — the
    # user can configure AI later via the dashboard.
    ai_provider_base_url = os.getenv("AI_PROVIDER_BASE_URL", "").strip()
    ai_provider_api_key = os.getenv("AI_PROVIDER_API_KEY", "").strip()
    # No strict validation here — AI features will simply be unavailable
    # until the user configures AI_PROVIDER_* via the dashboard.

    kwargs: dict = {
        "wechat_backend": os.getenv("WECHAT_BACKEND", "wcdb").strip(),
        "wechat_data_dir": os.getenv("WECHAT_DATA_DIR", "").strip(),
        "admin_wxid": os.getenv("ADMIN_WXID", "").strip(),
        "db_path": os.getenv("LOCAL_DB_PATH", "data/messages.db").strip(),
        "poll_interval_sec": float(os.getenv("POLL_INTERVAL_SEC", "1.0")),
        "chunk_size": int(os.getenv("CHUNK_SIZE", "400")),
        "agent_max_steps": int((os.getenv("AGENT_MAX_STEPS") or "").strip() or "8"),
        "enable_restricted_features": os.getenv("ENABLE_RESTRICTED_FEATURES", "false").strip().lower() == "true",
        "memory_consolidation_enabled": os.getenv("MEMORY_CONSOLIDATION_ENABLED", "false").strip().lower() == "true",
        "log_level": os.getenv("LOG_LEVEL", "INFO").strip(),
        "log_file": os.getenv("LOG_FILE", "data/bot.log").strip(),
        # AI Provider unified config
        "ai_provider_base_url": ai_provider_base_url,
        "ai_provider_api_key": ai_provider_api_key,
        "ai_provider_type": os.getenv("AI_PROVIDER_TYPE", "openai").strip(),
        "ai_provider_model": os.getenv("AI_PROVIDER_MODEL", "").strip(),
        "ai_provider_extra_body": os.getenv("AI_PROVIDER_EXTRA_BODY", "").strip(),
        # Voice 标准模式（A 档）
        "voice_standard_enabled": os.getenv("VOICE_STANDARD_ENABLED", "true").strip().lower() != "false",
        "voice_stt_provider": (os.getenv("VOICE_STT_PROVIDER", "browser").strip().lower()
                               or "browser"),
        "voice_stt_base_url": os.getenv("VOICE_STT_BASE_URL", "").strip(),
        "voice_stt_api_key": os.getenv("VOICE_STT_API_KEY", "").strip(),
        "voice_stt_model": os.getenv("VOICE_STT_MODEL", "whisper-1").strip() or "whisper-1",
        "voice_tts_provider": (os.getenv("VOICE_TTS_PROVIDER", "edge").strip().lower()
                               or "edge"),
        "voice_tts_base_url": os.getenv("VOICE_TTS_BASE_URL", "").strip(),
        "voice_tts_api_key": os.getenv("VOICE_TTS_API_KEY", "").strip(),
        "voice_tts_model": os.getenv("VOICE_TTS_MODEL", "tts-1").strip() or "tts-1",
        "voice_tts_voice": (os.getenv("VOICE_TTS_VOICE", "zh-CN-XiaoxiaoNeural").strip()
                            or "zh-CN-XiaoxiaoNeural"),
        "voice_tts_qwen_model": (os.getenv("VOICE_TTS_QWEN_MODEL",
                                           "qwen3-tts-flash-realtime").strip()
                                 or "qwen3-tts-flash-realtime"),
        "voice_tts_qwen_voice": (os.getenv("VOICE_TTS_QWEN_VOICE", "Cherry").strip()
                                 or "Cherry"),
        # Voice Realtime（B 档）
        "voice_assistant_enabled": os.getenv("VOICE_ENABLED", "true").strip().lower() != "false",
        "voice_realtime_enabled": os.getenv("VOICE_REALTIME_ENABLED", "false").strip().lower() == "true",
        "voice_realtime_api_key": os.getenv("VOICE_REALTIME_API_KEY", "").strip(),
        "voice_realtime_model": (os.getenv("VOICE_REALTIME_MODEL",
                                           "qwen3.8-omni-flash-realtime").strip()
                                 or "qwen3.8-omni-flash-realtime"),
        "voice_realtime_voice": (os.getenv("VOICE_REALTIME_VOICE", "Tina").strip()
                                 or "Tina"),
        "voice_realtime_turn_mode": (os.getenv("VOICE_REALTIME_TURN_MODE",
                                               "smart_turn").strip().lower()
                                     or "smart_turn"),
        # 火山 Seeduplex
        "voice_rt_provider": (os.getenv("VOICE_RT_PROVIDER", "qwen").strip().lower()
                              or "qwen"),
        "voice_volc_enabled": os.getenv("VOICE_VOLC_ENABLED", "false").strip().lower() == "true",
        "voice_volc_api_key": os.getenv("VOICE_VOLC_API_KEY", "").strip(),
        "voice_volc_voice": (os.getenv("VOICE_VOLC_VOICE",
                                       "zh_female_vv_uranus_bigtts").strip()
                             or "zh_female_vv_uranus_bigtts"),
    }

    _validate_config(kwargs)

    return BotConfig(**kwargs)


def is_onboarding_done() -> bool:
    """Check if onboarding has been completed without loading full config.

    Uses find_env_file() for consistent .env resolution.
    """
    env_path = find_env_file()
    if env_path and env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ONBOARDING_DONE="):
                return line.split("=", 1)[1].strip().lower() == "true"
        return False  # .env exists but no ONBOARDING_DONE key
    return False  # No .env found
