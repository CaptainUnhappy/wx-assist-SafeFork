"""一次性迁移脚本：把 messages.db 里历史遗留的 XML 消息清洗成可读文本。

背景：早期入库链路只做了 zstd 解压和群聊前缀剥离，没有把消息体里的 XML
（引用回复/聊天记录/链接/图片/名片/位置/邮件/通话/系统消息…）转成可读文本，
导致库里约 18% 的行是原始 XML：关键词告警推送一坨 XML、摘要/RAG/记忆读到
标签和 base64。入库链路现在会在写入前清洗（src/wechat/msg_text.py），
本脚本把存量数据补齐到同一形态。

幂等性：清洗后内容不再以 "<" 开头，重复执行不会二次处理。

安全性：默认**只统计不写入**，确认影响面后再加 --apply 真正回写。
回写会覆盖 content 列（message_id 不变），建议先备份：
    copy data\\messages.db data\\messages.db.bak-before-cleanxml

用法：
    python scripts/migrate_messages_clean_xml.py                 # 干跑，看影响面
    python scripts/migrate_messages_clean_xml.py --apply         # 真正回写
    python scripts/migrate_messages_clean_xml.py --apply --db 其他路径/messages.db
"""

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

# 允许以源码模式直接运行（scripts/ 在项目根，需能 import src 包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.wechat.msg_text import clean_message_content  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_messages_clean_xml")

# 待处理条件：以 "<" 开头，或 HTML 转义过的 XML（&lt;）
_BATCH_SQL = """SELECT rowid, msg_type, content FROM messages
                WHERE rowid > ? AND (content LIKE '<%' OR content LIKE '&lt;%')
                ORDER BY rowid LIMIT ?"""


def migrate(db_path: str, apply: bool = False, sample: int = 12) -> int:
    """清洗历史 XML 消息，返回内容发生变化（需要回写）的条数。"""
    if not Path(db_path).exists():
        logger.error("数据库不存在: %s", db_path)
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            """SELECT COUNT(*) AS n FROM messages
               WHERE content LIKE '<%' OR content LIKE '&lt;%'"""
        ).fetchone()["n"]
        logger.info("待检查的 XML 消息: %d 条", total)
        if total == 0:
            return 0

        done = changed = 0
        shown = 0
        last_rowid = 0
        cursor = conn.cursor()
        while True:
            rows = cursor.execute(_BATCH_SQL, (last_rowid, 500)).fetchall()
            if not rows:
                break
            for row in rows:
                raw = row["content"] or ""
                cleaned = clean_message_content(raw, row["msg_type"])
                last_rowid = row["rowid"]
                if not cleaned or cleaned == raw:
                    continue
                changed += 1
                if shown < sample:
                    shown += 1
                    logger.info("示例 %d: %r\n            -> %r",
                                shown, raw[:160], cleaned[:160])
                if apply:
                    conn.execute(
                        "UPDATE messages SET content = ? WHERE rowid = ?",
                        (cleaned, row["rowid"]),
                    )
            done += len(rows)
            conn.commit()
            logger.info("已检查 %d/%d (待回写 %d)", done, total, changed)

        if apply:
            logger.info("迁移完成: 回写 %d 条", changed)
        else:
            logger.info("[干跑] 共 %d 条会被清洗（未写入，确认后加 --apply）", changed)
        return changed
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="清洗 messages.db 历史 XML 消息为可读文本")
    parser.add_argument("--db", default="data/messages.db",
                        help="messages.db 路径（默认 data/messages.db）")
    parser.add_argument("--apply", action="store_true",
                        help="真正回写数据库（默认只统计不写入）")
    args = parser.parse_args()

    t0 = time.time()
    n = migrate(args.db, apply=args.apply)
    logger.info("耗时 %.1fs", time.time() - t0)
    sys.exit(0)
