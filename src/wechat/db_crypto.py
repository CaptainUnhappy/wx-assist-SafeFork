"""微信 4.x 数据库解密层 —— 纯 Python 实现，零原生依赖。

背景
----
本项目原先通过第三方闭源组件 ``lib/wcdb_api.dll``（来自 WeFlow）读取微信
本地数据库。该组件内置联网授权校验，授权服务器失效后 ``wcdb_init`` 返回
``-1000``、``InitProtection`` 返回 ``-101``，导致整个读取层不可用。

微信 4.x 的数据库使用**标准 SQLCipher 4** 加密，不依赖任何私有格式，
因此可以完全用 Python 实现，不再依赖任何第三方 DLL：

- 密钥派生：``PBKDF2-HMAC-SHA512``，256000 轮 → 32 字节 enc_key
- MAC 密钥：``mac_salt = salt XOR 0x3A``，``PBKDF2(enc_key, mac_salt, 2 轮)``
- 页结构：每页 4096 字节，尾部保留 80 字节（16 字节 IV + 64 字节 HMAC-SHA512）
- 页解密：``AES-256-CBC``，IV 取页尾 reserve 区
- 页校验：``HMAC-SHA512(页数据 + 页号小端 4 字节)``

实测结论（2026-10-01）
----------------------
- ``WCDB_KEY`` 的 64 位 hex **直接 hex 解码即为 enc_key**，无需再 XOR 任何
  ``internal_db_key``（那是 WeChatDataAnalysis 的另一条密钥恢复路径）。
- 6 个库、7 万余页解密全部 HMAC 通过，零异常页。
- 解密速度 114~120 MB/s。

本模块只负责"加密页 ↔ 明文页"的转换，不含任何业务查询逻辑；
业务查询见 ``db_reader.py``。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── SQLCipher 4 常量 ─────────────────────────────────────────────────
PAGE_SIZE = 4096
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE_SIZE = IV_SIZE + HMAC_SIZE          # 80
KEY_SIZE = 32
PBKDF2_ROUNDS = 256000
MAC_ROUNDS = 2
SQLITE_HEADER = b"SQLite format 3\x00"

# ── SQLite WAL 常量 ─────────────────────────────────────────────────
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683

# 解密时的分块读大小（须为 PAGE_SIZE 的整数倍，保证页边界对齐）。
_READ_CHUNK = 1 << 20  # 1 MB


class DecryptError(RuntimeError):
    """解密失败（密钥不对、文件损坏等）。"""


class KeyMismatchError(DecryptError):
    """密钥与数据库不匹配（首页 HMAC 校验失败）。"""


# ── 密钥派生 ─────────────────────────────────────────────────────────

def derive_keys(key_material: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """从密钥素材和 salt 派生 ``(enc_key, mac_key)``。

    Args:
        key_material: 32 字节的原始密钥（WCDB_KEY 的 hex 解码结果）。
        salt: 数据库首页前 16 字节。

    Returns:
        ``(enc_key, mac_key)``，各 32 字节。
    """
    enc_key = hashlib.pbkdf2_hmac(
        "sha512", key_material, salt, PBKDF2_ROUNDS, dklen=KEY_SIZE
    )
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac(
        "sha512", enc_key, mac_salt, MAC_ROUNDS, dklen=KEY_SIZE
    )
    return enc_key, mac_key


def parse_key(key_hex: str) -> bytes:
    """把 ``WCDB_KEY``（64 位 hex）解析成 32 字节密钥素材。

    实测微信侧导出的就是可直接使用的 enc_key，无需额外变换。
    """
    key_hex = (key_hex or "").strip()
    if len(key_hex) != 64:
        raise DecryptError(f"密钥长度应为 64 位 hex，实际 {len(key_hex)} 位")
    try:
        return bytes.fromhex(key_hex)
    except ValueError as exc:
        raise DecryptError(f"密钥不是合法的 hex 字符串: {exc}") from exc


def page_hmac(page: bytes, mac_key: bytes, page_no: int) -> bytes:
    """计算某一页的 HMAC-SHA512 校验值。

    第 1 页开头有 16 字节的 ``SQLite format 3\\0`` 文件头，不参与校验；
    数据段截止到 reserve 区开始处（含 IV）。
    """
    offset = SALT_SIZE if page_no == 1 else 0
    data_end = PAGE_SIZE - RESERVE_SIZE + IV_SIZE
    mac = hmac.new(mac_key, page[offset:data_end], hashlib.sha512)
    mac.update(struct.pack("<I", page_no))
    return mac.digest()


def verify_page(page: bytes, mac_key: bytes, page_no: int) -> bool:
    """校验某一页的 HMAC 是否正确。"""
    if len(page) < PAGE_SIZE:
        return False
    data_end = PAGE_SIZE - RESERVE_SIZE + IV_SIZE
    expected = page[data_end:data_end + HMAC_SIZE]
    return hmac.compare_digest(page_hmac(page, mac_key, page_no), expected)


def decrypt_page(page: bytes, enc_key: bytes, page_no: int) -> bytes:
    """解密单页，返回 4096 字节明文页（标准 SQLite 页布局）。"""
    from Crypto.Cipher import AES

    if len(page) < PAGE_SIZE:
        page = page + b"\x00" * (PAGE_SIZE - len(page))

    offset = SALT_SIZE if page_no == 1 else 0
    iv = page[PAGE_SIZE - RESERVE_SIZE: PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
    ciphertext = page[offset: PAGE_SIZE - RESERVE_SIZE]
    plain = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(ciphertext)

    body = bytearray()
    if page_no == 1:
        body += SQLITE_HEADER
    body += plain
    if len(body) < PAGE_SIZE:
        body += b"\x00" * (PAGE_SIZE - len(body))
    return bytes(body[:PAGE_SIZE])


def encrypt_page(plain_page: bytes, enc_key: bytes, page_no: int,
                 salt: bytes = b"", mac_key: bytes = b"") -> bytes:
    """把明文页加密回 SQLCipher 页（写回场景与测试用）。

    注意第 1 页：明文页开头的 16 字节是 ``SQLite format 3\\0`` 文件头，
    它**在加密页里应当被 salt 取代**（salt 是随机字节，不是文件头）。
    因此不能用 ``plain_page[:16]`` 当 salt，必须由调用方显式传入。

    Args:
        plain_page: 4096 字节明文页。
        enc_key: 加密密钥。
        page_no: 页号（从 1 开始）。
        salt: 第 1 页要写在前 16 字节的 salt；不传则填零。
        mac_key: 传入则计算并写入页尾 HMAC；不传则 HMAC 区留零。
    """
    from Crypto.Cipher import AES

    if len(plain_page) < PAGE_SIZE:
        plain_page = plain_page + b"\x00" * (PAGE_SIZE - len(plain_page))

    offset = SALT_SIZE if page_no == 1 else 0
    payload = plain_page[offset: PAGE_SIZE - RESERVE_SIZE]
    iv = os.urandom(IV_SIZE)
    ciphertext = AES.new(enc_key, AES.MODE_CBC, iv).encrypt(payload)

    out = bytearray()
    if page_no == 1:
        out += (salt or b"\x00" * SALT_SIZE)[:SALT_SIZE]
    out += ciphertext
    out += iv
    out += b"\x00" * HMAC_SIZE
    page = bytes(out[:PAGE_SIZE])

    if mac_key:
        mac = page_hmac(page, mac_key, page_no)
        page = page[: PAGE_SIZE - HMAC_SIZE] + mac
    return page


# ── 密钥校验 ─────────────────────────────────────────────────────────

def verify_key(key_material: bytes, db_path: Path) -> bool:
    """用数据库首页的 HMAC 校验密钥是否正确。"""
    try:
        with open(db_path, "rb") as fh:
            page1 = fh.read(PAGE_SIZE)
    except OSError as exc:
        raise DecryptError(f"读取数据库失败: {exc}") from exc

    if len(page1) < PAGE_SIZE:
        return False
    if page1[:SALT_SIZE] == SQLITE_HEADER[:SALT_SIZE]:
        # 已经是明文库
        return True

    salt = page1[:SALT_SIZE]
    _, mac_key = derive_keys(key_material, salt)
    return verify_page(page1, mac_key, 1)


def is_plaintext_db(path: Path) -> bool:
    """判断文件是否已是明文 SQLite 库。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    return head == SQLITE_HEADER


