"""离线重建 RAG 索引。

设计要点：
1. 先停止应用（检测到运行中的实例默认拒绝，除非 --force）。
2. 使用 messages.db 的 SQLite 快照，在 staging 目录重建，不碰线上索引。
3. 校验通过后才切换；切换过程写日志文件，崩溃后下次运行可自动回滚。
4. 聊天记录按 --days 窗口重建；公众号/朋友圈/收藏按当前缓存全量重建。

用法：
    python scripts/rebuild_rag.py [--data-dir data] [--days 30] [--skip-cache] [--force]
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ITEMS = ("chroma", "rag_state.json", "last_indexed.json")
JOURNAL = "rag_rebuild.json"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _detect_running(data_dir: Path) -> list[str]:
    try:
        import psutil
    except Exception:
        return []
    data_dir = data_dir.resolve()
    hits = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "cwd"]):
        try:
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe") or ""
            cmd = " ".join(proc.info.get("cmdline") or [])
            cwd = proc.info.get("cwd") or ""
            if name == "wx-assist.exe" and exe and Path(exe).parent == data_dir.parent:
                hits.append(f"wx-assist.exe (pid {proc.info['pid']})")
            elif name.startswith("python") and ("desktop.py" in cmd or "server.py" in cmd):
                if cwd and Path(cwd).resolve() == REPO_ROOT:
                    hits.append(f"python desktop/server (pid {proc.info['pid']})")
        except Exception:
            continue
    return hits


def _backup_db(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _journal_path(data_dir: Path) -> Path:
    return data_dir / JOURNAL


def _write_journal(data_dir: Path, payload: dict) -> None:
    tmp = _journal_path(data_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _journal_path(data_dir))


def _recover(data_dir: Path) -> None:
    """上一次切换中断时，尽量把旧索引恢复回原位。"""
    journal = _journal_path(data_dir)
    if journal.exists():
        try:
            data = json.loads(journal.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if data.get("state") in ("preparing", "switching"):
            backup = data.get("backup") or {}
            stamp = data.get("stamp", "recover")
            for name in ITEMS:
                target = data_dir / name
                previous = backup.get(name)
                if previous and Path(previous).exists():
                    if target.exists():
                        os.replace(target, data_dir / f"{name}.failed-{stamp}")
                    os.replace(previous, target)
                    _log(f"[恢复] {name} <- {previous}")
                elif target.exists() and not previous:
                    # 该项原来不存在（例如首次重建），移除半成品
                    os.replace(target, data_dir / f"{name}.failed-{stamp}")
            _write_journal(data_dir, {**data, "state": "recovered"})
            _log("[恢复] 检测到中断的重建，已还原旧索引")
    # 无日志但有残留 .previous-*：只在目标缺失时还原
    for name in ITEMS:
        target = data_dir / name
        if target.exists():
            continue
        candidates = sorted(data_dir.glob(f"{name}.previous-*"))
        if candidates:
            os.replace(candidates[-1], target)
            _log(f"[恢复] {name} 从 {candidates[-1].name} 还原")


def _switch(data_dir: Path, staging_data: Path) -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = {}
    _write_journal(data_dir, {"state": "preparing", "stamp": stamp, "backup": {}})

    for name in ITEMS:
        current = data_dir / name
        if current.exists():
            previous = data_dir / f"{name}.previous-{stamp}"
            os.replace(current, previous)
            backup[name] = str(previous)

    _write_journal(data_dir, {"state": "switching", "stamp": stamp, "backup": backup})

    try:
        for name in ITEMS:
            source = staging_data / name
            if source.exists():
                os.replace(source, data_dir / name)
    except Exception:
        for name in ITEMS:
            target = data_dir / name
            if target.exists():
                os.replace(target, staging_data / name)
            previous = backup.get(name)
            if previous and Path(previous).exists():
                os.replace(previous, target)
        _write_journal(data_dir, {"state": "rolled_back", "stamp": stamp, "backup": backup})
        raise

    _write_journal(data_dir, {"state": "done", "stamp": stamp, "backup": backup})
    return backup


def rebuild(data_dir: Path, days: int = 30, skip_cache: bool = False,
            embedder_factory=None) -> dict:
    from src.assistant.rag.chunking import SlidingWindowChunker
    from src.assistant.rag.embedder import FastEmbedder
    from src.assistant.rag.engine import RAGEngine
    from src.assistant.rag.vector_store import ChromaStore
    from src.db.content_cache import ContentCache

    data_dir = data_dir.resolve()
    _recover(data_dir)

    db_path = data_dir / "messages.db"
    if not db_path.exists():
        raise FileNotFoundError(f"messages.db not found: {db_path}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    staging_root = data_dir / f".rebuild-{stamp}"
    staging_data = staging_root / "data"
    staging_data.mkdir(parents=True, exist_ok=False)
    snapshot = staging_data / "messages.db"
    started = time.monotonic()

    _backup_db(db_path, snapshot)
    _log(f"[1/4] 已快照 messages.db ({snapshot.stat().st_size / 1048576:.0f} MB)")

    previous_cwd = Path.cwd()
    os.chdir(staging_root)  # last_indexed.json 等相对路径落到 staging
    connection = None
    store = None
    try:
        embedder = (embedder_factory or FastEmbedder)()
        embedder.warmup()
        store = ChromaStore(str(staging_data / "chroma"), startup_timeout=180)
        store.warmup()
        engine = RAGEngine(store, embedder, SlidingWindowChunker())
        engine._state_path = staging_data / "rag_state.json"

        connection = sqlite3.connect(snapshot, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        ok = engine.cold_start(connection, tracked_groups=None, days=days)
        if not ok:
            raise RuntimeError("聊天记录索引失败")
        _log(f"[2/4] 聊天记录索引完成，chunks={store.count()}")

        if not skip_cache:
            cache = ContentCache(str(snapshot))
            for source, label in (("oa", "公众号"), ("sns", "朋友圈"), ("fav", "收藏")):
                try:
                    if source == "oa":
                        cache._index_oa(engine)
                    elif source == "sns":
                        cache._index_sns(engine)
                    else:
                        cache._index_fav(engine)
                    _log(f"       {label}索引完成，chunks={store.count()}")
                except Exception as exc:
                    raise RuntimeError(f"{label}索引失败: {exc}") from exc
            _log("[3/4] 缓存索引完成")
        else:
            _log("[3/4] 跳过缓存索引")

        staged_count = store.count()
        store.close()
        store = None

        reopened = ChromaStore(str(staging_data / "chroma"), startup_timeout=180)
        try:
            reopened.warmup()
            verified = reopened.count()
        finally:
            reopened.close()
        if verified != staged_count or verified <= 0:
            raise RuntimeError(f"校验失败: staged={staged_count} reopened={verified}")
        _log(f"[4/4] 校验通过，chunks={verified}")

        connection.close()
        connection = None
        os.chdir(previous_cwd)

        backup = _switch(data_dir, staging_data)
        elapsed = round(time.monotonic() - started, 1)
        result = {
            "ok": True,
            "chunks": verified,
            "days": days,
            "seconds": elapsed,
            "previous": backup,
            "staging": str(staging_root),
        }
        (data_dir / "rag_rebuild_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result
    finally:
        os.chdir(previous_cwd)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线重建 RAG 索引")
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--skip-cache", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="检测到应用在运行时仍然继续（危险）")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    running = _detect_running(data_dir)
    if running and not args.force:
        _log("检测到应用可能正在运行：" + ", ".join(running))
        _log("请先完全退出应用后重试；确认无风险可加 --force。")
        return 2
    if running:
        _log("[警告] 检测到运行中的实例，但已指定 --force")

    try:
        result = rebuild(data_dir, days=args.days, skip_cache=args.skip_cache)
    except Exception as exc:
        _log(f"[失败] {exc}")
        return 1
    _log(json.dumps(result, ensure_ascii=False))
    _log("旧索引保留在 *.previous-*；确认无误后可手动删除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
