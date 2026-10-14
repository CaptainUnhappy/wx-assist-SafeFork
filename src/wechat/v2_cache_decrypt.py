"""WeChat V2 cache image transformation.

Transforms V2-format .dat cache files found in WeChat's local storage
(<wxid_dir>/business/favorite/{data,mid,thumb}/).

V2 file structure:
  [6B magic: 07 08 V2 08 07] [4B aes_size LE] [4B xor_size LE] [1B padding]
  [aligned_aes_size bytes AES-128-ECB ciphertext]
  [raw_data (unencrypted)]
  [xor_size bytes XOR-encrypted tail]

AES key derivation (Windows):
  aes_key = MD5(str(uin) + "wxid_" + wxid_base).hexdigest()[:16]
  xor_key = uin & 0xFF

  where uin is extracted from kvcomm filenames:
    $APPDATA/Tencent/xwechat/net/kvcomm/key_0_{uin}_*_input.statistic

AES section alignment:
  aligned_aes_size = aes_size + (16 - aes_size % 16)  if aes_size % 16 != 0
  aligned_aes_size = aes_size + 16                      if aes_size % 16 == 0
  (PKCS7 always adds at least one full padding block)
"""

import hashlib
import logging
import os
import re
import shutil
import struct
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


V2_MAGIC = b'\x07\x08V2\x08\x07'
V1_MAGIC = b'\x07\x08V1\x08\x07'
V1_FIXED_KEY = b'cfcd208495d565ef'  # md5("0")[:16]

# JPEG/PNG/WebP magic for validation
IMAGE_MAGIC = {
    'jpg': (b'\xFF\xD8\xFF',),
    'png': (b'\x89PNG',),
    'gif': (b'GIF8',),
    'webp': (b'RIFF',),  # need secondary check for WEBP
    'wxgf': (b'wxgf',),  # WeChat HEVC container
    'bmp': (b'BM',),
}


def _read_dat_file(file_path, max_retries=3):
    """Read a .dat file with retry and copy-to-temp fallback for locked files.

    WeChat may lock .dat files while running, causing PermissionError.
    This helper retries with increasing delay, then falls back to
    copying the file to a temp location before reading.
    """
    for attempt in range(max_retries):
        try:
            with open(file_path, 'rb') as f:
                return f.read()
        except PermissionError:
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            # Fallback: copy to temp then read
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix='.dat') as tmp:
                    shutil.copy2(file_path, tmp.name)
                    with open(tmp.name, 'rb') as f:
                        data = f.read()
                    os.unlink(tmp.name)
                    return data
            except Exception:
                return None
        except FileNotFoundError:
            return None
    return None


def _aligned_aes_block_size(aes_size: int) -> int:
    """PKCS7-aligned AES section size.

    Same formula as wechat-decrypt/decode_image.py:aligned_aes_block_size().
    When aes_size is already a multiple of 16, PKCS7 still adds a full
    16-byte padding block, so we add 16.
    """
    if aes_size % 16:
        return aes_size + (16 - aes_size % 16)
    return aes_size + 16


def _detect_image_format(header: bytes) -> str:
    """Detect image format from decrypted header bytes."""
    if len(header) < 4:
        return 'bin'
    if header[:3] == b'\xFF\xD8\xFF':
        return 'jpg'
    if header[:4] == b'\x89PNG':
        return 'png'
    if header[:3] == b'GIF':
        return 'gif'
    if header[:4] == b'RIFF' and len(header) >= 12 and header[8:12] == b'WEBP':
        return 'webp'
    if header[:4] == b'wxgf':
        return 'hevc'
    if header[:2] == b'BM':
        return 'bmp'
    return 'bin'


