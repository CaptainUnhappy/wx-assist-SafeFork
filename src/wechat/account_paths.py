"""微信账号目录识别 —— 按内部结构识别，不依赖目录名前缀。

背景
----
微信 4.x 的账号目录名 = 微信号 + ``_`` + 4位hex后缀（如 ``caojunzhi_abcd``），
只有从未设置过微信号的账号才以 ``wxid_`` 开头。按 ``startswith("wxid_")``
扫描目录会漏掉大量自定义微信号用户，因此统一改为结构识别：

- 4.x：``<账号目录>/db_storage/session/session.db`` 存在
- 3.x：``<账号目录>/Msg/MSG*.db``（含 ``Msg/Multi/MSG*.db``）存在

特殊目录（如 ``all_users``、明文缓存等）因不含上述结构会被自然排除。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def is_account_dir(path) -> bool:
    """判断目录是否为微信账号目录（按内部结构判定，与目录名无关）。"""
    try:
        p = Path(path)
        if not p.is_dir():
            return False
        # 4.x：db_storage/session/session.db
        if (p / "db_storage" / "session" / "session.db").exists():
            return True
        # 3.x：Msg/MSG*.db 或 Msg/Multi/MSG*.db
        msg_dir = p / "Msg"
        if msg_dir.is_dir():
            if any(msg_dir.glob("MSG*.db")):
                return True
            multi = msg_dir / "Multi"
            if multi.is_dir() and any(multi.glob("MSG*.db")):
                return True
        return False
    except OSError:
        return False


def list_account_dirs(base_dir) -> list:
    """列出数据根目录下的全部账号目录，按修改时间倒序（最近登录在前）。

    目录不可读（权限/不存在）时返回空列表，与旧版"跳过该候选目录"
    的语义一致。
    """
    base = Path(base_dir)
    try:
        dirs = [d for d in base.iterdir() if is_account_dir(d)]
    except OSError:
        return []
    try:
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return dirs


def find_account_base_from_dbpath(db_path) -> str:
    """从数据库文件路径反推数据根目录（账号目录的父目录）。

    兼容 4.x 与 3.x 布局；路径不存在或不在标准微信布局中时返回空串。
    """
    try:
        p = Path(db_path).resolve()
    except OSError:
        return ""
    if p.is_file():
        p = p.parent
    for anc in (p, *p.parents):
        if is_account_dir(anc):
            return str(anc.parent)
    return ""
