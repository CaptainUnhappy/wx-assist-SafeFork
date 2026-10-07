"""消息 content → 可读文本（入库前的数据清洗）。

微信 4.x 把大量非文本消息的 payload 以 XML 形式塞在 content 里（图片、表情、
语音、视频、位置、名片、邮件、通话、引用回复、聊天记录、链接、转账…），
且外层 msg_type 经常被存成 1，光看类型无法判断。

入库链路（wcdb_backend._standardize）在写 messages.db 前调用本模块，
保证库里存的是"清洗后的数据"，下游（关键词告警 / 摘要 / RAG / 记忆）
不必各自再解析 XML。

设计原则：
1. 只做"去结构、留文本"：人能读的文本（title/des/引用内容/聊天记录条目…）
   全部保留，避免清洗后关键词漏命中；机器字段（aeskey、cdn 地址、msgsource、
   签名）一律丢弃。
2. 覆盖 WEBUI 显示层（api_handlers._extract_system_msg_text / _parse_msg_media）
   已支持的同类消息，标签风格保持一致（[图片] [表情] [邮件] [通话] [位置] [名片]）。
3. 非 XML 文本（含未解压的 hex）原样返回，绝不丢数据；无法识别的 XML 兜底为
   "[未知消息]"，不把原始 XML 写回库里。
"""

import html
import logging
import re

logger = logging.getLogger(__name__)

# 明确媒体类型（local_type）→ 标签，与 WEBUI 显示一致
_MEDIA_LABELS = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]"}

# 消息体内直接承载媒体（外层 msg_type 常被存成 1）→ 标签
_TAG_LABELS = {
    "img": "[图片]",
    "emoji": "[表情]",
    "videomsg": "[视频]",
    "voicemsg": "[语音]",
}

# 兜底提取时允许收集的"人能读的"标签（按文档顺序），其余标签一律丢弃
_TEXT_TAGS = frozenset({
    "title", "des", "desc", "subject", "content", "plain", "template", "nickname",
    "label", "poiname", "feedesc", "pay_memo", "sendertitle",
    "datadesc", "datatitle", "digest", "summary", "sender", "announcement",
})

_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>\s*")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_REFERMSG_RE = re.compile(r"<refermsg[\s\S]*?</refermsg>", re.I)
_REFER_CONTENT_RE = re.compile(r"<content\b[^>]*>", re.I)
_ANY_OPEN_TAG_RE = re.compile(r"<([a-zA-Z0-9_]+)\b([^>]*)>")
# 机器数据：base64/phash/hex 等无空格长串、纯数字
_MACHINE_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=_-]{16,}$")
_PURE_NUMBER_RE = re.compile(r"^\d+$")


def _text(value: str) -> str:
    """去掉 CDATA 包装、反转义 HTML 实体，并把空白折叠成单空格。"""
    if not value:
        return ""
    value = _CDATA_RE.sub(r"\1", value)
    value = html.unescape(value)
    return " ".join(value.split())


def _tag_text(content: str, name: str) -> str:
    """取 <name>…</name> 的文本。

    跳过自闭合的 <name />（同一个 XML 里常同时存在 <content /> 和别的
    <content>…</content>，不跳过会跨标签错配）；用 \\b 防止 <msg> 误匹配
    <msg_type>。
    """
    for m in re.finditer(rf"<{name}\b([^>]*)>(.*?)</{name}>", content, re.I | re.S):
        if m.group(1).rstrip().endswith("/"):
            continue
        return _text(m.group(2))
    return ""


def _attr(content: str, tag: str, name: str) -> str:
    """取 <tag … name="值"> 的属性值。"""
    m = re.search(rf'<{tag}\b[^>]*\b{name}\s*=\s*"([^"]*)"', content, re.I)
    return _text(m.group(1)) if m else ""


def _plain_text(content: str) -> str:
    """彻底去标签：保留所有文本节点（HTML 片段 / 未知 XML 的兜底）。"""
    text = _COMMENT_RE.sub(" ", content)
    return _text(_ANY_TAG_RE.sub(" ", text))


def _readable(value: str) -> str:
    """取一段文本用于展示：转义还原后如果还夹着标签（如 &lt;a href=…&gt;）就去掉标签。"""
    text = _text(value)
    return _plain_text(text) if "<" in text else text


def _human_text(text: str) -> str:
    """丢掉邮件/图片 XML 里夹带的机器数据（base64、phash、纯数字）。"""
    parts = []
    for token in (text or "").split(" "):
        if not token:
            continue
        if _PURE_NUMBER_RE.match(token) or _MACHINE_TOKEN_RE.match(token):
            continue
        parts.append(token)
    return " ".join(parts)