# ── WAL 合并 ─────────────────────────────────────────────────────────

def _parse_wal_frames(wal_path: Path, enc_key: bytes, mac_key: bytes):
    """解析并解密 WAL 中的每一帧，产出 ``(页号, 明文页)``。

    SQLCipher 库的 WAL 帧头是明文的（SQLite 自己管理），页数据部分加密，
    因此可以逐帧解密后覆盖到主库对应页上，得到"含最新写入"的快照。

    关键点：WAL 文件里会残留**上一个写入周期**的帧，它们与本周期 WAL 头的
    salt 不匹配。只有 salt 匹配的帧才是有效的，遇到不匹配必须**立即停止**
    （不能跳过继续扫），否则旧帧会覆盖掉主库里的新数据，导致数据回退。
    """
    try:
        data = wal_path.read_bytes()
    except OSError:
        return

    if len(data) < WAL_HEADER_SIZE:
        return

    magic = struct.unpack_from(">I", data, 0)[0]
    if magic not in (WAL_MAGIC_LE, WAL_MAGIC_BE):
        logger.debug("WAL %s 魔数不匹配 (0x%x)，跳过", wal_path.name, magic)
        return

    wal_page_size = struct.unpack_from(">I", data, 8)[0]
    if wal_page_size != PAGE_SIZE:
        logger.warning(
            "WAL %s 页大小 %d 与预期 %d 不符，跳过合并",
            wal_path.name, wal_page_size, PAGE_SIZE,
        )
        return

    # WAL 头的 salt（帧必须与之匹配才有效）
    wal_salt1 = struct.unpack_from(">I", data, 16)[0]
    wal_salt2 = struct.unpack_from(">I", data, 20)[0]

    frame_size = WAL_FRAME_HEADER_SIZE + wal_page_size
    offset = WAL_HEADER_SIZE
    total = len(data)

    # SQLite 保证每个页在 WAL 中按写入顺序出现；同一页号后写覆盖先写。
    # 按帧顺序 yield，由调用方顺序覆盖即可得到最终状态。
    while offset + frame_size <= total:
        page_no = struct.unpack_from(">I", data, offset)[0]
        frame_salt1 = struct.unpack_from(">I", data, offset + 8)[0]
        frame_salt2 = struct.unpack_from(">I", data, offset + 12)[0]

        if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
            # 进入上一个周期的残留帧，其后的内容一律无效
            logger.debug(
                "WAL %s 在第 %d 帧处 salt 不匹配，停止合并（已处理 %d 帧）",
                wal_path.name, (offset - WAL_HEADER_SIZE) // frame_size,
                (offset - WAL_HEADER_SIZE) // frame_size,
            )
            return

        page_data = data[offset + WAL_FRAME_HEADER_SIZE: offset + frame_size]
        if page_no > 0:
            try:
                plain = decrypt_page(page_data, enc_key, page_no)
            except Exception as exc:            # noqa: BLE001
                logger.debug("WAL 第 %d 页解密失败: %s", page_no, exc)
            else:
                yield page_no, plain
        offset += frame_size