def decrypt_v2_cache(data: bytes, aes_key: str, xor_key: int,
                     quiet: bool = False) -> Optional[bytes]:
    """Decrypt a V2-format cache file from raw bytes.

    Args:
        data: Raw file bytes (including V2 header).
        aes_key: 16-char ASCII AES key string.
        xor_key: Single-byte XOR key (0-255).
        quiet: True 时把"解密失败"降级为 debug 日志（密钥验证探针
            会批量试错，逐条 WARNING 会刷屏）。

    Returns:
        Decrypted image bytes, or None on failure.
    """
    if len(data) < 15:
        return None

    sig = data[:6]
    if sig not in (V2_MAGIC, V1_MAGIC):
        return None

    # V1 uses fixed key
    if sig == V1_MAGIC:
        aes_key = V1_FIXED_KEY.decode('ascii')

    aes_size, xor_size = struct.unpack_from('<LL', data, 6)
    aligned = _aligned_aes_block_size(aes_size)

    offset = 15
    if offset + aligned > len(data):
        return None

    # AES-128-ECB decrypt
    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Padding

        key_bytes = aes_key.encode('ascii')[:16]
        if len(key_bytes) < 16:
            return None

        cipher = AES.new(key_bytes, AES.MODE_ECB)
        aes_ciphertext = data[offset:offset + aligned]
        decrypted_aes = Padding.unpad(cipher.decrypt(aes_ciphertext), AES.block_size)
    except (ValueError, KeyError) as e:
        # unpad 失败 = 密钥不匹配或数据损坏。这是最高价值的诊断信号，
        # 若密钥推导错误会导致所有 V2 图片静默解密失败。
        if quiet:
            logger.debug("V2 cache decrypt failed (verification probe): %s", e)
        else:
            logger.warning("V2 cache decrypt failed (bad key or corrupt data): %s", e)
        return None

    offset += aligned

    # Raw (unencrypted) middle section
    raw_end = len(data) - xor_size
    raw_data = data[offset:raw_end] if offset < raw_end else b''

    # XOR tail section
    xor_data = bytes(b ^ xor_key for b in data[raw_end:])

    result = decrypted_aes + raw_data + xor_data

    # Validate decryption succeeded
    fmt = _detect_image_format(result[:16])

    # Check for video formats (MP4, WebM, etc.) — not in _detect_image_format
    is_video = (len(result) >= 8 and result[4:8] == b'ftyp')  # MP4: ftyp box

    if fmt == 'bin' and not is_video:
        return None  # Key is likely wrong

    # Additional tail validation for JPEG/PNG (skip for video)
    if not is_video:
        if fmt == 'jpg' and len(result) >= 2 and result[-2:] != b'\xFF\xD9':
            return None
        if fmt == 'png' and b'IEND' not in result[-12:]:
            return None

    return result


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def extract_uin_from_kvcomm() -> Optional[int]:
    """Extract WeChat uin from kvcomm statistic filenames.

    Scans $APPDATA/Tencent/xwechat/net/kvcomm/ (and net_1/) for files
    containing uin patterns, then validates via XOR key voting against
    actual V2 .dat file tail bytes.

    The uin can appear in multiple filename patterns:
      key_0_{uin}_..._input.statistic       (appid=0, uin in 2nd slot)
      key_{uin}_{appid}_..._input.statistic (uin in 1st slot)
      monitordata_{uin}_...                 (uin in 1st slot)
      monitordata_0_...                     (no uin)

    Returns the validated uin as int, or None if not found.
    """
    appdata = os.environ.get('APPDATA', '')
    if not appdata:
        return None

    # Search both net/ and net_1/ directories
    candidate_dirs = [
        Path(appdata) / 'Tencent' / 'xwechat' / 'net' / 'kvcomm',
        Path(appdata) / 'Tencent' / 'xwechat' / 'net_1' / 'kvcomm',
    ]

    # Collect all candidate uins from filenames
    # Pattern 1: key_0_{uin}_... or monitordata_0_... → uin is 2nd number
    pattern_uin_slot2 = re.compile(r'key_0_(\d+)_')
    # Pattern 2: key_{uin}_{appid}_... → uin is 1st number (must be > 100000000)
    pattern_uin_slot1 = re.compile(r'key_(\d{9,11})_\d+_')
    # Pattern 3: monitordata_{uin}_... → uin is 1st number
    pattern_monitor_uin = re.compile(r'monitordata_(\d{9,11})_')

    candidate_uins = set()

    for kvcomm_dir in candidate_dirs:
        if not kvcomm_dir.exists():
            continue
        for f in kvcomm_dir.iterdir():
            if not f.is_file():
                continue
            name = f.name
            # Slot 2: key_0_{uin}_*
            for m in pattern_uin_slot2.finditer(name):
                val = int(m.group(1))
                if val > 100000000:  # Plausible uin
                    candidate_uins.add(val)
            # Slot 1: key_{uin}_{appid}_*
            for m in pattern_uin_slot1.finditer(name):
                candidate_uins.add(int(m.group(1)))
            # Monitor: monitordata_{uin}_*
            for m in pattern_monitor_uin.finditer(name):
                candidate_uins.add(int(m.group(1)))

    if not candidate_uins:
        return None

    # Validate: the correct uin's xor_key = uin & 0xFF should match
    # the majority vote from V2 .dat file tail bytes (JPEG ends with FF D9)
    fav_cache_dir = _find_fav_cache_dir()
    xor_from_files = _vote_xor_key_from_dat_files(fav_cache_dir)

    if xor_from_files is not None:
        # Pick the uin whose xor_key matches the file-derived value
        for uin in candidate_uins:
            if (uin & 0xFF) == xor_from_files:
                return uin

    # Fallback: return the largest plausible uin (often correct for multi-account)
    return max(candidate_uins)


