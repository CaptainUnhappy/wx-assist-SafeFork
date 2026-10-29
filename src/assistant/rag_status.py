"""RAG 进度快照（线程安全，只读暴露给 Dashboard）。"""

import copy
import threading
import time

_LOCK = threading.RLock()

_JOB_LABELS = {
    "msg": "聊天记录",
    "oa": "公众号文章",
    "sns": "朋友圈",
    "fav": "收藏",
}


def _blank_job(label: str):
    return {
        "label": label,
        "completed": 0,
        "total": 0,
        "state": "idle",
        "started_at": 0.0,
        "elapsed": 0.0,
    }


_STATE = {
    "phase": "disabled",
    "error": "",
    "device": "cpu",
    "batch_size": 0,
    "started_at": 0.0,
    "finished_at": 0.0,
    "jobs": {key: _blank_job(label) for key, label in _JOB_LABELS.items()},
}


def reset(phase: str = "disabled", device: str = "cpu", batch_size: int = 0) -> None:
    """重置整个状态（启动/关闭 RAG 时调用）。"""
    with _LOCK:
        _STATE["phase"] = phase
        _STATE["error"] = ""
        _STATE["device"] = device
        _STATE["batch_size"] = int(batch_size or 0)
        _STATE["started_at"] = time.time() if phase not in ("disabled",) else 0.0
        _STATE["finished_at"] = 0.0
        _STATE["jobs"] = {key: _blank_job(label) for key, label in _JOB_LABELS.items()}


def update(phase: str = None, error: str = None,
           device: str = None, batch_size: int = None) -> None:
    with _LOCK:
        if phase is not None:
            _STATE["phase"] = phase
            if phase in ("ready", "error"):
                _STATE["finished_at"] = _STATE["finished_at"] or time.time()
            elif phase in ("loading", "preparing", "indexing"):
                _STATE["finished_at"] = 0.0
        if error is not None:
            _STATE["error"] = error
        if device is not None:
            _STATE["device"] = device
        if batch_size is not None:
            _STATE["batch_size"] = int(batch_size)


def job(key: str, label: str = None, total: int = None, completed: int = None,
        delta: int = 0, state: str = None) -> None:
    """更新单个来源的进度；任何异常都不应影响索引本身。"""
    with _LOCK:
        item = _STATE["jobs"].setdefault(key, _blank_job(label or key))
        if label:
            item["label"] = label
        if state == "running" and not item["started_at"]:
            item["started_at"] = time.time()
        if state:
            item["state"] = state
        if total is not None:
            item["total"] = max(0, int(total))
        if completed is not None:
            item["completed"] = max(0, int(completed))
        elif delta:
            item["completed"] = max(0, item["completed"] + int(delta))
        if item["started_at"]:
            item["elapsed"] = round(time.time() - item["started_at"], 1)


def snapshot() -> dict:
    with _LOCK:
        data = copy.deepcopy(_STATE)
    end = data["finished_at"] or time.time()
    data["elapsed"] = round(end - data["started_at"], 1) if data["started_at"] else 0.0
    for item in data["jobs"].values():
        total = item["total"]
        percent = item["completed"] * 100.0 / total if total else 0.0
        item["percent"] = round(min(100.0, max(0.0, percent)), 1)
    return data
