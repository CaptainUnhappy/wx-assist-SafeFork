"""消息 content 清洗（入库层）的单元测试。

样本全部是**按微信 payload 结构手工构造的合成数据**（图片/表情/引用/聊天记录/
链接/名片/邮件/通话/位置/转账/群系统消息），不含任何真实聊天内容。
断言保证清洗后：可读、不泄漏 XML、不丢人能读的文本（关键字匹配才不会漏）。
"""
import hashlib
from unittest.mock import Mock

import pytest

from src.wechat.msg_text import clean_message_content
from src.wechat.wcdb_backend import WcdbBackend


@pytest.mark.parametrize("content,local_type,expected", [
    # 纯文本原样保留（短文本、以数字/冒号开头的文本都不能被误伤）
    ("8:00 开会", 1, "8:00 开会"),
    ("", 1, ""),
    # 图片 / 表情 / 语音 / 视频：按类型打标签
    ('<?xml version="1.0"?>\n<msg>\n\t<img aeskey="aa" cdnthumburl="0011"/>\n</msg>', 1, "[图片]"),
    ("<msg><emoji md5=\"aa\" len=\"100\" /></msg>", 1, "[表情]"),
    ("<msg><voicemsg voicelength=\"1000\" /></msg>", 1, "[语音]"),
    ('<?xml version="1.0"?>\n<msg>\n\t<videomsg aeskey="aa"/>\n</msg>', 1, "[视频]"),
    ("<msg><img aeskey=\"aa\"/></msg>", 3, "[图片]"),
    # 图片 XML 里夹带的 phash base64 / 纯数字不能当正文
    ('<msg>\n\t<img aeskey="aa"/>\n\teyJwaGFzaCI6IjAwMDAwMDAwMDAwMDAwMDAifQ== 0 0 0 0 0\n</msg>', 1, "[图片]"),
    # 图片类系统提示（红包）里的文字要保留
    ('<img src="SystemMessages_HongbaoIcon.png"/>  你领取了甲的'
     '<_wc_custom_link_ href="weixin://x">转账</_wc_custom_link_>', 1, "你领取了甲的 转账"),
    # 引用回复：被引用原文 + 本次回复都要留（引用里常是要监控的报单信息）
    ('<msg><appmsg><title>示例回复</title><type>57</type><refermsg><type>1</type>'
     '<displayname>张三</displayname><content>示例报价 100.5 元</content>'
     '</refermsg></appmsg></msg>', 1,
     "[引用 张三] 示例报价 100.5 元 → 示例回复"),
    # 引用的是图片时，不能把被引用的 XML 原样吐出来
    ('<msg><appmsg><title>示例回复</title><type>57</type><refermsg><type>3</type>'
     '<displayname>李四</displayname><content><?xml version="1.0"?><msg><img aeskey="aa"/>'
     '</msg></content></refermsg></appmsg></msg>', 1, "[引用 李四] [图片] → 示例回复"),
    # 被引用的是 HTML 转义过的 XML（转发消息常见），同样不能原样吐出来
    ('<msg>\n\t<appmsg>\n\t\t<title>1</title>\n\t\t<type>57</type>\n\t\t<refermsg>\n'
     '\t\t\t<type>49</type>\n\t\t\t<displayname>王五</displayname>\n'
     '\t\t\t<content>&lt;?xml version="1.0"?&gt;\n&lt;msg&gt;\n\t&lt;appmsg&gt;\n'
     '\t\t&lt;title&gt;示例标题&lt;/title&gt;\n\t\t&lt;des&gt;示例摘要&lt;/des&gt;\n'
     '\t\t&lt;type&gt;5&lt;/type&gt;\n\t&lt;/appmsg&gt;\n&lt;/msg&gt;</content>\n'
     '\t\t</refermsg>\n\t</appmsg>\n</msg>', 1,
     "[引用 王五] 示例标题 | 示例摘要 → 1"),
    # 聊天记录：标题 + 摘要 + 被转发内容明细
    ('<msg><appmsg><title>群聊的聊天记录</title><des>张三: 你好</des><type>19</type>'
     '<recorditem>&lt;recordinfo&gt;&lt;desc&gt;张三: 你好&lt;/desc&gt;&lt;datalist&gt;'
     '&lt;dataitem&gt;&lt;datadesc&gt;你好&lt;/datadesc&gt;&lt;datatitle&gt;示例文章&lt;/datatitle&gt;'
     '&lt;/dataitem&gt;&lt;/datalist&gt;&lt;/recordinfo&gt;</recorditem>'
     '</appmsg></msg>', 1, "[聊天记录] 群聊的聊天记录 | 张三: 你好 | 示例文章"),
    # 链接类 appmsg：标题 + 摘要
    ('<?xml version="1.0"?>\n<msg>\n\t<appmsg appid="" sdkver="0">\n'
     '\t\t<title>示例文章标题</title>\n\t\t<des>示例文章摘要</des>\n'
     '\t\t<type>5</type>\n\t\t<content />\n'
     '\t\t<url>https://example.com/a?b=1</url>\n\t</appmsg>\n</msg>', 1,
     "示例文章标题 | 示例文章摘要"),
    # 文件
    ('<msg><appmsg><title>示例文件.pdf</title><type>6</type>'
     '<appattach><totallen>1000</totallen></appattach></appmsg></msg>', 1,
     "[文件] 示例文件.pdf"),
    # 拍一拍（type=62）没有 <des>，取 <title>
    ('<msg><appmsg><title>"甲" 拍了拍 "乙"</title><des></des><type>62</type>'
     '</appmsg></msg>', 1, '"甲" 拍了拍 "乙"'),
    # 转账 / 红包：金额在 <des>，备注在 <pay_memo>
    ('<msg><appmsg><title><![CDATA[微信转账]]></title>'
     '<des><![CDATA[收到转账1000.00元]]></des><type>2000</type>'
     '<pay_memo><![CDATA[示例备注]]></pay_memo></appmsg></msg>', 1,
     "[转账] 收到转账1000.00元 | 示例备注"),
    ('<msg><appmsg><des>示例红包提示</des>'
     '<type><![CDATA[2001]]></type></appmsg></msg>', 1, "[红包] 示例红包提示"),
    # 邮件（type=35）、通话（type=50）、位置（type=48）、名片（type=42）
    ('<msg><pushmail><content><subject><![CDATA[示例邮件主题]]></subject>'
     '<digest><![CDATA[示例邮件正文]]></digest></content></pushmail></msg>', 1,
     "[邮件] 示例邮件主题 | 示例邮件正文"),
    ('<voipmsg type="VoIPBubbleMsg"><VoIPBubbleMsg><msg><![CDATA[通话时长 00:10]]></msg>'
     '</VoIPBubbleMsg></voipmsg>', 1, "[通话] 通话时长 00:10"),
    ('<msg>\n\t<location x="1" y="2" label="示例地址" poiname="示例地点"/>\n</msg>', 1,
     "[位置] 示例地址"),
    ('<msg bigheadimgurl="http://x" nickname="示例名片" />', 1, "[名片] 示例名片"),
    # 群置顶 / 撤回等 sysmsg
    ('<sysmsg type="mmchatroomtopmsg"><mmchatroomtopmsg><nickname>示例昵称</nickname>'
     '</mmchatroomtopmsg></sysmsg>', 1, "示例昵称 置顶了一条消息"),
    ('<sysmsg type="revokemsg"><revokemsg><content>"示例账号" 撤回了一条群公告</content>'
     '</revokemsg></sysmsg>', 1, '"示例账号" 撤回了一条群公告'),
    # 模板里的 $变量$ 换成实际昵称，换不出来就保留占位符
    ('<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template>'
     '<plain><![CDATA[]]></plain>'
     '<template><![CDATA["$adder$"通过扫描"$from$"分享的二维码加入群聊]]></template>'
     '<link_list><link name="adder"><memberlist><member><nickname><![CDATA[示例昵称]]>'
     '</nickname></member></memberlist></link></link_list>'
     '</content_template></sysmsgtemplate></sysmsg>', 1,
     '"示例昵称"通过扫描"$from$"分享的二维码加入群聊'),
    # HTML 转义过的 XML 也要能清洗
    ('&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;标题&lt;/title&gt;&lt;type&gt;5&lt;/type&gt;'
     '&lt;des&gt;摘要&lt;/des&gt;&lt;/appmsg&gt;&lt;/msg&gt;', 1, "标题 | 摘要"),
    # 认不出来的 XML：保留文本节点，绝不把 XML 写回库里
    ("<msg><unknown><a>xxx</a></unknown></msg>", 1, "xxx"),
    ("<msg><unknown><a href='x'/></unknown></msg>", 1, "[未知消息]"),
])
def test_clean_message_content(content, local_type, expected):
    assert clean_message_content(content, local_type) == expected