def _find_fav_cache_dir() -> Optional[Path]:
    """Find the favorite cache directory from WECHAT_DATA_DIR."""
    data_dir = os.getenv('WECHAT_DATA_DIR', '')
    if not data_dir:
        return None
    data_path = Path(data_dir)
    if not data_path.exists():
        return None

    # Find the most recently used account directory
    # （结构识别，不依赖 wxid_ 前缀，自定义微信号账号同样支持）
    from src.wechat.account_paths import list_account_dirs
    account_dirs = list_account_dirs(data_path)
    if account_dirs:
        fav_dir = account_dirs[0] / 'business' / 'favorite'
        if fav_dir.exists():
            return fav_dir
    return None


def _vote_xor_key_from_dat_files(fav_dir: Optional[Path],
                                  sample: int = 32,
                                  min_samples: int = 3) -> Optional[int]:
    """Derive XOR key from V2 .dat file tail bytes (JPEG EOI = FF D9).

    For each V2 .dat file, the last 2 bytes XORed with 0xFF, 0xD9
    should yield the same xor_key if the file is a JPEG thumbnail.
    Takes majority vote across samples.
    """
    if not fav_dir or not fav_dir.exists():
        return None

    v2_magic = V2_MAGIC
    tail_votes: dict[int, int] = {}

    # Check thumb/ directory (most likely JPEG thumbnails)
    thumb_dir = fav_dir / 'thumb'
    if not thumb_dir.exists():
        # Try data/ as fallback
        thumb_dir = fav_dir / 'data'

    if not thumb_dir.exists():
        return None

    count = 0
    for f in thumb_dir.rglob('*'):
        if not f.is_file() or f.stat().st_size < 20:
            continue
        try:
            raw = _read_dat_file(str(f))
            if raw and len(raw) >= 8:
                head = raw[:6]
                tail = raw[-2:]
            else:
                continue
            if head == v2_magic and len(tail) == 2:
                xor_key = tail[0] ^ 0xFF
                check = tail[1] ^ 0xD9
                if xor_key == check:
                    tail_votes[xor_key] = tail_votes.get(xor_key, 0) + 1
                    count += 1
        except (OSError, ValueError):
            continue
        if count >= sample:
            break

    if not tail_votes:
        return None

    return max(tail_votes, key=tail_votes.get)


def derive_v2_keys(uin: int, wxid: str) -> tuple[str, int]:
    """Derive V2 AES key and XOR key from uin and wxid.

    Formula (Windows WeChat 4.x):
      aes_key = MD5(str(uin) + "wxid_" + wxid_base).hexdigest()[:16]
      xor_key = uin & 0xFF

    where wxid_base = wxid without the trailing _XXXX hex suffix.
    e.g. "wxid_abc123def_89ce" → "wxid_abc123def"

    Returns:
        (aes_key_str, xor_key_int)
    """
    # Strip trailing _XXXX suffix from wxid
    wxid_base = wxid.rsplit('_', 1)[0] if '_' in wxid else wxid

    # MD5(str(uin) + "wxid_" + wxid_base) - wxid_base already has "wxid_" prefix!
    key_input = f'{uin}{wxid_base}'
    aes_key = hashlib.md5(key_input.encode()).hexdigest()[:16]
    xor_key = uin & 0xFF

    return aes_key, xor_key


def _derive_keys_from_base(uin: int, wxid_base: str) -> tuple[str, int]:
    """由"最终推导输入"直接生成密钥（不再剥目录名后缀）。

    公式与 :func:`derive_v2_keys` 一致：aes = MD5(str(uin) + wxid_base)[:16]，
    xor = uin & 0xFF。注意：候选输入已是最终形式（如 Name2Id 里的内部
    wxid），不能再次走 derive_v2_keys 的 rsplit 剥后缀，否则会把
    ``wxid_xxx`` 错剥成 ``wxid``。
    """
    aes_key = hashlib.md5(f'{uin}{wxid_base}'.encode()).hexdigest()[:16]
    return aes_key, uin & 0xFF


def _v2_key_works(sample: bytes, aes_key: str, xor_key: int) -> bool:
    """用真实 V2 样本验证密钥：能解出合法图片/视频数据才算有效。

    验证探针会批量试错（可能上百个候选），失败走 debug 日志避免刷屏。
    """
    if not sample or not aes_key:
        return False
    try:
        return decrypt_v2_cache(sample, aes_key, xor_key, quiet=True) is not None
    except Exception:                          # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Cache Manager (singleton)
# ---------------------------------------------------------------------------

class V2CacheManager:
    """Manages V2 decryption keys for multiple WeChat accounts.

    Thread-safe singleton. Keys are auto-derived from kvcomm filenames
    on first access and cached in memory.
    """

    _instance = None
    _lock = threading.Lock()
    _detect_lock = threading.Lock()  # protects _detect_keys() from concurrent execution

    def __init__(self, wechat_data_dir: str):
        self._data_dir = Path(wechat_data_dir)
        self._keys: dict[str, tuple[str, int]] = {}
        self._uin: Optional[int] = None
        self._detected = False

    @classmethod
    def get_instance(cls, wechat_data_dir: str = '') -> 'V2CacheManager':
        with cls._lock:
            data_dir = wechat_data_dir or os.getenv('WECHAT_DATA_DIR', '')
            if cls._instance is None:
                cls._instance = cls(data_dir)
            elif data_dir and not cls._instance._data_dir_valid():
                # Rebuild if existing instance was created with an invalid path
                # (e.g. WECHAT_DATA_DIR wasn't set yet on first call)
                logger.info("V2CacheManager: rebuilding with valid data_dir=%s (old was %s)",
                            data_dir, cls._instance._data_dir)
                # Clean up old instance state before replacing
                if cls._instance is not None:
                    cls._instance._keys = {}
                    cls._instance._detected = False
                cls._instance = cls(data_dir)
            return cls._instance

    def _data_dir_valid(self) -> bool:
        """Check if the current data_dir points to an existing directory."""
        try:
            return self._data_dir.exists() and self._data_dir.is_dir()
        except Exception:
            return False

    def _detect_keys(self):
        """Auto-detect uin from kvcomm and derive keys for all accounts.

        Thread-safe: uses _detect_lock to prevent concurrent key detection.
        Double-checks _detected flag after acquiring lock.
        """
        if self._detected:
            return
        with self._detect_lock:
            if self._detected:  # double-check after acquiring lock
                return

            self._detected = True
            uin = extract_uin_from_kvcomm()
            if uin is None:
                return

            self._uin = uin

            # Derive keys for each account directory.
            # 目录识别不依赖 wxid_ 前缀（自定义微信号账号同样支持）；
            # 密钥推导对每个账号目录走"候选 + 真实 V2 文件验证"闭环。
            if self._data_dir.exists():
                from src.wechat.account_paths import list_account_dirs
                for d in list_account_dirs(self._data_dir):
                    if not d.name.startswith('wxid_'):
                        logger.info(
                            "V2 keys: account dir %r is not wxid_-prefixed "
                            "(custom wechat id) — using candidate+verify "
                            "resolution", d.name)
                    try:
                        self._keys[d.name] = self._resolve_keys_for_account(d, uin)
                    except Exception as exc:       # noqa: BLE001
                        logger.debug("V2 key resolution failed for %s: %s",
                                     d.name, exc)
                        self._keys[d.name] = derive_v2_keys(uin, d.name)

    def _resolve_keys_for_account(self, account_dir: Path,
                                  uin: int) -> tuple[str, int]:
        """为单个账号目录解析 V2 密钥（候选 + 真实 V2 文件验证闭环）。

        兼容性设计（对现有 wxid_ 账号零影响）：

        - 首选候选 = 目录名剥 ``_XXXX`` 后缀，与旧逻辑的推导输入完全一致，
          验证通过（或无法验证）时结果与旧行为相同；
        - 只有当首选候选验证失败、且其他候选验证成功时才改变结果——
          即验证闭环只可能把"解不出图"修正为"能解出图"，反之不可能；
        - 找不到 V2 样本文件时直接返回旧推导结果，行为不变。

        候选来源见 :meth:`_collect_wxid_candidates`。不做内存扫描。
        """
        dirname = account_dir.name
        default_keys = derive_v2_keys(uin, dirname)

        sample = self._find_v2_sample(account_dir)
        if sample is None:
            return default_keys
        if _v2_key_works(sample, *default_keys):
            return default_keys

        for cand in self._collect_wxid_candidates(account_dir):
            keys = _derive_keys_from_base(uin, cand)
            if keys == default_keys:
                continue
            if _v2_key_works(sample, *keys):
                logger.info(
                    "V2 keys verified via alternative candidate for %s "
                    "(base=%r, non-default key adopted)", dirname, cand)
                return keys

        logger.debug(
            "V2 key verification found no working candidate for %s; "
            "keeping default derivation", dirname)
        return default_keys

    def _find_v2_sample(self, account_dir: Path,
                        max_candidates: int = 24) -> Optional[bytes]:
        """从账号目录的收藏缓存里找一个 V2 样本文件（用于密钥验证）。

        只认 V2 头（V1 用固定密钥 ``md5("0")``，无法用于验证候选）。
        优先 ``thumb/``（文件最小、验证最快），按文件大小升序尝试，
        最多读 ``max_candidates`` 个文件即止。
        """
        fav_dir = Path(account_dir) / 'business' / 'favorite'
        if not fav_dir.is_dir():
            return None
        for sub in ('thumb', 'mid', 'data'):
            base = fav_dir / sub
            if not base.is_dir():
                continue
            try:
                entries = sorted(base.rglob('*'), key=lambda p: p.stat().st_size)
            except OSError:
                continue
            tried = 0
            for f in entries:
                try:
                    if not f.is_file() or f.stat().st_size < 20:
                        continue
                except OSError:
                    continue
                tried += 1
                if tried > max_candidates:
                    break
                raw = _read_dat_file(str(f))
                if raw and raw[:6] == V2_MAGIC:
                    return raw
        return None

    def _collect_wxid_candidates(self, account_dir: Path) -> list:
        """收集密钥推导输入（wxid_base）候选，按优先级去重保序。

        1) 目录名剥 ``_XXXX`` 后缀 —— 旧逻辑的默认推导输入；
        2) 目录名本身（目录无后缀时与 1 相同）；
        3) session.db ``Name2Id`` 表中的内部 wxid —— 自定义微信号账号的
           真实内部 ID 只能从这里获得（参考 WeChatDataAnalysis 的做法，
           已在本机实测：目录 ``wxid_***_d3e3`` → Name2Id 内为无后缀 ID）。
        """
        dirname = account_dir.name
        base = dirname.rsplit('_', 1)[0] if '_' in dirname else dirname
        candidates = [base]
        if dirname != base:
            candidates.append(dirname)
        for wxid in self._name2id_internal_wxids(account_dir):
            if wxid not in candidates:
                candidates.append(wxid)
        return candidates

    def _name2id_internal_wxids(self, account_dir: Path,
                                limit: int = 200) -> list:
        """从 session.db 的 Name2Id 表提取内部 wxid 候选（失败返回空列表）。

        Name2Id 里既有自己也有联系人，无法直接定位"自己"——但候选都会
        经过真实 V2 文件解密验证，误候选不会被采纳，因此宽松收集是安全的。
        上限 200 个：验证探针逐个试解，需要为最坏情况（无候选命中）兜底。
        """
        try:
            session_db = Path(account_dir) / 'db_storage' / 'session' / 'session.db'
            if not session_db.exists():
                return []
            from src.web.api_handlers import get_wcdb_client
            client = get_wcdb_client()
            if not client:
                return []
            rows = client.exec_query('session', db_path=str(session_db),
                                     sql='SELECT user_name FROM Name2Id')
            out = []
            for r in rows or []:
                u = str((r or {}).get('user_name', '') or '').strip()
                if u.startswith('wxid_') and u not in out:
                    out.append(u)
                    if len(out) >= limit:
                        break
            return out
        except Exception as exc:               # noqa: BLE001
            logger.debug("Name2Id wxid candidates unavailable for %s: %s",
                         getattr(account_dir, 'name', account_dir), exc)
            return []

    def get_keys(self, wxid: str) -> Optional[tuple[str, int]]:
        """Get (aes_key, xor_key) for the given wxid.

        Returns None if keys could not be derived.
        """
        self._detect_keys()

        # Direct match
        if wxid in self._keys:
            return self._keys[wxid]

        # Try matching wxid directory (wxid may not include suffix)
        for full_wxid, keys in self._keys.items():
            if full_wxid.startswith(wxid):
                return keys

        return None

    @property
    def uin(self) -> Optional[int]:
        self._detect_keys()
        return self._uin

    def decrypt_fav_image(self, local_id: int, wxid: str,
                          size: str = 'original',
                          fullmd5: str = None,
                          fullsize: int = None) -> Optional[bytes]:
        """Find and decrypt a favorite image from local cache.

        Resolution strategy (in priority order):
        1. fullmd5 + hardlink.db → precise .dat path (best)
        2. <fullsize>/<thumbfullsize> + file size matching (fallback)

        Args:
            local_id: Favorite item's local_id from favorite.db
            wxid: WeChat user ID (full with suffix)
            size: "original" (data/), "mid", or "thumb"
            fullmd5: Explicit fullmd5 (from chat record dataitem, overrides XML lookup)
            fullsize: Explicit fullsize (from chat record dataitem)

        Returns:
            Decrypted image bytes, or None on failure.
        """
        self._detect_keys()
        keys = self.get_keys(wxid)
        if not keys:
            return None

        aes_key, xor_key = keys

        # ── Strategy 1: Use provided fullmd5 OR extract from XML ──────────
        file_path = None
        md5_info = None

        # If fullmd5 is explicitly provided (from chat records), use it directly
        if fullmd5:
            md5_info = fullmd5
            resolved = self._resolve_hardlink(md5_info, wxid)
            if resolved and Path(resolved).exists() and Path(resolved).stat().st_size > 0:
                file_path = resolved

        # Fallback: extract from XML if not provided
        if not file_path and not fullmd5:
            md5_info = self._get_fav_md5_from_xml(local_id, size)
            if md5_info:
                resolved = self._resolve_hardlink(md5_info, wxid)
                if resolved and Path(resolved).exists() and Path(resolved).stat().st_size > 0:
                    file_path = resolved

        # ── Strategy 2: size matching fallback ─────────────────────────
        if not file_path:
            # Use provided fullsize, or extract from XML
            target_size = fullsize if fullsize else None
            if not target_size:
                # Try to get size from XML
                xml_size = self._get_fav_size_from_xml(local_id, size)
                target_size = xml_size
            if target_size:
                file_path = self._find_by_size(local_id, wxid, size, target_size,
                                               has_fullmd5=bool(fullmd5))

        if not file_path:
            return None

        try:
            data = _read_dat_file(file_path)
            if data is None:
                return None

            # Check if it's V2 format
            if data[:6] == V2_MAGIC or data[:6] == V1_MAGIC:
                return decrypt_v2_cache(data, aes_key, xor_key)
            else:
                # Legacy format - return raw
                return data

        except Exception:
            return None

    def decrypt_chat_image(self, fullmd5: str, talker: str,
                           create_time: int = 0,
                           wxid: str = "",
                           size: str = "original") -> Optional[bytes]:
        """Find and decrypt a chat image from MsgAttach directory.

        Chat images are stored as V2-encrypted .dat files under:
          {accountDir}/msg/attach/{sessionMd5}/{yyyy-MM}/Img/{md5}.dat

        Resolution strategy:
          1. Compute sessionMd5 from talker (chatroom or wxid)
          2. Determine year-month from create_time
          3. Look for {fullmd5}.dat (original) or {fullmd5}_t.dat (thumb)
          4. Decrypt using V2 cache keys

        Args:
            fullmd5: Image MD5 from packed_info_data
            talker: Session ID (chatroom or wxid)
            create_time: Message timestamp (used to find year-month directory)
            wxid: WeChat user ID
            size: "original" or "thumb"

        Returns:
            Decrypted image bytes, or None on failure.
        """
        self._detect_keys()

        if not wxid:
            data_dir = os.getenv("WECHAT_DATA_DIR", "")
            if data_dir:
                from src.wechat.account_paths import list_account_dirs
                account_dirs = list_account_dirs(Path(data_dir))
                if account_dirs:
                    wxid = account_dirs[0].name

        if not wxid:
            return None

        keys = self.get_keys(wxid)
        if not keys:
            return None

        aes_key, xor_key = keys

        # Compute session MD5 for directory lookup
        session_md5 = hashlib.md5(talker.encode("utf-8")).hexdigest()

        # Determine year-month directories to search
        search_dirs = []
        if create_time and create_time > 0:
            from datetime import datetime
            try:
                dt = datetime.fromtimestamp(create_time)
                ym = dt.strftime("%Y-%m")
                search_dirs.append(ym)
                # Also check adjacent months (messages near month boundary)
                if dt.day <= 1:
                    prev = datetime.fromtimestamp(create_time - 86400)
                    search_dirs.append(prev.strftime("%Y-%m"))
                elif dt.day >= 28:
                    nxt = datetime.fromtimestamp(create_time + 86400)
                    search_dirs.append(nxt.strftime("%Y-%m"))
            except Exception:
                pass

        # Build base attach directory
        attach_base = self._data_dir / wxid / "msg" / "attach" / session_md5
        if not attach_base.exists():
            # Fallback: try without session MD5 (some images may be in other dirs)
            return None

        # Determine filename suffixes to try
        if size == "thumb":
            suffixes = ["_t", ""]  # Prefer thumbnail, fall back to full
        else:
            # original: try _h (high-res), then full, then _t (fallback)
            suffixes = ["_h", "", "_t"]

        # If no create_time, scan all available year-month dirs
        if not search_dirs:
            try:
                search_dirs = sorted([d.name for d in attach_base.iterdir() if d.is_dir()])
            except Exception:
                return None

        # Search for the .dat file
        # Returns (decrypted_data, is_wxgf) or None
        def _try_decrypt(dat_path):
            if not dat_path.exists() or dat_path.stat().st_size == 0:
                return None
            try:
                data = _read_dat_file(str(dat_path))
                if data is None:
                    return None
                if data[:6] == V2_MAGIC or data[:6] == V1_MAGIC:
                    result = decrypt_v2_cache(data, aes_key, xor_key)
                    if result:
                        return result
                else:
                    # Legacy/unencrypted format
                    return data
            except Exception:
                pass
            return None

        for ym in search_dirs:
            img_dir = attach_base / ym / "Img"
            if not img_dir.exists():
                continue

            for suffix in suffixes:
                dat_path = img_dir / f"{fullmd5}{suffix}.dat"
                result = _try_decrypt(dat_path)
                if result:
                    # wxgf (WeChat HEVC) can't be displayed in browser
                    # When size=original, skip wxgf and try next suffix (_h -> full -> _t)
                    # When size=thumb, also skip wxgf and try next
                    if result[:4] == b"wxgf" and suffix != "_t":
                        # Skip wxgf, try next suffix in the list
                        continue
                    return result

            # If all non-wxgf suffixes failed, return wxgf original as last resort
            for suffix in ["", "_h"]:
                dat_path = img_dir / f"{fullmd5}{suffix}.dat"
                result = _try_decrypt(dat_path)
                if result:
                    return result

        # Fallback: scan all Img directories if targeted search failed
        try:
            for ym_dir in sorted(attach_base.iterdir()):
                if not ym_dir.is_dir():
                    continue
                img_dir = ym_dir / "Img"
                if not img_dir.exists():
                    continue
                for suffix in suffixes:
                    dat_path = img_dir / f"{fullmd5}{suffix}.dat"
                    result = _try_decrypt(dat_path)
                    if result:
                        if result[:4] == b"wxgf" and suffix != "_t":
                            continue
                        return result

                # Last resort: return wxgf if nothing else worked
                for suffix in ["", "_h"]:
                    dat_path = img_dir / f"{fullmd5}{suffix}.dat"
                    result = _try_decrypt(dat_path)
                    if result:
                        return result
        except Exception:
            pass

        return None

    def _get_fav_md5_from_xml(self, local_id: int, size_type: str) -> Optional[str]:
        """Extract image/video MD5 from favorite XML content for hardlink lookup.

        For original images, uses <fullmd5>.
        For thumbnails, uses <thumbfullmd5>.

        Fallback: try get_items(500) first, if JSON parse error, fall back to get_by_id.
        """
        try:
            import re as _re

            if not _readers:
                from src.wechat.v2_cache_decrypt import _get_reader_for_wxid
                data_dir = os.getenv('WECHAT_DATA_DIR', '')
                if not data_dir:
                    return None
                from src.wechat.account_paths import list_account_dirs
                account_dirs = list_account_dirs(Path(data_dir))
                if account_dirs:
                    _get_reader_for_wxid(account_dirs[0].name)

            # Search across all favorite types (not just type=2 images)
            # because videos (type=4) and chat records (type=14) also have fullmd5
            for check_wxid, reader in _readers.items():
                try:
                    if not hasattr(reader, 'get_items'):
                        continue
                    # Strategy 1: batch query (efficient but may trigger JSON truncation)
                    items = reader.get_items(limit=500)
                    for item in items:
                        if str(item.get('local_id')) == str(local_id):
                            content = item.get('content_raw', '')
                            if size_type in ('thumb', 'thumbnail'):
                                match = _re.search(r'<thumbfullmd5>([a-f0-9]{32})</thumbfullmd5>', content)
                            else:
                                match = _re.search(r'<fullmd5>([a-f0-9]{32})</fullmd5>', content)
                            if match:
                                return match.group(1)
                except Exception:
                    # Strategy 2: fallback to single-item query if batch fails
                    try:
                        if hasattr(reader, 'get_by_id'):
                            item = reader.get_by_id(local_id)
                            if item:
                                content = item.get('content_raw', '')
                                if size_type in ('thumb', 'thumbnail'):
                                    match = _re.search(r'<thumbfullmd5>([a-f0-9]{32})</thumbfullmd5>', content)
                                else:
                                    match = _re.search(r'<fullmd5>([a-f0-9]{32})</fullmd5>', content)
                                if match:
                                    return match.group(1)
                    except Exception:
                        continue
            return None
        except Exception:
            return None

    def _resolve_hardlink(self, md5: str, wxid: str) -> Optional[str]:
        """通过 hardlink.db 由 MD5 定位 ``.dat`` 文件（纯 Python 实现）。

        原先调用 ``wcdb_api.dll`` 的 ``wcdb_resolve_image_hardlink``；该 DLL
        已停用，改为查询 ``hardlink.db`` 的 ``image_hardlink_info_v4`` 表，
        再用 ``dir2id`` 还原目录名。
        """
        if not md5:
            return None
        try:
            from src.web.api_handlers import get_wcdb_client

            client = get_wcdb_client()
            if not client:
                return None
            path = client.resolve_image_hardlink(md5)
            return path or None
        except Exception:
            return None

    def _find_by_size(self, local_id: int, wxid: str,
                      size: str = 'original',
                      target_size: int = None,
                      has_fullmd5: bool = False) -> Optional[str]:
        """Fallback: find cache file by matching <fullsize>/<thumbfullsize> + V2 header.

        Args:
            has_fullmd5: True when the caller provided an explicit fullmd5
                (e.g. from chat_records). In this case, skip the last-resort
                thumb/ fallback because returning a random thumbnail for a
                multi-image item is worse than showing nothing.
        """
        fav_dir = self._data_dir / wxid / 'business' / 'favorite'
        if not fav_dir.exists():
            return None

        if target_size is None:
            target_size = self._get_fav_size_from_xml(local_id, size)
        if target_size is None:
            return None

        size_tolerance = 100
        dir_map = {
            'original': ('data', target_size),
            'mid': ('mid', target_size),
            'thumb': ('thumb', target_size),
            'thumbnail': ('thumb', target_size),
        }

        dir_name, expected_size = dir_map.get(size, ('data', target_size))
        target_dir = fav_dir / dir_name

        if not target_dir.exists():
            for alt_dir in ['data', 'mid', 'thumb']:
                alt_path = fav_dir / alt_dir
                if alt_path.exists():
                    target_dir = alt_path
                    break

        for f in target_dir.rglob('*'):
            if not f.is_file():
                continue
            file_size = f.stat().st_size
            if file_size == 0:
                continue
            if abs(file_size - (expected_size + 31)) < size_tolerance:
                return str(f)

        # Last resort: return first file from thumb/
        # Only when no fullmd5 was provided (type=2 single image scenario).
        # When fullmd5 is provided (type=14 chat records), skip last resort
        # because returning a random thumbnail is worse than showing nothing.
        if not has_fullmd5:
            thumb_dir = fav_dir / 'thumb'
            if thumb_dir.exists():
                for f in sorted(thumb_dir.rglob('*'), key=lambda p: p.stat().st_size, reverse=True):
                    if f.is_file() and f.stat().st_size > 1000:
                        return str(f)

        return None

    def _get_fav_size_from_xml(self, local_id: int, size_type: str) -> Optional[int]:
        """Extract image size from favorite XML content.

        Fallback: try get_items(500) first, if JSON parse error, fall back to get_by_id.
        """
        try:
            import sys
            import re as _re

            # Try to get a reader - check cached readers first
            if not _readers:
                from src.wechat.v2_cache_decrypt import _get_reader_for_wxid
                data_dir = os.getenv('WECHAT_DATA_DIR', '')
                if not data_dir:
                    return None

                # Try each account（结构识别，取最近使用的账号目录）
                from src.wechat.account_paths import list_account_dirs
                account_dirs = list_account_dirs(Path(data_dir))
                if account_dirs:
                    wxid = account_dirs[0].name
                    _get_reader_for_wxid(wxid)

            # Search through cached readers (all types, not just type=2)
            for check_wxid, reader in _readers.items():
                try:
                    if not hasattr(reader, 'get_items'):
                        continue
                    # Strategy 1: batch query (efficient but may trigger JSON truncation)
                    items = reader.get_items(limit=500)
                    for item in items:
                        # local_id is stored as string, convert for comparison
                        item_local_id = item.get('local_id')
                        if str(item_local_id) == str(local_id):
                            content = item.get('content_raw', '')
                            if size_type in ('thumb', 'thumbnail'):
                                match = _re.search(r'<thumbfullsize>(\d+)</thumbfullsize>', content)
                                if match:
                                    return int(match.group(1))
                            else:  # original
                                match = _re.search(r'<fullsize>(\d+)</fullsize>', content)
                                if match:
                                    return int(match.group(1))
                except Exception:
                    # Strategy 2: fallback to single-item query if batch fails
                    try:
                        if hasattr(reader, 'get_by_id'):
                            item = reader.get_by_id(local_id)
                            if item:
                                content = item.get('content_raw', '')
                                if size_type in ('thumb', 'thumbnail'):
                                    match = _re.search(r'<thumbfullsize>(\d+)</thumbfullsize>', content)
                                    if match:
                                        return int(match.group(1))
                                else:  # original
                                    match = _re.search(r'<fullsize>(\d+)</fullsize>', content)
                                    if match:
                                        return int(match.group(1))
                    except Exception:
                        continue

            return None

        except Exception:
            return None


# ---------------------------------------------------------------------------
# Shared reader pool (reuse WcdbFavReader instances)
# ---------------------------------------------------------------------------

_reader_lock = threading.Lock()
_readers: dict[str, object] = {}


def _get_reader_for_wxid(wxid: str):
    """Get or create a WcdbFavReader for the given wxid.

    Uses the shared WcdbNativeClient instead of independent DLL lifecycle.
    """
    global _readers

    with _reader_lock:
        if wxid in _readers:
            return _readers[wxid]

        try:
            from src.wechat.wcdb_fav_reader import WcdbFavReader
            from src.web.api_handlers import get_wcdb_client

            client = get_wcdb_client()
            if not client:
                return None

            reader = WcdbFavReader(client)
            _readers[wxid] = reader
            return reader

        except Exception:
            return None


def reset_reader_pool() -> None:
    """清空共享 reader 池。

    池里的 ``WcdbFavReader`` 都持有同一个 WCDB 客户端引用。Bot 停止时会关闭
    该客户端（``reset_wcdb_client``），此时池中条目就变成"持有已关闭客户端"的
    死引用，再被复用会抛 ``RuntimeError: 读取器未初始化``。因此必须在关闭
    客户端的同时清空这里，让下次调用重新构建。
    """
    with _reader_lock:
        _readers.clear()
