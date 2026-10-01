"""微信数据库客户端 —— 纯 Python 实现。

历史与现状
----------
本模块原先通过 ctypes 加载第三方闭源组件 ``lib/wcdb_api.dll``（来自 WeFlow
项目）读取微信数据库，并对其做一字节内存补丁绕过其完整性自检。

该 DLL 内置联网授权校验（向 ``api.weflow.top`` 请求 token），2026-10-01 起
授权服务失效，``InitProtection`` 返回 ``-101``、``wcdb_init`` 返回 ``-1000``，
读取能力整体不可用。WeFlow 自身也已在新版本中改为
``wcdb_authenticate`` + 本地 TCP 握手的另一套授权，无法直接沿用。

微信 4.x 的数据库是**标准 SQLCipher 4**，密钥由本机进程内存中提取即可，
解密与查询完全可以用 Python 实现，不依赖任何第三方二进制。因此本模块改为：

- 解密：:mod:`src.wechat.db_crypto`（PBKDF2 + AES-CBC 分页 + WAL 合并）
- 读取：:mod:`src.wechat.db_reader`（sqlite3 查询 + 跨分片合并 + 结构解析）

**对外接口与旧实现完全一致**，因此 ``api_handlers`` / ``wcdb_backend`` /
``sns_client`` 等上层代码无需改动。

不支持的能力
------------
以下功能本质上需要**写回微信原始数据库**（在原始库里建 SQLite 触发器），
只读解密方案无法实现，因此保留接口但返回明确的"不支持"：

- 消息防撤回（``*_message_anti_revoke_trigger``）
- 朋友圈防删（``*_sns_block_delete_trigger``）
- 删除朋友圈（``delete_sns_post``）
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from . import db_crypto
from .db_reader import WeChatDbReader

logger = logging.getLogger(__name__)

# 本地读取模式下不支持的操作（需要写回微信原始库）
_UNSUPPORTED = "当前使用本地读取模式（纯 Python 解密），不支持写回微信数据库"


# ── 数据目录探测（与原实现一致，与 DLL 无关）──────────────────────────

def _find_wxid_and_dbpath(custom_base_dir: str = ""):
    """自动探测微信 wxid 与数据根目录。

    ``WXID`` / ``DB_PATH`` 环境变量优先；否则扫描传入目录，再回退到
    ``Documents\\xwechat_files``、``Documents\\WeChat Files``。
    """
    env_wxid = os.environ.get("WXID", "").strip()
    env_db_path = os.environ.get("DB_PATH", "").strip()
    if env_wxid and env_db_path:
        p = Path(env_db_path)
        if p.exists():
            if p.is_file():
                parent = p.parent
                while parent.parent != parent:
                    if parent.name.startswith("wxid_"):
                        base = str(parent.parent)
                        logger.info("Env override (file→dir): wxid=%s base=%s", env_wxid, base)
                        return env_wxid, base
                    parent = parent.parent
            elif p.is_dir():
                logger.info("Env override (dir): wxid=%s base=%s", env_wxid, env_db_path)
                return env_wxid, env_db_path

    candidates: list[Path] = []
    if custom_base_dir:
        custom = Path(custom_base_dir)
        if custom.exists() and custom.is_dir():
            candidates.append(custom)
            logger.info("Scanning custom WECHAT_DATA_DIR: %s", custom)
        else:
            logger.warning(
                "WECHAT_DATA_DIR=%s does not exist or is not a directory — "
                "falling back to auto-detection",
                custom_base_dir,
            )

    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(0, 5, 0, 0, buf)  # CSIDL_PERSONAL
        documents = Path(buf.value)
    except Exception:                              # noqa: BLE001
        documents = Path.home() / "Documents"
    for default_base in (documents / "xwechat_files", documents / "WeChat Files"):
        if default_base not in candidates:
            candidates.append(default_base)

    for base in candidates:
        if not base.exists():
            continue
        try:
            wxid_dirs = sorted(
                [d for d in base.iterdir() if d.is_dir() and d.name.startswith("wxid_")],
                key=lambda d: d.stat().st_mtime,
                reverse=True,
            )
        except PermissionError:
            logger.warning("Permission denied reading %s — skipping", base)
            continue

        for wxid_dir in wxid_dirs:
            session_db = wxid_dir / "db_storage" / "session" / "session.db"
            if session_db.exists():
                source = "custom" if (base == candidates[0] and custom_base_dir) else "auto"
                logger.info("%s-detected: wxid=%s db=%s", source, wxid_dir.name, str(base))
                return wxid_dir.name, str(base)

    raise FileNotFoundError(
        "未找到微信数据目录。可能原因：\n"
        "1. 微信未安装或未登录过；\n"
        "2. 微信数据目录不在默认位置（如已迁移到其他盘）。\n"
        "请在系统配置 → 数据路径中手动指定微信数据目录（即包含 wxid_* 文件夹的父目录）。"
    )


def _find_account_dir(wxid: str, base_dir: str) -> Path:
    """在数据根目录下定位具体的账号目录。

    微信的账号目录名形如 ``wxid_xxx_abcd``，而 ``WXID`` 配置里可能只写了
    ``wxid_xxx`` 前缀，这里做前缀匹配。
    """
    base = Path(base_dir)
    if not base.exists():
        raise RuntimeError(f"微信数据目录不存在: {base}")

    if wxid:
        exact = base / wxid
        if exact.is_dir():
            return exact
        for entry in base.iterdir():
            if entry.is_dir() and entry.name.startswith(wxid):
                return entry

    for entry in base.iterdir():
        if entry.is_dir() and entry.name.startswith("wxid_"):
            return entry

    raise RuntimeError(f"在 {base} 下找不到 wxid_* 账号目录")


# ── 客户端 ───────────────────────────────────────────────────────────

class WcdbNativeClient:
    """微信数据库客户端（纯 Python 实现，接口与旧 DLL 版本一致）。"""

    def __init__(self, dll_dir=None, config_path=None):
        # dll_dir / config_path 仅为兼容旧调用方签名而保留，不再使用
        self._dll_dir = dll_dir
        self._config_path = config_path

        self._reader: WeChatDbReader | None = None
        self._key = ""
        self._wxid = ""
        self._base_dir = ""
        self._account_dir = ""
        self._config: dict = {}
        self._nicknames: dict[str, str] = {}
        self._opened = False
        self._lock = threading.RLock()

        self._load_config()

    # ── 属性（上层依赖）──────────────────────────────────────────────

    @property
    def account_dir(self) -> str:
        """解析出的账号目录（``.../xwechat_files/wxid_xxx_abcd``）。"""
        return self._account_dir

    @property
    def favorite_db_path(self) -> str:
        """收藏库路径。"""
        if self._account_dir:
            return str(Path(self._account_dir) / "db_storage" / "favorite" / "favorite.db")
        return ""

    @property
    def sns_db_path(self) -> str:
        """朋友圈库路径。"""
        if self._account_dir:
            return str(Path(self._account_dir) / "db_storage" / "sns" / "sns.db")
        return ""

    # ── 配置与初始化 ────────────────────────────────────────────────

    def _load_config(self) -> None:
        """读取微信目录与密钥（优先环境变量，其次 .env）。"""
        custom_dir = ""
        try:
            from src.config import load_config
            config = load_config()
            custom_dir = getattr(config, "wechat_data_dir", "") or ""
            if not self._key:
                self._key = (getattr(config, "wcdb_key", "") or "").strip()
        except Exception as exc:                   # noqa: BLE001
            logger.debug("load_config 不可用，回退到环境变量: %s", exc)

        try:
            self._wxid, self._base_dir = _find_wxid_and_dbpath(custom_dir)
        except FileNotFoundError:
            # 探测失败不在这里抛——留到 init() 时给出更完整的上下文
            self._wxid, self._base_dir = "", custom_dir

        if not self._key:
            self._key = os.environ.get("WCDB_KEY", "").strip()

        # 尽量在构造阶段就解析出账号目录，方便上层在任何时机访问 account_dir
        if self._wxid and self._base_dir:
            try:
                self._account_dir = str(_find_account_dir(self._wxid, self._base_dir))
            except Exception as exc:               # noqa: BLE001
                logger.debug("构造阶段解析账号目录失败: %s", exc)

        self._config = {"myWxid": self._wxid, "dbPath": self._base_dir}

    def init(self):
        """初始化读取器（等价于旧版的 DLL 加载 + 补丁 + 引擎初始化）。"""
        with self._lock:
            if self._reader is not None:
                logger.debug("读取器已初始化，跳过")
                return

            if not self._base_dir:
                self._wxid, self._base_dir = _find_wxid_and_dbpath()
            if not self._key:
                raise RuntimeError(
                    "KEY_MISSING: 未找到 WCDB_KEY。请先在「系统配置 → 数据配置」"
                    "完成密钥提取（微信保持登录状态）。"
                )

            account_dir = _find_account_dir(self._wxid, self._base_dir)
            self._account_dir = str(account_dir)

            self._reader = WeChatDbReader(
                account_dir, self._key, cache_dir=self._default_cache_dir(),
            )
            self._reader.set_my_wxid(self._wxid)
            logger.info("本地读取器已就绪: %s", self._account_dir)

    def _default_cache_dir(self) -> Path:
        """明文解密缓存目录（放在项目 data/ 下，便于清理且不污染微信目录）。"""
        try:
            from src.config import PROJECT_ROOT
            return Path(PROJECT_ROOT) / "data" / "decrypted_cache"
        except Exception:                          # noqa: BLE001
            return Path.cwd() / "data" / "decrypted_cache"

    def open(self):
        """打开账号库并校验密钥（失败抛 RuntimeError，与原实现一致）。"""
        with self._lock:
            if self._reader is None:
                self.init()

            session_db = Path(self._account_dir) / "db_storage" / "session" / "session.db"
            if not session_db.exists():
                raise RuntimeError(f"session.db not found in {self._account_dir}")

            # 用首页 HMAC 校验密钥，避免带着错误密钥跑到查询阶段才报错
            try:
                key_material = db_crypto.parse_key(self._key)
            except db_crypto.DecryptError as exc:
                raise RuntimeError(f"KEY_MISSING: {exc}") from exc

            if not db_crypto.verify_key(key_material, session_db):
                raise RuntimeError(
                    "KEY_MISSING: 数据库密钥不正确或已失效，请重新提取密钥"
                )

            self._opened = True
            self._load_nickname_cache()
            logger.info("账号库已打开: %s", session_db)
            return True

    def _load_nickname_cache(self) -> None:
        """预加载 wxid → 显示名映射。"""
        if self._reader is None:
            return
        try:
            names = self._reader.get_display_names(
                [s["username"] for s in self._reader.get_sessions(limit=1000)]
            )
            self._nicknames.update({k: v for k, v in names.items() if v})
        except Exception as exc:                   # noqa: BLE001
            logger.debug("预加载显示名失败: %s", exc)

    def reopen(self):
        """重新打开（密钥或数据目录变化后调用）。"""
        with self._lock:
            self.close()
            self._load_config()
            self.init()
            self.open()

    def close(self):
        """关闭读取器（幂等）。"""
        with self._lock:
            self._opened = False
            if self._reader is not None:
                try:
                    self._reader.close()
                except Exception:                  # noqa: BLE001
                    pass
                self._reader = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── 内部 ────────────────────────────────────────────────────────

    def _require_reader(self) -> WeChatDbReader:
        if self._reader is None:
            raise RuntimeError("读取器未初始化，请先调用 init() 与 open()")
        return self._reader

    # ── 会话 ────────────────────────────────────────────────────────

    def get_sessions(self, limit=500):
        """会话列表（按最近活跃排序）。"""
        return self._require_reader().get_sessions(limit=limit)

    # ── 消息 ────────────────────────────────────────────────────────

    def get_messages(self, talker, limit=200, offset=0):
        """指定会话的消息（跨分片合并，按时间升序）。"""
        return self._require_reader().get_messages(talker, limit=limit, offset=offset)

    # ── 显示名 / 头像 ───────────────────────────────────────────────

    def get_display_names(self, usernames):
        """wxid → 显示名。"""
        if not usernames:
            return {}
        return self._require_reader().get_display_names(usernames)

    def get_avatar_urls(self, usernames):
        """wxid → 头像 URL。"""
        if not usernames:
            return {}
        return self._require_reader().get_avatar_urls(usernames)

    def resolve_nickname(self, wxid):
        """单个 wxid 的显示名（带内部缓存）。"""
        if not wxid:
            return ""
        if wxid in self._nicknames and self._nicknames[wxid]:
            return self._nicknames[wxid]
        try:
            name = self._require_reader().resolve_nickname(wxid)
        except Exception:                          # noqa: BLE001
            name = wxid
        self._nicknames[wxid] = name
        return name

    # ── 联系人 / 群 ─────────────────────────────────────────────────

    def get_contacts(self, keyword="", limit=1000):
        """联系人列表。"""
        return self._require_reader().get_contacts(keyword=keyword, limit=limit)

    def get_contact_status(self, usernames: list[str]) -> dict:
        """置顶/折叠/静音状态。"""
        if not usernames:
            return {}
        try:
            return self._require_reader().get_contact_status(usernames)
        except Exception as exc:                   # noqa: BLE001
            logger.debug("get_contact_status 失败: %s", exc)
            return {}

    def get_group_members(self, chatroom_id: str) -> list[dict]:
        """群成员列表。"""
        if not chatroom_id:
            return []
        return self._require_reader().get_group_members(chatroom_id)

    # ── 通用 SQL ────────────────────────────────────────────────────

    def exec_query(self, kind, db_path="", sql=""):
        """通用 SQL 查询，返回行 dict 列表。

        ``kind="message"`` 且带 ``db_path`` 时，若该路径是加密的微信库，
        会先解密再查询（``db_reader`` 内部处理）。
        """
        if not sql:
            return []
        return self._require_reader().exec_query(kind, db_path=db_path, sql=sql)

    # ── 收藏 ────────────────────────────────────────────────────────

    def get_favorites(self, limit=200, offset=0):
        """收藏列表（表 ``fav_db_item``）。"""
        return self._require_reader().get_favorites(limit=limit, offset=offset)

    # ── 语音 ────────────────────────────────────────────────────────

    def get_voice_data(self, session_id, create_time, local_id, svr_id, candidates=None):
        """取语音数据。返回 ``{"success": bool, "hex": str}``。"""
        return self._require_reader().get_voice_data(
            session_id, create_time=create_time, local_id=local_id, svr_id=svr_id,
            candidates=candidates,
        )

    # ── 图片路径（替代 wcdb_resolve_image_hardlink）──────────────────

    def resolve_image_hardlink(self, md5: str) -> str:
        """由图片 md5 定位 ``.dat`` 文件路径。"""
        if not md5:
            return ""
        try:
            return self._require_reader().resolve_image_hardlink(
                md5, account_dir=self._account_dir
            )
        except Exception as exc:                   # noqa: BLE001
            logger.debug("resolve_image_hardlink 失败: %s", exc)
            return ""

    # ── 朋友圈 ──────────────────────────────────────────────────────

    def get_sns_timeline(self, limit=20, offset=0, usernames=None,
                         keyword=None, start_time=0, end_time=0):
        """朋友圈时间线。"""
        return self._require_reader().get_sns_timeline(
            limit=limit, offset=offset, usernames=usernames,
            keyword=keyword or "", start_time=start_time, end_time=end_time,
        )

    def get_sns_usernames(self):
        """发过朋友圈的 wxid 列表。"""
        return self._require_reader().get_sns_usernames()

    # ── 需要写回微信库的操作：本地读取模式不支持 ─────────────────────

    def install_sns_block_delete_trigger(self):
        return {"success": False, "error": _UNSUPPORTED}

    def uninstall_sns_block_delete_trigger(self):
        return {"success": False, "error": _UNSUPPORTED}

    def check_sns_block_delete_trigger(self):
        return {"success": False, "installed": False, "error": _UNSUPPORTED}

    def install_message_anti_revoke_trigger(self, session_id: str):
        return {"success": False, "error": _UNSUPPORTED}

    def uninstall_message_anti_revoke_trigger(self, session_id: str):
        return {"success": False, "error": _UNSUPPORTED}

    def check_message_anti_revoke_trigger(self, session_id: str):
        return {"success": False, "installed": False, "error": _UNSUPPORTED}

    def delete_sns_post(self, post_id):
        return {"success": False, "error": _UNSUPPORTED}