def merge_wal(enc_key: bytes, mac_key: bytes, main_path: Path,
              wal_path: Path, out: bytearray) -> int:
    """把 WAL 中的页覆盖到已解密的主库数据上，返回应用页数。"""
    applied = 0
    for page_no, plain in _parse_wal_frames(wal_path, enc_key, mac_key):
        start = (page_no - 1) * PAGE_SIZE
        end = start + PAGE_SIZE
        if start < 0:
            continue
        if end > len(out):
            out.extend(b"\x00" * (end - len(out)))
        out[start:end] = plain
        applied += 1
    return applied


def _replace_with_retry(tmp: Path, dst: Path, attempts: int = 5,
                        delay: float = 0.2) -> None:
    """原子替换明文库，对 Windows 的"目标被占用"做短暂重试。

    ``os.replace`` 在目标文件被其他句柄打开时抛 ``PermissionError``
    （WinError 5）——例如另一个进程、杀软扫描，或同进程里另一处打开的快照。
    这类占用常常是瞬时的，重试几次即可成功；仍失败则抛出，由调用方决定
    是降级沿用旧快照还是向上报错。
    """
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, dst)
            return
        except OSError as exc:                     # WinError 5 / 32
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    try:
        tmp.unlink()
    except OSError:
        pass
    assert last_exc is not None
    raise last_exc


