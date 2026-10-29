"""离线 RAG 自检：在临时目录建索引、重启、检索。

不读取也不修改真实索引，打包版可安全执行。
"""

import shutil
import tempfile
import time
from pathlib import Path

import numpy as np


def run_check(vectors: int = 1100, keep: bool = False) -> dict:
    """建立一次性索引并验证“写入 → 重开 → 检索”，返回结果字典。"""
    from .models import Chunk
    from .vector_store import ChromaStore

    vectors = max(1, int(vectors))
    work = Path(tempfile.mkdtemp(prefix="rag-check-"))
    index = work / "chroma"
    result = {
        "ok": False,
        "vectors": vectors,
        "stored": 0,
        "reopened": 0,
        "hits": 0,
        "elapsed": 0.0,
        "path": str(work) if keep else "",
    }
    store = None
    started = time.monotonic()
    try:
        chunks = [
            Chunk(f"check-{i}", "check", str(i), "check", "checker",
                  "自检样本 " * 20, str(1700000000 + i))
            for i in range(vectors)
        ]
        rng = np.random.default_rng(42)
        matrix = rng.random((vectors, 512), dtype=np.float32)

        store = ChromaStore(str(index), startup_timeout=180)
        store.warmup()
        for offset in range(0, vectors, 512):
            store.add(chunks[offset:offset + 512], matrix[offset:offset + 512])
        stored = store.count()
        store.close()
        store = None

        reopened = ChromaStore(str(index), startup_timeout=180)
        try:
            reopened.warmup()
            reopened_count = reopened.count()
            hits = reopened.search(matrix[0], top_k=3)
        finally:
            reopened.close()

        result.update(
            ok=stored == vectors and reopened_count == vectors and bool(hits),
            stored=stored,
            reopened=reopened_count,
            hits=len(hits),
        )
    finally:
        result["elapsed"] = round(time.monotonic() - started, 1)
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    return result
