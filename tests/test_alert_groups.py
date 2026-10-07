"""提醒分组模型：老配置迁移、提交校验、agent add_alert 语义。

回归重点：
  1. 旧的"一会话一条"平铺配置必须无损变成单会话分组（关键词/开关/推送目标逐字保留）；
  2. 迁移是幂等的，且**不抛异常**（解析期抛错会让 load_assistant_config 用默认配置
     覆盖整份文件，抹掉用户全部提醒配置）；
  3. agent 的 add_alert 只拿得到会话名：同名分组要并入而不是新建，命中组内某个
     会话名时要并入那个组并在回执里说明，新建时 id 必须唯一。
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.assistant.alert as alert_mod
import src.assistant.config as config_mod
from src.assistant.alert import AlertEngine
from src.assistant.config import (
    AlertChat,
    AlertGroup,
    AssistantConfig,
    _parse_alert_groups,
    validate_alert_groups,
)

LEGACY = [
    {"chat_id": "a@chatroom", "group_name": "测试发单群",
     "keywords": ["保证金", "100万"], "enabled": True, "push_target": "ilink"},
    {"chat_id": "b@chatroom", "group_name": "证券单群",
     "keywords": ["100.01"], "enabled": False, "push_target": ""},
    {"chat_id": "c@chatroom", "group_name": "空关键词群",
     "keywords": [], "enabled": True, "push_target": "ilink"},
    {"group_name": "只有群名的旧条目", "keywords": ["派单"],
     "enabled": True, "push_target": ""},
]


class _ConfigIsolated(unittest.TestCase):
    """把 CONFIG_PATH 指到临时目录，绝不碰用户真实配置。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig = config_mod.CONFIG_PATH
        config_mod.CONFIG_PATH = Path(self._tmp) / "assistant_config.json"
        self.addCleanup(setattr, config_mod, "CONFIG_PATH", self._orig)
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _write(self, payload):
        config_mod.CONFIG_PATH.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class LegacyMigrationTest(_ConfigIsolated):

    def test_flat_entries_become_single_chat_groups(self):
        self._write({"alert_groups": LEGACY})
        cfg = config_mod.load_assistant_config()
        self.assertEqual(len(cfg.alert_groups), 4)

        first = cfg.alert_groups[0]
        self.assertEqual(first.name, "测试发单群")
        self.assertTrue(first.id)
        self.assertEqual(first.keywords, ["保证金", "100万"])
        self.assertTrue(first.enabled)
        self.assertEqual(first.push_target, "ilink")
        self.assertEqual(
            [(c.chat_id, c.name, c.enabled) for c in first.chats],
            [("a@chatroom", "测试发单群", True)],
        )

        second = cfg.alert_groups[1]
        self.assertFalse(second.enabled)          # 开关保留
        self.assertEqual(second.push_target, "")

    def test_empty_keywords_entry_is_kept(self):
        """空关键词的条目不能丢：用户之后还要回来填词，会话绑定不能没了。"""
        self._write({"alert_groups": LEGACY})
        cfg = config_mod.load_assistant_config()
        empty = [g for g in cfg.alert_groups if g.name == "空关键词群"]
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0].keywords, [])
        self.assertEqual(empty[0].chats[0].chat_id, "c@chatroom")

    def test_name_only_entry_keeps_name_for_engine(self):
        self._write({"alert_groups": LEGACY})
        cfg = config_mod.load_assistant_config()
        legacy = [g for g in cfg.alert_groups if g.name == "只有群名的旧条目"][0]
        self.assertEqual(legacy.chats, [AlertChat(chat_id="", name="只有群名的旧条目")])

    def test_migration_is_idempotent(self):
        self._write({"alert_groups": LEGACY})
        once = config_mod.load_assistant_config()
        config_mod.save_assistant_config(once)
        twice = config_mod.load_assistant_config()
        self.assertEqual(
            [(g.id, g.name, [c.chat_id for c in g.chats], g.keywords, g.enabled)
             for g in once.alert_groups],
            [(g.id, g.name, [c.chat_id for c in g.chats], g.keywords, g.enabled)
             for g in twice.alert_groups],
        )

    def test_duplicate_chat_across_legacy_entries_keeps_first(self):
        self._write({"alert_groups": LEGACY + [
            {"chat_id": "a@chatroom", "group_name": "重复的会话",
             "keywords": ["x"], "enabled": True, "push_target": ""},
        ]})
        cfg = config_mod.load_assistant_config()
        owners = [g.name for g in cfg.alert_groups
                  if any(c.chat_id == "a@chatroom" for c in g.chats)]
        self.assertEqual(owners, ["测试发单群"])

    def test_dirty_input_never_raises(self):
        """手改配置塞脏数据只能 warning，不能让整份配置被默认值覆盖。"""
        for bad in (None, "not-a-list", [None, 5, "x"], [{"keywords": "nope"}],
                    [{"chat_id": 123, "group_name": None, "keywords": None}]):
            with self.subTest(bad=bad):
                groups = _parse_alert_groups(bad)
                self.assertIsInstance(groups, list)


