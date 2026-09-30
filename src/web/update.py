"""GitHub Release 检查与自动更新。

设计要点：
  - 检查结果缓存 30 分钟，并落盘到 data/update_cache.json；断网时也能看到
    上次的结果，不会因为 GitHub 抽风就让整个页面报错。
  - 只查 releases/latest，不遍历历史版本。
  - Release 正文（Markdown）在这里解析成结构化数据，前端用 React 元素渲染，
    既不引入 markdown 依赖，也不用 innerHTML（没有 XSS 面）。
  - 下载先写 .part 再改名，中途失败不会留下半个 exe 冒充完整包。
  - 就地替换走 helper 批处理：等本进程退出 → 换文件 → 重启 → 自删。
    换文件失败会回滚，保证最坏情况只是"更新失败，旧版还能用"。
"""
import html as _html
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as _ET
from pathlib import Path

import requests

from src.version import (
    ASSET_FULL,
    ASSET_LITE,
    GITHUB_REPO,
    __version__,
    is_newer,
    release_page_url,
)

logger = logging.getLogger(__name__)

_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_ATOM_FEED = f"https://github.com/{GITHUB_REPO}/releases.atom"
_USER_AGENT = f"wx-assist/{__version__}"
_TIMEOUT = (8, 25)       # (连接超时, 读取超时)
_CACHE_TTL = 30 * 60     # 检查结果缓存时长（秒）
_CHUNK_SIZE = 256 * 1024  # 下载分片大小

# Windows 进程创建标志
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


# ── 路径 ──────────────────────────────────────────────────────────────