def _harvest_text(content: str) -> str:
    """按文档顺序收集可读标签的文本，去重后以 " | " 拼接。

    只遍历每个"开标签"再配对闭合标签，不整体配对元素 —— 否则最外层的
    <msg>…</msg> 一次就把整个文档吞掉，里面的 <title> 再也不会被看到。
    """
    found: list[str] = []
    lower = content.lower()
    for m in _ANY_OPEN_TAG_RE.finditer(content):
        name = m.group(1).lower()
        if name not in _TEXT_TAGS or m.group(2).rstrip().endswith("/"):
            continue
        close = lower.find(f"</{name}>", m.end())
        if close < 0:
            continue
        text = _readable(content[m.end():close])
        if text and text not in found:
            found.append(text)
    return " | ".join(found)


def _coerce_local_type(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _looks_like_xml(value: str) -> bool:
    """判断是不是 XML（含被 HTML 转义成 &lt; 的那一类）。

    引用消息里被引用的内容可能是转义过的 XML（微信转发消息时常见），
    只看 "<" 会漏，直接当纯文本输出就会把一整段 XML 吐出去。
    """
    head = value.lstrip()
    return head.startswith("<") or head.startswith("&lt;")


def _strip_sender_prefix(value: str) -> str:
    """剥掉被引用正文前面的"发送者:"前缀（群聊原文格式）。

    只有在剥掉后露出的是 XML 时才剥：这样既能识破
    "wxid_xx:\\n&lt;?xml …" 这类转发消息，也不会误伤 "8:00 开会" 这种真实文本。
    """
    m = re.match(r"^[a-zA-Z0-9_@.\-]+:\s*", value or "")
    if m and _looks_like_xml(value[m.end():]):
        return value[m.end():].lstrip()
    return value


def _refer_quote(refer: str) -> tuple[str, int]:
    """取出 <refermsg> 里被引用的正文（可能本身又是图片/链接等一段 XML）。"""
    start = _REFER_CONTENT_RE.search(refer)
    if not start:
        return "", 1
    end = refer.lower().rfind("</content>")  # 取最后一个：被引用正文里可能嵌套 <content>
    if end < start.end():
        return "", 1
    return refer[start.end():end].strip(), _coerce_local_type(_tag_text(refer, "type"))


def _clean_quote(body: str) -> str:
    """引用回复（appmsg type=57）。

    被引用的原文和本次回复的文本都要保留：只留回复文本会让"引用了报单信息"
    这类消息再也匹配不到关键词（原实现里引用内容参与匹配）。
    """
    refer = _REFERMSG_RE.search(body)
    quoted = sender = ""
    if refer:
        refer_xml = refer.group(0)
        raw_quoted, refer_type = _refer_quote(refer_xml)
        raw_quoted = _strip_sender_prefix(raw_quoted)
        sender = _tag_text(refer_xml, "displayname")
        if _looks_like_xml(raw_quoted):
            # 被引用的可能又是图片/链接/聊天记录等一整段 XML（含转义过的）
            quoted = clean_message_content(raw_quoted, refer_type)
        elif refer_type in _MEDIA_LABELS:
            # 引用的是图片/语音等，正文只有二进制地址，没有可读文字
            quoted = _MEDIA_LABELS[refer_type]
        else:
            quoted = _readable(raw_quoted)

    outer = _REFERMSG_RE.sub("", body)
    reply = _tag_text(outer, "des") or _tag_text(outer, "title")

    if quoted and reply:
        head = f"[引用 {sender}]" if sender else "[引用]"
        return f"{head} {quoted} → {reply}"
    if quoted:
        head = f"[引用 {sender}]" if sender else "[引用内容]"
        return f"{head} {quoted}"
    if reply:
        return reply
    return "[引用消息]"


def _clean_chat_records(outer: str) -> str:
    """合并转发的聊天记录（appmsg type=19）。

    <des> 是微信自带的条目摘要，<recorditem> 里还有被转发内容的明细
    （HTML 转义的一段 XML）。两者都收，只丢掉已被前文覆盖的重复条目，
    避免漏掉被转发内容里的关键字。
    """
    parts: list[str] = []

    def add(value: str) -> None:
        for piece in (value or "").split(" | "):
            piece = _readable(piece)
            joined = " | ".join(parts)
            if piece and piece not in joined:
                parts.append(piece)

    add(_tag_text(outer, "title"))
    add(_tag_text(outer, "des"))
    add(_harvest_text(_tag_text(outer, "recorditem")))
    for m in re.finditer(r"<dataitem\b[^>]*>(.*?)</dataitem>", outer, re.I | re.S):
        item = m.group(1)
        add(_tag_text(item, "datadesc") or _tag_text(item, "datatitle"))
    joined = " | ".join(parts)
    return "[聊天记录] " + joined if joined else "[聊天记录]"


def _clean_appmsg(body: str) -> str:
    """应用消息（<appmsg>）。"""
    # <refermsg> 内部自带 <type>，先摘掉，避免把被引用消息的类型当成外层类型
    outer = _REFERMSG_RE.sub("", body)
    appmsg_type = _tag_text(outer, "type")

    if appmsg_type == "57":
        return _clean_quote(body)
    if appmsg_type == "19":
        return _clean_chat_records(outer)

    title = _readable(_tag_text(outer, "title"))
    des = _readable(_tag_text(outer, "des"))

    if appmsg_type in ("6", "74"):
        name = _readable(_tag_text(outer, "filename")) or title
        return f"[文件] {name}" if name else "[文件]"
    if appmsg_type == "8" or "<emoji" in outer[:400].lower():
        return "[表情]"
    if appmsg_type in ("2000", "2001"):
        kind = "红包" if appmsg_type == "2001" else "转账"
        # 备注（pay_memo）是付款人自己写的，常带业务关键字，必须保留
        parts = [p for p in (des or title, _readable(_tag_text(outer, "pay_memo"))) if p]
        return f"[{kind}] " + " | ".join(parts) if parts else f"[{kind}]"

    # 其余 appmsg：按文档顺序收集可读字段（链接文章的标题+摘要、拍一拍、
    # 小程序、音乐、收藏笔记、群公告…）
    return _harvest_text(outer) or "[应用消息]"


def _fill_template_vars(text: str, body: str) -> str:
    """把 sysmsgtemplate 里的 $adder$ 之类占位符换成实际昵称。

    模板本身不含昵称，昵称在同级的 <link name="adder">…<nickname> 里；
    换不出来就保留占位符，不编造。
    """
    def _repl(m):
        name = m.group(1)
        link = re.search(
            rf'<link\b[^>]*\bname\s*=\s*"{re.escape(name)}"[^>]*>(.*?)</link>',
            body, re.I | re.S,
        )
        if not link:
            return m.group(0)
        return _tag_text(link.group(1), "nickname") or m.group(0)

    return re.sub(r"\$([A-Za-z0-9_]+)\$", _repl, text)


def _clean_sysmsg(body: str) -> str:
    """系统消息（<sysmsg>）：撤回 / 群公告 / 置顶 / 入群 / 支付提醒…"""
    if "mmchatroomtopmsg" in body:
        nick = _tag_text(body, "nickname")
        if nick:
            return f"{nick} 置顶了一条消息"
    for name in ("content", "plain"):
        text = _tag_text(body, name)
        if text:
            return _plain_text(text)
    template = _tag_text(body, "template")
    if template:
        return _fill_template_vars(template, body)
    return _harvest_text(body) or "[系统消息]"


def clean_message_content(content: str, local_type=1) -> str:
    """把一条消息的原始 content 清洗成可读文本。

    Args:
        content: WCDB 里取出的 content（调用方需先做 zstd 解压和群聊前缀剥离）。
        local_type: 消息类型（wx 4.x 常是高位编码，仅作为提示）。

    Returns:
        可读文本；非 XML 内容原样返回（可能是纯文本或未解压的 hex）。
    """
    if not content:
        return ""
    content = content.strip()
    if not content:
        return ""

    # 少数消息的 XML 被 HTML 转义后再存（&lt;appmsg …）
    if "&lt;" in content and "<" not in content[:100]:
        unescaped = html.unescape(content)
        if unescaped.lstrip().startswith("<"):
            content = unescaped.strip()

    if not content.lstrip().startswith("<"):
        return content  # 纯文本 / 未解压的 hex：原样保留

    body = _XML_DECL_RE.sub("", content.lstrip())

    label = _MEDIA_LABELS.get(_coerce_local_type(local_type))
    if label:
        return label

    head = body[:400].lower()
    if "<appmsg" in head:
        return _clean_appmsg(body)

    if "<pushmail" in body.lower():
        subject = _readable(_tag_text(body, "subject"))
        digest = _readable(_tag_text(body, "digest"))
        parts = [p for p in (subject, digest) if p]
        return "[邮件] " + " | ".join(parts) if parts else "[邮件]"

    if "<voipmsg" in head or "<voipinvitemsg" in head:
        duration = _tag_text(body, "msg")
        return f"[通话] {duration}" if duration else "[通话]"

    if "<location" in head:
        where = _attr(body, "location", "label") or _attr(body, "location", "poiname")
        return f"[位置] {where}" if where else "[位置]"

    if "<sysmsg" in head:
        return _clean_sysmsg(body)

    # 名片：<msg bigheadimgurl="…" nickname="…">
    nick = _attr(body, "msg", "nickname")
    if nick:
        return f"[名片] {nick}"

    # 图片 / 表情 / 语音 / 视频：体内还有可读文字时保留文字（如"你领取了…红包"），
    # 否则打标签。图片 XML 里常夹带 phash/pdqhash 的 base64，需靠 _human_text 滤掉。
    m = re.search(r"<(img|emoji|videomsg|voicemsg)\b", head, re.I)
    if m:
        text = _human_text(_plain_text(body))
        return text or _TAG_LABELS[m.group(1).lower()]

    text = _harvest_text(body) or _plain_text(body)
    return text or "[未知消息]"