class ValidateAlertGroupsTest(unittest.TestCase):

    def test_valid_new_shape(self):
        self.assertEqual(validate_alert_groups([
            {"id": "ag_001", "name": "组A",
             "chats": [{"chat_id": "a@chatroom", "name": "A", "enabled": True}]},
        ]), "")

    def test_missing_name(self):
        self.assertIn("名称不能为空", validate_alert_groups(
            [{"name": "  ", "chats": [{"chat_id": "a"}]}]))

    def test_missing_chats(self):
        self.assertIn("至少要选择一个会话", validate_alert_groups(
            [{"name": "组A", "chats": []}]))

    def test_duplicate_chat_across_groups(self):
        err = validate_alert_groups([
            {"name": "组A", "chats": [{"chat_id": "a@chatroom"}]},
            {"name": "组B", "chats": [{"chat_id": "a@chatroom"}]},
        ])
        self.assertIn("重复使用了同一个会话", err)

    def test_name_only_chat_is_accepted(self):
        """agent 建的旧条目只有群名，不能因为校验把用户挡在保存之外。"""
        self.assertEqual(validate_alert_groups(
            [{"name": "组A", "chats": [{"chat_id": "", "name": "某群"}]}]), "")

    def test_non_list(self):
        self.assertEqual(validate_alert_groups("nope"), "提醒分组格式不正确")


class AddAlertToolTest(_ConfigIsolated):
    """agent 只有会话名可用，验证"并入已有分组 / 新建单会话分组"的语义。"""

    def setUp(self):
        super().setUp()
        from src.agent.tools import ToolExecutor
        self.executor = ToolExecutor.__new__(ToolExecutor)
        self.executor._alert_engine = MagicMock()

    def _add(self, name, keywords):
        return self.executor._handle_add_alert(name, keywords)

    def test_creates_single_chat_group_with_unique_id(self):
        self._add("抢单群A", ["派单"])
        cfg = config_mod.load_assistant_config()
        self.assertEqual(len(cfg.alert_groups), 1)
        g = cfg.alert_groups[0]
        self.assertEqual(g.id, "ag_001")
        self.assertEqual(g.name, "抢单群A")
        self.assertEqual(g.chats, [AlertChat(chat_id="", name="抢单群A")])
        self.assertEqual(g.keywords, ["派单"])
        # 写配置后必须热更新引擎，否则新关键词要等重启才生效
        self.executor._alert_engine.update_config.assert_called_once()

    def test_same_name_merges_into_existing_group(self):
        self._add("抢单群A", ["派单"])
        out = self._add("抢单群A", ["急单"])
        cfg = config_mod.load_assistant_config()
        self.assertEqual(len(cfg.alert_groups), 1, "同名不能新建第二个分组")
        self.assertEqual(sorted(cfg.alert_groups[0].keywords), ["急单", "派单"])
        self.assertIn("已更新", out)

    def test_chat_name_inside_group_merges_and_names_the_group(self):
        """分组叫"证券组"、里面有个会话叫"群B"：对"群B"加词要并进"证券组"。"""
        cfg = config_mod.load_assistant_config()
        cfg.alert_groups = [AlertGroup(
            id="ag_001", name="证券组",
            chats=[AlertChat(chat_id="b@chatroom", name="群B")],
            keywords=["100万"],
        )]
        config_mod.save_assistant_config(cfg)

        out = self._add("群B", ["100.01"])
        cfg = config_mod.load_assistant_config()
        self.assertEqual(len(cfg.alert_groups), 1)
        self.assertEqual(cfg.alert_groups[0].name, "证券组")
        self.assertEqual(sorted(cfg.alert_groups[0].keywords), ["100.01", "100万"])
        self.assertIn("证券组", out, "回执要说明并入了哪个分组")

    def test_new_name_gets_next_unique_id(self):
        self._add("抢单群A", ["派单"])
        self._add("证券组", ["100万"])
        cfg = config_mod.load_assistant_config()
        self.assertEqual([g.id for g in cfg.alert_groups], ["ag_001", "ag_002"])

    def test_created_group_passes_validation_and_engine_matches(self):
        """agent 建的组要能过前端保存校验，且引擎按群名真的能命中。"""
        self._add("抢单群A", ["派单"])
        cfg = config_mod.load_assistant_config()
        raw = config_mod._config_to_dict(cfg)["alert_groups"]
        self.assertEqual(validate_alert_groups(raw), "")

        with patch.object(alert_mod, "_TRIGGERED_PATH",
                          Path(self._tmp) / "triggered.json"), \
             patch("src.im.targets.bound_push_targets", return_value=[]):
            outbox = MagicMock()
            outbox.add.return_value = 1
            cfg.assistant_enabled = True   # 磁盘默认配置总开关是关的
            engine = AlertEngine(cfg, outbox)
            nid = engine.check({
                "group_name": "抢单群A", "sender_name": "张三",
                "content": "有个派单谁接", "timestamp": int(time.time()),
            })
            self.assertIsNotNone(nid, "只有群名的 agent 组必须仍能命中")


class ListAlertsToolTest(_ConfigIsolated):

    def test_lists_groups_with_chats_and_keywords(self):
        cfg = config_mod.load_assistant_config()
        cfg.alert_groups = [AlertGroup(
            id="ag_001", name="证券组",
            chats=[AlertChat(chat_id="a@chatroom", name="群A"),
                   AlertChat(chat_id="b@chatroom", name="群B", enabled=False)],
            keywords=["100万"],
        )]
        config_mod.save_assistant_config(cfg)

        from src.agent.tools import ToolExecutor
        executor = ToolExecutor.__new__(ToolExecutor)
        executor._alert_engine = None
        out = executor._handle_list_alerts()
        self.assertIn("证券组", out)
        self.assertIn("群A", out)
        self.assertIn("100万", out)
        self.assertNotIn("群B", out, "组内被关掉的会话不该出现在回执里")


if __name__ == "__main__":
    unittest.main()