def _project_root() -> Path:
    """程序根目录：打包后 = exe 所在目录，源码模式 = 仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def _updates_dir() -> Path:
    path = _project_root() / "data" / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_file() -> Path:
    return _project_root() / "data" / "update_cache.json"


# ── .env 读取 ─────────────────────────────────────────────────────────

def _read_env_key(key: str, default: str = "") -> str:
    """从 .env 直接读一个配置项（现读文件，保证改完立即生效）。"""
    try:
        from src.config import find_env_file
        env_path = find_env_file()
        if env_path and Path(env_path).exists():
            for line in Path(env_path).read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                name, value = stripped.split("=", 1)
                if name.strip() == key:
                    return value.strip()
    except Exception as exc:
        logger.debug("读取 .env 键 %s 失败: %s", key, exc)
    return default


# ── 构建变体 ──────────────────────────────────────────────────────────

def build_variant() -> str:
    """当前跑的是完整版还是精简版 —— 决定更新时该下载哪个资产。

    判据与 server.py 的 RAG 可用性一致：build.spec 打包时排除了
    chromadb / fastembed / onnxruntime，build_rag.spec 没有排除。
    不区分变体的话，精简版用户会被"更新"成 255MB 的完整版。
    """
    for name in ("src.assistant.rag", "fastembed", "chromadb", "onnxruntime"):
        if importlib.util.find_spec(name) is None:
            return "lite"
    return "full"


def _can_auto_install() -> tuple:
    """能否就地替换当前 exe。返回 (是否可以, 不能的原因)。"""
    if not getattr(sys, "frozen", False):
        return False, "源码模式不支持自动更新，请用 git pull 拉取新版本"
    exe_dir = Path(sys.executable).resolve().parent
    probe = exe_dir / f".wxassist_write_test_{os.getpid()}"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except Exception:
        return False, "程序所在目录没有写入权限，请手动下载更新包"
    return True, ""


# ── Release 正文解析（Markdown → 结构化块）────────────────────────────

_INLINE_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_INLINE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_FENCE = re.compile(r"^\s*```")


def _clean_inline(text: str) -> str:
    """去掉行内的 Markdown 标记，只留可读文本。

    链接保留成 '文本（地址）' 形式，前端不需要再解析。
    """
    text = _INLINE_LINK.sub(lambda m: m.group(1) or m.group(2), text)
    text = _INLINE_BOLD.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    return text.strip()


def parse_release_notes(markdown: str) -> list:
    """把 Release 正文解析成 [{type, text}] 列表。

    只处理更新日志里常见的几种写法：标题、列表、代码块、分隔线、段落。
    刻意保持简单 —— 解析不了的行一律当普通段落，不会丢内容。
    """
    blocks = []
    if not markdown:
        return blocks

    in_code = False
    code_lines = []

    for raw_line in markdown.replace("\r\n", "\n").split("\n"):
        stripped = raw_line.strip()

        # 代码块开关
        if _FENCE.match(raw_line):
            if in_code:
                blocks.append({"type": "code", "text": "\n".join(code_lines)})
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(raw_line.rstrip())
            continue

        if not stripped:
            continue

        # 分隔线
        if re.fullmatch(r"[-*_]{3,}", stripped):
            blocks.append({"type": "hr", "text": ""})
            continue

        # 标题
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = min(len(heading.group(1)), 4)
            blocks.append({"type": f"h{level}", "text": _clean_inline(heading.group(2))})
            continue

        # 列表（-、*、+）；记录缩进层级，前端可以做出层级感
        item = re.match(r"^([-*+]|\d+\.)\s+(.*)$", stripped)
        if item:
            indent = len(raw_line) - len(raw_line.lstrip())
            blocks.append({
                "type": "li",
                "text": _clean_inline(item.group(2)),
                "indent": 1 if indent >= 2 else 0,
            })
            continue

        # 引用
        if stripped.startswith(">"):
            blocks.append({"type": "quote", "text": _clean_inline(stripped.lstrip("> "))})
            continue

        blocks.append({"type": "p", "text": _clean_inline(stripped)})

    if in_code and code_lines:
        blocks.append({"type": "code", "text": "\n".join(code_lines)})

    return blocks


# ── 检查更新 ──────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_cache = {"ts": 0.0, "data": None}


def _load_disk_cache() -> dict:
    path = _cache_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_disk_cache(payload: dict) -> None:
    try:
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("写更新缓存失败: %s", exc)


def _auth_headers() -> dict:
    """GitHub API 请求头。

    未登录时限额 60 次/小时，配合 30 分钟缓存完全够用；
    如果你在 .env 里配了 GITHUB_TOKEN 就用上，额度更高。
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    token = _read_env_key("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _pick_asset(assets: list, variant: str):
    """按构建变体挑对应的 exe，避免精简版用户被更新成完整版。"""
    wanted = ASSET_FULL if variant == "full" else ASSET_LITE
    for asset in assets:
        if asset.get("name") == wanted:
            return asset
    # 兜底：老版本里精简版用过连字符命名（wx-assist-no-rag.exe）
    for asset in assets:
        name = str(asset.get("name") or "")
        lowered = name.lower()
        if not lowered.endswith(".exe"):
            continue
        is_lite = "no_rag" in lowered or "no-rag" in lowered
        if is_lite == (variant == "lite"):
            return asset
    return None


def _github_error_message(response) -> str:
    """尽量把 GitHub 返回的真实原因带出来，别只说"失败了"。"""
    message = ""
    try:
        message = str((response.json() or {}).get("message") or "").strip()
    except Exception:
        message = ""
    if not message:
        message = (response.text or "").strip().replace("\n", " ")[:120]
    return f"GitHub 返回 {response.status_code}：{message}" if message else f"GitHub 返回 {response.status_code}"


def _fetch_release_via_api() -> dict:
    """走 GitHub API 拿最新 Release（信息最全：带资产列表和文件大小）。"""
    response = requests.get(_API_LATEST, headers=_auth_headers(), timeout=_TIMEOUT)
    if response.status_code == 404:
        raise RuntimeError(f"仓库 {GITHUB_REPO} 还没有发布任何版本")
    if response.status_code >= 400:
        raise RuntimeError(_github_error_message(response))
    data = response.json()
    return {
        "tag_name": str(data.get("tag_name") or ""),
        "name": data.get("name") or data.get("tag_name") or "",
        "body": data.get("body") or "",
        "published_at": data.get("published_at") or "",
        "html_url": data.get("html_url") or "",
        "assets": data.get("assets") or [],
        "source": "api",
    }


# Atom 订阅源里的正文是 HTML，先转回 Markdown 行，复用同一套解析器。
_HTML_BLOCK_RULES = (
    (re.compile(r"(?is)<br\s*/?>"), "\n"),
    (re.compile(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>"),
     lambda m: "\n" + "#" * int(m.group(1)) + " " + m.group(2) + "\n"),
    (re.compile(r"(?is)<li[^>]*>(.*?)</li>"), lambda m: "\n- " + m.group(1) + "\n"),
    (re.compile(r"(?is)</?(ul|ol)[^>]*>"), "\n"),
    (re.compile(r"(?is)<p[^>]*>(.*?)</p>"), lambda m: "\n" + m.group(1) + "\n"),
    (re.compile(r"(?is)<pre[^>]*>(.*?)</pre>"), lambda m: "\n```\n" + m.group(1) + "\n```\n"),
    (re.compile(r"(?is)<hr\s*/?>"), "\n---\n"),
)


def _html_to_markdown(raw_html: str) -> str:
    """把 Release 正文的 HTML 压成 Markdown 行。"""
    text = raw_html or ""
    for pattern, replacement in _HTML_BLOCK_RULES:
        text = pattern.sub(replacement, text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    return _html.unescape(text)


def _fetch_release_via_atom() -> dict:
    """走 releases.atom 订阅源兜底。

    GitHub API 对匿名访问限 60 次/小时，而共享代理出口 IP 很容易把它用光
    （实测就撞上过 ratelimit-remaining = 0）。Atom 订阅源不计入该配额，
    版本号、发布时间、更新日志都能拿到，唯独没有资产列表 —— 下载地址按
    资产命名规则拼即可，所以照样能下载。
    """
    response = requests.get(_ATOM_FEED, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(_github_error_message(response))

    root = _ET.fromstring(response.content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", ns)
    if entry is None:
        raise RuntimeError(f"仓库 {GITHUB_REPO} 还没有发布任何版本")

    link = entry.find("atom:link", ns)
    html_url = (link.get("href") if link is not None else "") or ""

    # tag 只在链接里：https://github.com/<repo>/releases/tag/v1.6.0
    tag = ""
    if "/releases/tag/" in html_url:
        tag = html_url.rsplit("/releases/tag/", 1)[1].strip("/")
    if not tag:
        title = (entry.findtext("atom:title", "", ns) or "").strip()
        tag = title.split(" ", 1)[0]
    if not tag:
        raise RuntimeError("没能从订阅源里解析出版本号")

    title = (entry.findtext("atom:title", "", ns) or tag).strip()
    return {
        "tag_name": tag,
        "name": title,
        "body": _html_to_markdown(entry.findtext("atom:content", "", ns) or ""),
        "published_at": (entry.findtext("atom:updated", "", ns) or "").strip(),
        "html_url": html_url or release_page_url(tag),
        "assets": None,  # 订阅源没有资产列表 → 下载地址按命名规则拼
        "source": "atom",
    }


def _fetch_release() -> dict:
    """取最新 Release：优先 API，被限流或失败时降级到 Atom 订阅源。"""
    try:
        return _fetch_release_via_api()
    except Exception as exc:
        logger.info("GitHub API 不可用（%s），改用 releases.atom 订阅源", exc)
        return _fetch_release_via_atom()


def _download_url(tag: str, asset_name: str) -> str:
    """按命名规则直接拼下载地址 —— 不依赖 API，限流时照样能下。"""
    return f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{asset_name}"


def _select_asset(release: dict, variant: str, tag: str):
    """挑当前变体对应的更新包；拿不到资产列表时按命名规则拼地址。"""
    assets = release.get("assets")
    if assets is None:
        name = ASSET_FULL if variant == "full" else ASSET_LITE
        return {"name": name, "size": 0, "url": _download_url(tag, name)}
    return _pick_asset(assets, variant)


def _build_payload(release: dict, from_cache: bool) -> dict:
    variant = build_variant()
    tag = str(release.get("tag_name") or "")
    asset = _select_asset(release, variant, tag)

    skipped_version = _read_env_key("SKIP_VERSION").lstrip("vV")
    longer = is_newer(tag, __version__)
    skipped = bool(skipped_version) and skipped_version == tag.lstrip("vV")

    can_install, install_reason = _can_auto_install()

    payload = {
        "ok": True,
        "current_version": __version__,
        "latest_version": tag.lstrip("vV"),
        "latest_tag": tag,
        "has_update": longer and not skipped,
        "is_newer": longer,
        "skipped": skipped,
        "skipped_version": skipped_version,
        "release_name": release.get("name") or tag,
        "published_at": release.get("published_at") or "",
        "html_url": release.get("html_url") or release_page_url(tag),
        "notes": parse_release_notes(release.get("body") or ""),
        "build_variant": variant,
        "can_auto_install": can_install,
        "install_blocked_reason": install_reason,
        "asset": None,
        "checked_at": time.time(),
        "from_cache": from_cache,
        "source": release.get("source") or "api",
    }
    if asset:
        payload["asset"] = {
            "name": asset.get("name"),
            "size": asset.get("size") or 0,
            "url": asset.get("browser_download_url") or "",
        }
    else:
        payload["install_blocked_reason"] = (
            f"该版本没有上传适配当前版本（{'完整版' if variant == 'full' else '精简版'}）的文件"
        )
        payload["can_auto_install"] = False
    return payload


def check_update(force: bool = False) -> dict:
    """检查是否有新版本。命中缓存就直接返回，force=True 强制重新请求。"""
    now = time.time()

    with _cache_lock:
        cached_ts = _cache["ts"]
        cached_data = _cache["data"]

    # 内存缓存
    if not force and cached_data and (now - cached_ts) < _CACHE_TTL:
        return dict(cached_data, from_cache=True)

    # 落盘缓存（进程重启后仍然有效）
    if not force and not cached_data:
        disk = _load_disk_cache()
        if disk and (now - float(disk.get("_ts") or 0)) < _CACHE_TTL:
            with _cache_lock:
                _cache["ts"] = float(disk.get("_ts") or 0)
                _cache["data"] = disk.get("data")
            return dict(disk["data"], from_cache=True)

    try:
        release = _fetch_release()
    except Exception as exc:
        logger.warning("检查更新失败: %s", exc)
        # 请求失败时，宁可用旧结果也不要让页面一片空白
        fallback = cached_data or (_load_disk_cache().get("data"))
        if fallback:
            return dict(fallback, from_cache=True, stale=True,
                        warning=f"检查更新失败（{exc}），下面是上次的结果")
        return {"ok": False, "error": str(exc), "current_version": __version__}

    payload = _build_payload(release, from_cache=False)
    with _cache_lock:
        _cache["ts"] = now
        _cache["data"] = payload
    _save_disk_cache({"_ts": now, "data": payload})
    return payload


def invalidate_cache() -> None:
    """让下一次检查重新请求 GitHub。"""
    with _cache_lock:
        _cache["ts"] = 0.0
        _cache["data"] = None


def mark_skipped(version: str) -> dict:
    """忽略某个版本：写进 .env 的 SKIP_VERSION，之后不再提示它。"""
    cleaned = str(version or "").lstrip("vV").strip()
    if not cleaned:
        return {"ok": False, "error": "缺少版本号"}
    try:
        from src.config import find_env_file, write_env_atomic
        env_path = find_env_file()
        write_env_atomic(env_path, {"SKIP_VERSION": cleaned})
        os.environ["SKIP_VERSION"] = cleaned
    except Exception as exc:
        logger.warning("写入 SKIP_VERSION 失败: %s", exc)
        return {"ok": False, "error": f"保存失败：{exc}"}
    invalidate_cache()
    return {"ok": True, "skipped_version": cleaned}


# ── 下载 ──────────────────────────────────────────────────────────────

_download_lock = threading.Lock()
_download_cancel = threading.Event()
_download_thread = None
_download_state = {
    "state": "idle",     # idle | downloading | done | error | cancelled
    "received": 0,
    "total": 0,
    "error": "",
    "path": "",
    "asset_name": "",
    "tag": "",
    "started_at": 0.0,
    "finished_at": 0.0,
}


def download_state() -> dict:
    with _download_lock:
        state = dict(_download_state)
    if state["state"] == "downloading" and state["started_at"]:
        elapsed = max(time.time() - state["started_at"], 0.001)
        state["speed"] = int(state["received"] / elapsed)
    else:
        state["speed"] = 0
    return state


def _download_worker(url: str, dest: Path, total: int, tag: str, asset_name: str) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        headers = {"User-Agent": _USER_AGENT}
        token = _read_env_key("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with requests.get(url, headers=headers, stream=True, timeout=_TIMEOUT) as response:
            if response.status_code == 404:
                raise RuntimeError("该版本没有上传适配当前版本的文件，请到 GitHub 手动下载")
            response.raise_for_status()
            if not total:
                total = int(response.headers.get("Content-Length") or 0)
            with open(part, "wb") as handle:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if _download_cancel.is_set():
                        raise InterruptedError("已取消")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    with _download_lock:
                        _download_state["received"] += len(chunk)
        if _download_cancel.is_set():
            raise InterruptedError("已取消")
        part.replace(dest)
        with _download_lock:
            _download_state.update({
                "state": "done",
                "path": str(dest),
                "finished_at": time.time(),
            })
        logger.info("更新包下载完成: %s", dest)
    except InterruptedError:
        part.unlink(missing_ok=True)
        with _download_lock:
            _download_state.update({"state": "cancelled", "finished_at": time.time()})
        logger.info("更新包下载已取消")
    except Exception as exc:
        part.unlink(missing_ok=True)
        with _download_lock:
            _download_state.update({
                "state": "error",
                "error": str(exc),
                "finished_at": time.time(),
            })
        logger.warning("更新包下载失败: %s", exc)


def start_download() -> dict:
    """开始下载匹配当前变体的更新包。已在下载中则直接返回当前状态。"""
    global _download_thread

    with _download_lock:
        if _download_state["state"] == "downloading":
            return {"ok": True, "state": dict(_download_state)}

    payload = check_update()
    if not payload.get("ok"):
        return {"ok": False, "error": payload.get("error") or "无法获取版本信息"}
    asset = payload.get("asset")
    if not asset or not asset.get("url"):
        return {"ok": False, "error": payload.get("install_blocked_reason") or "没有可下载的文件"}

    # 清掉上一轮留下的旧包，避免 data/updates 越堆越大
    for old in _updates_dir().glob("*.exe"):
        try:
            old.unlink()
        except Exception:
            pass

    tag = payload.get("latest_tag") or ""
    dest = _updates_dir() / f"{tag.lstrip('vV')}-{asset['name']}"

    _download_cancel.clear()
    with _download_lock:
        _download_state.update({
            "state": "downloading",
            "received": 0,
            "total": int(asset.get("size") or 0),
            "error": "",
            "path": "",
            "asset_name": asset["name"],
            "tag": tag,
            "started_at": time.time(),
            "finished_at": 0.0,
        })

    _download_thread = threading.Thread(
        target=_download_worker,
        args=(asset["url"], dest, int(asset.get("size") or 0), tag, asset["name"]),
        name="update-download",
        daemon=True,
    )
    _download_thread.start()
    return {"ok": True, "state": download_state()}


def cancel_download() -> dict:
    if _download_thread and _download_thread.is_alive():
        _download_cancel.set()
        return {"ok": True, "state": "cancelling"}
    return {"ok": True, "state": "idle"}


# ── 安装（就地替换 exe 并重启）────────────────────────────────────────

_HELPER_TEMPLATE = """@echo off
setlocal enabledelayedexpansion
set "TARGET={target}"
set "SOURCE={source}"
set "TRIES=0"

:swap
set /a TRIES+=1
rem 运行中的 exe 无法被重命名，所以"能否改名旧文件"就是"进程是否已退出"的判据。
rem 这比 tasklist 更可靠 —— 不受系统语言影响。
move /y "%TARGET%" "%TARGET%.old" >nul 2>&1
if errorlevel 1 (
  if !TRIES! lss 30 (
    timeout /t 1 /nobreak >nul
    goto swap
  )
  exit /b 1
)

move /y "%SOURCE%" "%TARGET%" >nul 2>&1
if errorlevel 1 (
  rem 换新文件失败 —— 回滚，保证旧版还能用
  move /y "%TARGET%.old" "%TARGET%" >nul 2>&1
  exit /b 1
)

del /q "%TARGET%.old" >nul 2>&1
start "" "%TARGET%"
timeout /t 2 /nobreak >nul
del /q "%~f0"
exit /b 0
"""


def _write_helper_script(target: Path, source: Path) -> Path:
    """生成替换用的批处理，放到临时目录（不污染 data/）。"""
    import tempfile
    bat_path = Path(tempfile.gettempdir()) / f"wx-assist-update-{os.getpid()}.bat"
    bat_path.write_text(
        _HELPER_TEMPLATE.format(target=str(target), source=str(source)),
        encoding="gbk",
        errors="replace",
    )
    return bat_path


def install_update() -> dict:
    """把下载好的 exe 原地替换掉当前程序，然后重启。

    替换动作交给独立批处理完成（Windows 上运行中的 exe 换不掉自己）：
    等本进程退出 → 改名旧文件 → 换上新的 → 启动 → 自删。
    任何一步失败都会回滚，最坏结果只是"更新失败，旧版照常可用"。
    """
    can_install, reason = _can_auto_install()
    if not can_install:
        return {"ok": False, "error": reason, "fallback_url": release_page_url()}

    state = download_state()
    if state["state"] != "done" or not state.get("path"):
        return {"ok": False, "error": "更新包还没下载完成，请先下载"}
    source = Path(state["path"])
    if not source.exists():
        return {"ok": False, "error": "下载的更新包已不存在，请重新下载"}

    target = Path(sys.executable).resolve()
    try:
        bat_path = _write_helper_script(target, source)
        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            cwd=str(target.parent),
        )
    except Exception as exc:
        logger.exception("启动更新程序失败")
        return {"ok": False, "error": f"启动更新程序失败：{exc}"}

    logger.info("更新程序已启动，准备退出当前进程: %s -> %s", source, target)
    return {"ok": True, "will_restart": True, "target": str(target)}


def schedule_app_exit(delay: float = 1.5) -> None:
    """稍后结束进程，让前端有时间把"正在更新"的提示显示出来。

    先停 Bot（关掉数据库连接），再 _exit —— 这里刻意跳过 atexit 的
    5 秒强杀兜底，避免它和更新批处理抢时间。
    """
    def _worker():
        time.sleep(delay)
        try:
            from src.web.server import _bot_control
            _bot_control.stop()
        except Exception as exc:
            logger.debug("退出前停止 Bot 失败: %s", exc)
        try:
            import logging as _logging
            _logging.shutdown()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_worker, name="update-exit", daemon=True).start()