def test_clean_never_returns_xml_for_xml_input():
    sample = ('<msg><appmsg><title>t</title><type>57</type><refermsg><type>1</type>'
              '<displayname>a</displayname><content>c</content></refermsg></appmsg></msg>')
    out = clean_message_content(sample, 1)
    assert not out.lstrip().startswith("<")


def _standardize(msg, talker="1234@chatroom", group_name="群A"):
    client = Mock()
    client.resolve_nickname.side_effect = lambda x: f"name-{x}"
    backend = WcdbBackend(groups=[group_name])
    backend._client = client
    return backend._standardize(msg, group_name, talker)


def test_standardize_stores_cleaned_content_but_keeps_raw_message_id():
    raw = ('<msg><appmsg><title>示例回复</title><type>57</type><refermsg><type>1</type>'
           '<displayname>张三</displayname><content>示例报价 100.5</content>'
           '</refermsg></appmsg></msg>')
    out = _standardize({
        "sender_username": "wxid_a",
        "message_content": raw,
        "localType": 1,
        "create_time": 1700000000,
    })
    assert out["content"] == "[引用 张三] 示例报价 100.5 → 示例回复"
    # 兜底 message_id 必须仍按清洗前的原文计算，否则历史去重会失效
    assert out["message_id"] == hashlib.md5(
        f"wxid_a|{raw}|1700000000".encode()
    ).hexdigest()
    # msg_type 保持原样，不改写下游判断
    assert out["msg_type"] == 1


