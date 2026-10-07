"""Tests for Assistant alert engine and digest filtering."""

import time
import unittest

from src.assistant.alert import AlertEngine
from src.assistant.digest import filter_messages, build_digest_prompt, generate_memory_update_prompt
from src.assistant.config import (
    AssistantConfig, AlertChat, AlertGroup, DigestChat, DigestGroup, GroupProfile,
)
from src.assistant.outbox import Outbox


class TestAlertEngine(unittest.TestCase):

    def test_keyword_match(self):
        cfg = AssistantConfig(assistant_enabled=True)
        cfg.alert_groups = [
            AlertGroup(id="ag_001", name="抢单群A",
                       chats=[AlertChat(chat_id="a@chatroom", name="抢单群A")],
                       keywords=["派单", "急单"], enabled=True),
        ]
        outbox = Outbox()
        engine = AlertEngine(cfg, outbox)

        msg = {
            "chat_id": "a@chatroom",
            "group_name": "抢单群A",
            "sender_name": "张三",
            "content": "急单！谁接 报价500 明天就要",
            "timestamp": int(time.time()),
        }
        nid = engine.check(msg)
        self.assertIsNotNone(nid)

    def test_keyword_case_insensitive(self):
        """只有群名、没有 chat_id 的旧条目仍按名字匹配（agent 工具建的组）。"""
        cfg = AssistantConfig(assistant_enabled=True)
        cfg.alert_groups = [
            AlertGroup(id="ag_001", name="测试群",
                       chats=[AlertChat(chat_id="", name="测试群")],
                       keywords=["需求", "报价"], enabled=True),
        ]
        outbox = Outbox()
        engine = AlertEngine(cfg, outbox)

        msg = {
            "group_name": "测试群",
            "sender_name": "李四",
            "content": "这个项目的报价是5000",
            "timestamp": int(time.time()),
        }
        nid = engine.check(msg)
        self.assertIsNotNone(nid)

    def test_no_match(self):
        cfg = AssistantConfig(assistant_enabled=True)
        cfg.alert_groups = [
            AlertGroup(id="ag_001", name="抢单群A",
                       chats=[AlertChat(chat_id="a@chatroom", name="抢单群A")],
                       keywords=["派单"], enabled=True),
        ]
        outbox = Outbox()
        engine = AlertEngine(cfg, outbox)

        msg = {
            "chat_id": "a@chatroom",
            "group_name": "抢单群A",
            "sender_name": "张三",
            "content": "哈哈 今天天气真好",
            "timestamp": int(time.time()),
        }
        nid = engine.check(msg)
        self.assertIsNone(nid)

    def test_other_chat_in_same_config_does_not_match(self):
        """分组只覆盖组内会话，别的群发同样的词不能触发。"""
        cfg = AssistantConfig(assistant_enabled=True)
        cfg.alert_groups = [
            AlertGroup(id="ag_001", name="抢单群A",
                       chats=[AlertChat(chat_id="a@chatroom", name="抢单群A")],
                       keywords=["派单"], enabled=True),
        ]
        outbox = Outbox()
        engine = AlertEngine(cfg, outbox)

        nid = engine.check({
            "chat_id": "other@chatroom", "group_name": "别的群",
            "content": "派单！", "timestamp": int(time.time()),
        })
        self.assertIsNone(nid)

    def test_multi_chat_group_shares_keywords(self):
        """一个组里多个会话共用同一份关键词，每个都能触发。"""
        cfg = AssistantConfig(assistant_enabled=True)
        cfg.alert_groups = [
            AlertGroup(id="ag_001", name="证券单组",
                       chats=[AlertChat(chat_id="a@chatroom", name="群A"),
                              AlertChat(chat_id="b@chatroom", name="群B")],
                       keywords=["100万"], enabled=True),
        ]
        outbox = Outbox()
        engine = AlertEngine(cfg, outbox)

        for cid, gname in (("a@chatroom", "群A"), ("b@chatroom", "群B")):
            with self.subTest(chat=cid):
                nid = engine.check({
                    "chat_id": cid, "group_name": gname,
                    "content": "出100万", "timestamp": int(time.time()),
                })
                self.assertIsNotNone(nid)

    def test_chat_level_disabled_skips_only_that_chat(self):
        """会话级开关关掉后，同组其他会话照常提醒。"""
        cfg = AssistantConfig(assistant_enabled=True)
        cfg.alert_groups = [
            AlertGroup(id="ag_001", name="证券单组",
                       chats=[AlertChat(chat_id="a@chatroom", name="群A", enabled=False),
                              AlertChat(chat_id="b@chatroom", name="群B", enabled=True)],
                       keywords=["100万"], enabled=True),
        ]
        outbox = Outbox()
        engine = AlertEngine(cfg, outbox)

        self.assertIsNone(engine.check({
            "chat_id": "a@chatroom", "group_name": "群A",
            "content": "出100万", "timestamp": int(time.time()),
        }))
        self.assertIsNotNone(engine.check({
            "chat_id": "b@chatroom", "group_name": "群B",
            "content": "出100万", "timestamp": int(time.time()),
        }))

    def test_disabled_group(self):
        cfg = AssistantConfig(assistant_enabled=True)
        cfg.alert_groups = [
            AlertGroup(id="ag_001", name="抢单群A",
                       chats=[AlertChat(chat_id="a@chatroom", name="抢单群A")],
                       keywords=["派单"], enabled=False),
        ]
        outbox = Outbox()
        engine = AlertEngine(cfg, outbox)

        msg = {
            "chat_id": "a@chatroom",
            "group_name": "抢单群A",
            "content": "急单派单！",
            "timestamp": int(time.time()),
        }
        nid = engine.check(msg)
        self.assertIsNone(nid)

    def test_assistant_disabled(self):
        cfg = AssistantConfig(assistant_enabled=False)
        outbox = Outbox()
        engine = AlertEngine(cfg, outbox)

        msg = {
            "group_name": "抢单群A",
            "content": "派单！",
            "timestamp": int(time.time()),
        }
        nid = engine.check(msg)
        self.assertIsNone(nid)


class TestDigestFiltering(unittest.TestCase):

    def test_filter_noise_replies(self):
        msgs = [
            {"content": "收到", "sender_name": "A"},
            {"content": "好的", "sender_name": "B"},
            {"content": "哈哈", "sender_name": "C"},
            {"content": "这是一个有意义的讨论", "sender_name": "D"},
        ]
        result = filter_messages(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "这是一个有意义的讨论")

    def test_filter_system_messages(self):
        msgs = [
            {"content": "张三加入了群聊"},
            {"content": "李四退出了群聊"},
            {"content": "有人修改群名为'新群名'"},
            {"content": "正常消息"},
        ]
        result = filter_messages(msgs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "正常消息")

    def test_filter_too_short(self):
        msgs = [
            {"content": "a"},
            {"content": "这是一条足够长的正常消息"},
        ]
        result = filter_messages(msgs)
        self.assertEqual(len(result), 1)

    def test_filter_ignore_keywords(self):
        msgs = [
            {"content": "这是广告推广信息"},
            {"content": "正常讨论"},
        ]
        result = filter_messages(msgs, ignore_keywords=["广告"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "正常讨论")

    def test_filter_media_placeholder(self):
        """媒体消息(带 msg_type)替换为结构化占位符并保留，供 LLM 感知上下文。"""
        msgs = [
            {"content": "[图片]", "msg_type": 3},
            {"content": "[语音]", "msg_type": 34},
            {"content": "正常消息"},
        ]
        result = filter_messages(msgs)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["content"], "{{ image }}")
        self.assertEqual(result[1]["content"], "{{ voice }}")
        self.assertEqual(result[2]["content"], "正常消息")

    def test_build_digest_prompt(self):
        dg = DigestGroup(
            id="dg_001",
            name="测试群",
            chats=[DigestChat(chat_id="x@chatroom", name="测试群")],
            schedule=["12:00"],
            profile=GroupProfile(
                style="行动项优先",
                custom_prompt="只输出待办",
            ),
            memory="上次聊了部署方案",
        )
        msgs = [
            {"sender_name": "A", "content": "好消息", "timestamp": 1700000000},
            {"sender_name": "B", "content": "什么消息", "timestamp": 1700000100},
        ]
        prompt = build_digest_prompt(dg, msgs)
        # 结构：会话名 + 近期记忆 + 最近消息（群信息段已移除）
        self.assertIn("## 本次摘要的会话\n测试群", prompt)
        self.assertIn("## 近期记忆", prompt)
        self.assertIn("上次聊了部署方案", prompt)
        self.assertIn("## 「测试群」最近 2 条消息", prompt)
        self.assertIn("好消息", prompt)
        self.assertIn("什么消息", prompt)
        # 已删除字段不应出现在 prompt 中
        self.assertNotIn("群简介", prompt)
        self.assertNotIn("关注点", prompt)
        self.assertNotIn("忽略内容", prompt)

    def test_build_digest_prompt_names_chat_not_group(self):
        """会话名与分组名不同时必须点会话名。

        一个分组配多个会话、本轮只有其中一个有新消息时，记忆是全组共用的；
        只写分组名的话，LLM 会把别的会话的历史当成这个会话的。
        """
        dg = DigestGroup(id="dg_001", name="羊毛组", memory="组里在聊淘宝新规")
        prompt = build_digest_prompt(dg, [{"sender_name": "A", "content": "hi",
                                           "timestamp": 1700000000}], "示例群A")
        self.assertIn("## 本次摘要的会话\n示例群A", prompt)
        self.assertIn("## 「示例群A」最近 1 条消息", prompt)
        self.assertIn("分组「羊毛组」共用", prompt)
        self.assertIn("只在与「示例群A」相关时引用", prompt)

    def test_memory_update_prompt(self):
        prompt = generate_memory_update_prompt("旧记忆", "新摘要内容")
        self.assertIn("旧记忆", prompt)
        self.assertIn("新摘要内容", prompt)
        self.assertIn("2000", prompt)

    def test_memory_update_prompt_is_group_scoped_and_per_chat(self):
        """记忆是分组级的：必须按会话分块，额度随会话数放大。"""
        prompt = generate_memory_update_prompt(
            "旧记忆", "## 甲\n要点A\n\n## 乙\n要点B",
            chat_names=["甲", "乙", "丙"], budget=2600)
        self.assertIn("本分组包含 3 个会话：甲、乙、丙", prompt)
        self.assertIn("### 会话名", prompt)
        self.assertIn("禁止把 A 会话的内容写进 B 会话", prompt)
        self.assertIn("不超过 2600 字", prompt)
        # 早期按单群口吻写的旧记忆要有去处，否则会被硬塞进某个会话
        self.assertIn("### 分组共性", prompt)
        self.assertNotIn("第一人称", prompt)


if __name__ == "__main__":
    unittest.main()
