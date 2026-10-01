"""朋友圈数据解析 —— 从 ``SnsTimeLine.content`` 的 XML 还原帖子结构。

数据结构（实测确认）
--------------------
``sns.db`` 的 ``SnsTimeLine`` 只有四列::

    tid            INTEGER   有符号 64 位整数，真实 id 需 & 0xFFFFFFFFFFFFFFFF
    user_name      TEXT      发布者 wxid
    content        TEXT      XML（<SnsDataItem><TimelineObject>...）
    pack_info_buf  BLOB      极小的 protobuf 补充（多为 0a00 空消息）

帖子正文、媒体列表、位置、发布时间**全部在 content 的 XML 里**，
点赞/评论则在 ``SnsMessage_tmp3`` 表（按 ``feed_id`` 关联）。

关键字段路径::

    /SnsDataItem/TimelineObject/id                  → 帖子 id
    /SnsDataItem/TimelineObject/createTime          → 发布秒级时间戳
    /SnsDataItem/TimelineObject/contentDesc         → 正文文本
    /SnsDataItem/TimelineObject/location            → 位置（属性）
    /SnsDataItem/TimelineObject/ContentObject/mediaList/media → 媒体列表
        media/url[@md5 @key @token]                 → 原图
        media/thumb[@key @token]                    → 缩略图
        media/type                                  → 1=图片 2=视频 …

输出的 dict 字段名与旧 DLL 完全一致（含 ``id``/``tid`` 双份、``media``/
``mediaList`` 双份），因此上层 ``api_handlers``/``sns_client`` 无需改动。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# 媒体类型：微信 XML 里 type 1 为图片，2 为视频（与上层约定一致）
_MEDIA_TYPE_IMAGE = "1"
_MEDIA_TYPE_VIDEO = "2"

_UINT64_MASK = 0xFFFFFFFFFFFFFFFF


def normalize_tid(tid: int) -> int:
    """把有符号 tid 还原成真实的 64 位无符号 id。"""
    try:
        value = int(tid)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return value & _UINT64_MASK
    return value


def _text(node) -> str:
    if node is None or node.text is None:
        return ""
    return node.text


def _find(node, path: str):
    """在节点下按相对路径查找，失败返回 None。"""
    if node is None:
        return None
    try:
        return node.find(path)
    except Exception:                              # noqa: BLE001
        return None


def _strip_markup(text: str) -> str:
    """把朋友圈正文里的换行与前后空白整理成单行可读文本。"""
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


def parse_content_xml(content: str) -> dict:
    """解析 ``SnsTimeLine.content`` 的 XML，返回结构化字段。"""
    result = {
        "id": 0,
        "createTime": 0,
        "contentDesc": "",
        "location": "",
        "latitude": 0.0,
        "longitude": 0.0,
        "media": [],
        "postType": 0,
        "isVideo": False,
    }
    if not content:
        return result

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.debug("朋友圈 XML 解析失败: %s", exc)
        return result

    timeline = root.find("TimelineObject")
    if timeline is None:
        timeline = _find(root, ".//TimelineObject")
    if timeline is None:
        return result

    # 帖子 id 与发布时间
    id_text = _text(timeline.find("id")).strip()
    if id_text.isdigit():
        result["id"] = int(id_text)
    create_text = _text(timeline.find("createTime")).strip()
    if create_text.isdigit():
        result["createTime"] = int(create_text)

    # 正文
    result["contentDesc"] = _strip_markup(_text(timeline.find("contentDesc")))

    # 位置
    location_node = timeline.find("location")
    if location_node is not None:
        result["location"] = str(
            location_node.get("poiName")
            or location_node.get("city")
            or ""
        ).strip()
        try:
            result["latitude"] = float(location_node.get("latitude") or 0)
            result["longitude"] = float(location_node.get("longitude") or 0)
        except (TypeError, ValueError):
            pass

    # 媒体列表
    media_node = timeline.find("ContentObject/mediaList")
    if media_node is not None:
        for item in media_node.findall("media"):
            url_node = item.find("url")
            thumb_node = item.find("thumb")
            media_type = _text(item.find("type")).strip() or _MEDIA_TYPE_IMAGE

            entry = {
                "type": media_type,
                "key": "",
                "token": "",
            }
            if url_node is not None:
                entry["url"] = (url_node.text or "").strip()
                entry["key"] = str(url_node.get("key") or "")
                entry["token"] = str(url_node.get("token") or "")
                entry["md5"] = str(url_node.get("md5") or "")
            elif thumb_node is not None:
                entry["url"] = (thumb_node.text or "").strip()
                entry["key"] = str(thumb_node.get("key") or "")
                entry["token"] = str(thumb_node.get("token") or "")

            if thumb_node is not None:
                entry["thumb"] = (thumb_node.text or "").strip()
            entry.setdefault("thumb", entry.get("url", ""))

            entry["imgUrl"] = entry.get("url", "")
            result["media"].append(entry)

            if media_type == _MEDIA_TYPE_VIDEO:
                result["isVideo"] = True

    result["postType"] = 2 if result["isVideo"] else (1 if result["media"] else 0)
    return result


def parse_timeline_row(tid: int, username: str, content: str,
                      pack_info_buf: bytes = b"",
                      display_name: str = "",
                      likes: list | None = None,
                      comments: list | None = None) -> dict:
    """把一行 ``SnsTimeLine`` 组装成与旧 DLL 兼容的 dict。"""
    real_tid = normalize_tid(tid)
    parsed = parse_content_xml(content)

    post_id = parsed["id"] or real_tid
    media = parsed["media"]
    like_list = list(likes or [])
    comment_list = list(comments or [])

    nickname = display_name or username

    return {
        # id 与 tid 必须同时存在（上层两种键都读）
        "id": post_id,
        "tid": post_id,
        "rawTid": real_tid,
        "username": username,
        "nickname": nickname,
        "displayName": nickname,
        "content": parsed["contentDesc"],
        "contentDesc": parsed["contentDesc"],
        "messageContent": parsed["contentDesc"],
        "createTime": parsed["createTime"],
        "create_time": parsed["createTime"],
        "location": parsed["location"],
        "locationName": parsed["location"],
        "latitude": parsed["latitude"],
        "longitude": parsed["longitude"],
        "media": media,
        "mediaList": media,
        "media_list": media,
        "mediaCount": len(media),
        "postType": parsed["postType"],
        "isVideo": parsed["isVideo"],
        "likes": like_list,
        "comments": comment_list,
        "likeCount": len(like_list),
        "commentCount": len(comment_list),
        "source": "local",
        "rawXml": content or "",
        "packInfoSize": len(pack_info_buf or b""),
    }


def parse_comment_row(feed_id: int, from_username: str, from_nickname: str,
                     to_username: str, to_nickname: str, content: str,
                     msg_type: int, create_time: int) -> dict:
    """把 ``SnsMessage_tmp3`` 一行转成点赞/评论条目。

    ``msg_type``：1=点赞，2=评论（与微信本地结构一致）。
    """
    entry = {
        "username": from_username,
        "nickname": from_nickname or from_username,
        "createTime": int(create_time or 0),
        "type": "like" if int(msg_type or 0) == 1 else "comment",
    }
    if int(msg_type or 0) == 2:
        entry["content"] = _strip_markup(content)
        entry["toUsername"] = to_username
        entry["toNickname"] = to_nickname
    return entry