def test_standardize_keeps_plain_text_untouched():
    out = _standardize({
        "sender_username": "wxid_a",
        "message_content": "wxid_a:\n今天 10:00 出发",
        "localType": 1,
        "create_time": 1700000000,
    })
    assert out["content"] == "今天 10:00 出发"


def test_standardize_still_skips_system_messages():
    assert _standardize({
        "sender_username": "wxid_a",
        "message_content": "张三 邀请 李四 加入了群聊",
        "localType": 10000,
        "create_time": 1700000000,
    }) is None


def test_quote_message_alert_matches_and_pushes_readable_text():
    """报障回归：引用消息入库后，关键词告警既不能丢命中，也不能推 XML。"""
    import json as _json
    import tempfile
    import time
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    import src.assistant.alert as alert_mod
    from src.assistant.alert import AlertEngine
    from src.assistant.config import AlertChat, AlertGroup, AssistantConfig

    raw = ('<?xml version="1.0"?>\n<msg> <appmsg appid="" sdkver="0"> '
           '<title>示例回复</title> <type>57</type> <appattach> <aeskey></aeskey> </appattach> '
           '<refermsg> <type>1</type> <displayname>张三</displayname> '
           '<content>示例报价 100.5 元</content> '
           '<msgsource>&lt;signature&gt;FAKE_SIGNATURE&lt;/signature&gt;</msgsource> '
           '</refermsg> </appmsg> </msg>')
    std = _standardize({
        "sender_username": "wxid_a",
        "message_content": raw,
        "localType": 1,
        "create_time": int(time.time()),
    })
    assert std["content"] == "[引用 张三] 示例报价 100.5 元 → 示例回复"

    cfg = AssistantConfig(assistant_enabled=True)
    cfg.alert_groups = [AlertGroup(
        id="ag_001", name="群A",
        chats=[AlertChat(chat_id="1234@chatroom", name="群A", enabled=True)],
        keywords=[r"/(?<!\d)100(?!\d)/"], enabled=True,
    )]
    outbox = MagicMock()
    outbox.add.return_value = 1
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(alert_mod, "_TRIGGERED_PATH", Path(tmp) / "t.json"), \
            patch("src.im.targets.bound_push_targets", return_value=[]):
        nid = AlertEngine(cfg, outbox).check(std)

    assert nid is not None, "引用消息里的关键字必须仍能命中"
    payload = _json.loads(outbox.add.call_args.kwargs["content"])
    assert "100.5" in payload["message"]
    assert "<appmsg" not in payload["display"] and "<msg>" not in payload["display"]
    assert "FAKE_SIGNATURE" not in payload["display"], "msgsource 签名属于机器字段，不该出现"
