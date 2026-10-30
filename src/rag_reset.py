"""索引重置：写标记，下次启动时改名旧索引并自动重建。

应用运行时 Chroma 子进程占着索引文件，不能直接删除/改名；
所以用户点「重置索引」只写一个标记，重启后由主流程执行。
"""

import json
import os
import shutil
import time
from pathlib import Path

FLAG_FILE = "reset_rag.pending"
INDEX_ITEMS = ("chroma", "rag_state.json", "last_indexed.json")


def _data_dir(data_dir=None) -> Path:
    return Path(data_dir or "data")


def flag_path(data_dir=None) -> Path:
    return _data_dir(data_dir) / FLAG_FILE


def request_reset(data_dir=None, reason: str = "") -> Path:
    """记录一次重置请求；下次启动时生效。"""
    target = flag_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reason": reason,
    }, ensure_ascii=False), encoding="utf-8")
    return target


def is_pending(data_dir=None) -> bool:
    return flag_path(data_dir).exists()


def perform_pending_reset(data_dir=None) -> dict | None:
    """启动时调用：把旧索引改名留存，清掉标记。没请求则返回 None。"""
    flag = flag_path(data_dir)
    if not flag.exists():
        return None

    data = _data_dir(data_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for name in INDEX_ITEMS:
        source = data / name
        if not source.exists():
            continue
        target = data / f"{name}.broken-{stamp}"
        index = 1
        while target.exists():
            target = data / f"{name}.broken-{stamp}-{index}"
            index += 1
        os.replace(source, target)
        moved.append({"from": name, "to": target.name})

    try:
        flag.unlink()
    except OSError:
        pass
    return {"stamp": stamp, "moved": moved}


def cleanup_broken(data_dir=None) -> list[str]:
    """重建成功后删除留存的旧索引（派生数据，不影响聊天记录）。"""
    data = _data_dir(data_dir)
    removed = []
    for name in INDEX_ITEMS:
        for path in data.glob(f"{name}.broken-*"):
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed
