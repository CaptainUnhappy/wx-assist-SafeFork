"""发版前校验：src/version.py 的 __version__ 必须与 git tag 一致。

用法：
    python scripts/check_version.py          # 与本地最新的 v* tag 对比
    python scripts/check_version.py 1.6.1    # 校验版本号是否就是 1.6.1

背景：
    以前出现过"程序里写着 1.0.1、Release 上却是 v1.6.0"的情况，导致
    "程序自报版本"不可信、自动更新无从比较。发版流程里先跑这个脚本，
    不一致就报错退出，不再靠人肉对齐。
"""
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = PROJECT_ROOT / "src" / "version.py"


def read_app_version() -> str:
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        VERSION_FILE.read_text(encoding="utf-8"),
        re.M,
    )
    if not match:
        sys.exit(f"[FAIL] 在 {VERSION_FILE} 里找不到 __version__")
    return match.group(1)


def git_tags() -> list:
    """按版本号倒序返回本地 v* tag；读不到则返回空列表。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "tag", "-l", "v*", "--sort=-v:refname"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def main() -> int:
    app_version = read_app_version()
    print(f"src/version.py  ->  {app_version}")

    expected = sys.argv[1].lstrip("vV") if len(sys.argv) > 1 else ""
    if expected:
        if app_version != expected:
            print(f"[FAIL] 与期望版本 {expected} 不一致")
            return 1
        print(f"[ OK ] 与期望版本 {expected} 一致")
        return 0

    tags = git_tags()
    if not tags:
        print("[WARN] 读不到 git tag（未安装 git 或不在仓库内），跳过对比")
        return 0

    latest = tags[0].lstrip("vV")
    print(f"最新 git tag    ->  v{latest}")
    if latest != app_version:
        print(
            "[FAIL] 版本号与最新 tag 不一致。两种改法：\n"
            f"       1) 把 src/version.py 改成 \"{latest}\"\n"
            f"       2) 为 {app_version} 打新 tag：git tag v{app_version}"
        )
        return 1

    print("[ OK ] 版本号一致，可以发布")
    return 0


if __name__ == "__main__":
    sys.exit(main())