# ── 整库解密 ─────────────────────────────────────────────────────────

def decrypt_database(
    src: Path,
    dst: Path,
    key_hex: str,
    *,
    merge_wal_file: bool = True,
    progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """解密整个数据库文件到 ``dst``（标准明文 SQLite 库）。

    Args:
        src: 加密库路径。
        dst: 输出明文库路径。
        key_hex: ``WCDB_KEY``（64 位 hex）。
        merge_wal_file: 是否把 ``<src>-wal`` 中的新页合并进来。
        progress: 可选进度回调 ``(已完成页数, 总页数)``。

    Returns:
        统计信息 dict：``pages``/``bad_pages``/``wal_pages``/``plaintext``。
    """
    key_material = parse_key(key_hex)

    if is_plaintext_db(src):
        # 已是明文：直接复制（同名 dst 旁可能残留上次打开生成的
        # ``-wal``/``-shm``，同样要清掉，理由见下方 os.replace 处）。
        data = src.read_bytes()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(str(dst) + suffix)
            except OSError:
                pass
        return {
            "pages": len(data) // PAGE_SIZE,
            "bad_pages": 0,
            "wal_pages": 0,
            "plaintext": True,
        }

    file_size = src.stat().st_size
    if file_size < PAGE_SIZE:
        raise DecryptError(f"数据库文件过小: {src} ({file_size} 字节)")

    # 先读首页：拿 salt 并校验密钥，避免为错误密钥解完整个库
    with open(src, "rb") as fh:
        page1 = fh.read(PAGE_SIZE)
    salt = page1[:SALT_SIZE]
    enc_key, mac_key = derive_keys(key_material, salt)

    if not verify_page(page1, mac_key, 1):
        raise KeyMismatchError(
            f"密钥与数据库不匹配: {src.name}（首页 HMAC 校验失败）"
        )

    total_pages = (file_size + PAGE_SIZE - 1) // PAGE_SIZE
    # 预分配输出：避免逐页 extend 造成的反复 realloc（大库上会翻倍占用）
    out = bytearray(total_pages * PAGE_SIZE)
    bad_pages = 0

    # 流式分块读取：一次只驻留一个 chunk，内存峰值 ≈ chunk + 输出缓冲，
    # 而不是"原始加密数据 + 输出"两份。
    with open(src, "rb") as fh:
        index = 0
        while index < total_pages:
            chunk = fh.read(_READ_CHUNK)
            if not chunk:
                break
            for offset in range(0, len(chunk), PAGE_SIZE):
                page = chunk[offset:offset + PAGE_SIZE]
                if len(page) < PAGE_SIZE:
                    page = page + b"\x00" * (PAGE_SIZE - len(page))
                page_no = index + 1
                if not verify_page(page, mac_key, page_no):
                    # 微信 4.x 在个别边界页上存在 HMAC 异常，不丢页，仅记录。
                    bad_pages += 1
                start = index * PAGE_SIZE
                out[start:start + PAGE_SIZE] = decrypt_page(page, enc_key, page_no)
                index += 1
                if progress is not None and (index % 5000 == 0 or index == total_pages):
                    progress(index, total_pages)

    wal_pages = 0
    if merge_wal_file:
        wal_path = Path(str(src) + "-wal")
        if wal_path.exists():
            wal_pages = merge_wal(
                enc_key, mac_key, src, wal_path, out
            )

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    # 上次失败可能留下同名 .part（被占用时删不掉，忽略即可）
    try:
        tmp.unlink()
    except OSError:
        pass
    tmp.write_bytes(out)
    _replace_with_retry(tmp, dst)

    # 明文快照保留了微信库的 WAL 模式：上次打开快照会在旁边生成
    # ``-wal``/``-shm``。若上次进程是崩溃退出（未干净关闭），这些残留
    # 会一直留着；只替换主文件的话，SQLite 打开新快照时会把陈旧 WAL
    # 帧重放回去（读到新旧混合的数据）。因此替换后必须一并清掉。
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(str(dst) + suffix)
        except OSError:
            pass

    if bad_pages:
        logger.warning("%s 有 %d 页 HMAC 校验异常（已解密但内容可能不完整）",
                       src.name, bad_pages)

    return {
        "pages": total_pages,
        "bad_pages": bad_pages,
        "wal_pages": wal_pages,
        "plaintext": False,
    }


def decrypt_to_bytes(src: Path, key_hex: str, *, merge_wal_file: bool = True) -> bytes:
    """解密到内存（供少量查询使用）。"""
    tmp = src.with_suffix(src.suffix + ".mem")
    try:
        decrypt_database(src, tmp, key_hex, merge_wal_file=merge_wal_file)
        return tmp.read_bytes()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ── 简易解密缓存（供读取层复用）─────────────────────────────────────

class DecryptCache:
    """按"源文件 mtime + WAL mtime"失效的明文库缓存。

    读取层每次查询前调用 :meth:`ensure`，只有源库或 WAL 发生变化时才会
    重新解密，避免高频轮询下反复做全量解密。
    """

    def __init__(self, cache_dir: Path, key_hex: str):
        self._cache_dir = Path(cache_dir)
        self._key_hex = key_hex
        self._lock = threading.Lock()
        self._stamps: dict[str, tuple] = {}

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def _stamp_of(self, src: Path) -> tuple:
        try:
            st = src.stat()
            main = (st.st_mtime_ns, st.st_size)
        except OSError:
            main = (0, 0)
        wal = src.with_name(src.name + "-wal")
        try:
            wst = wal.stat()
            wal_stamp = (wst.st_mtime_ns, wst.st_size)
        except OSError:
            wal_stamp = (0, 0)
        return (main, wal_stamp)

    def needs_refresh(self, src: Path) -> bool:
        """源库（或 WAL）是否已变化到需要重新解密。"""
        key = str(src)
        dst = self._cache_dir / src.name
        return not (self._stamps.get(key) == self._stamp_of(src)
                    and dst.exists() and dst.stat().st_size > 0)

    def ensure(self, src: Path, *, force: bool = False) -> Path:
        """确保 ``src`` 对应的明文库已就绪，返回明文库路径。

        注意：重新解密会覆盖明文库文件，调用方必须**先关闭**该库上已打开的
        连接（Windows 下被占用的文件无法覆盖）。

        若覆盖仍失败（其他进程/杀软占用了快照，或同进程另有实例），但已存在
        一份可用的旧快照，则**降级沿用旧快照**并告警——宁可数据略旧，也不能
        让整个会话列表/消息读取直接报错。
        """
        with self._lock:
            stamp = self._stamp_of(src)
            key = str(src)
            dst = self._cache_dir / src.name

            if (not force and self._stamps.get(key) == stamp
                    and dst.exists() and dst.stat().st_size > 0):
                return dst

            try:
                stats = decrypt_database(src, dst, self._key_hex)
            except OSError as exc:
                if dst.exists() and dst.stat().st_size > 0:
                    logger.warning(
                        "重新解密 %s 失败（%s），沿用已有一份快照（数据可能略旧）",
                        src.name, exc,
                    )
                    self._stamps[key] = stamp
                    return dst
                raise
            self._stamps[key] = stamp
            logger.debug(
                "解密 %s -> %s (%d 页, WAL %d 页, 异常 %d 页)",
                src.name, dst.name, stats["pages"], stats["wal_pages"], stats["bad_pages"],
            )
            return dst

    def invalidate(self, src: Optional[Path] = None) -> None:
        """让缓存失效（不传则全部失效）。"""
        with self._lock:
            if src is None:
                self._stamps.clear()
            else:
                self._stamps.pop(str(src), None)
