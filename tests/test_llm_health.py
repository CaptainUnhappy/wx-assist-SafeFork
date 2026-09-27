"""LLM 健康记录 + 面板 AI 状态判定。

回归的 bug：AI 余额耗尽后 provider 返回 403，重试 3 次全挂，但首页面板仍显示
"AI 后端 可达"。原因有两层：判定用的是"曾经成功调用过一次"（`last_api_call_time > 0`
永不清零），且 `ai_ok` 由 ai_verified / 曾经成功 / bot 上报三方 OR 而来，任何一处
置 False 都会在下次心跳被顶回 True。
"""
import time
import unittest
from unittest.mock import patch

from src.summarize import base as summarize_base
from src.summarize.base import (
    AbstractSummarizer,
    get_llm_health,
    record_llm_failure,
    record_llm_success,
)

_QUOTA_403 = (
    "Error code: 403 - {'error': {'message': 'token quota is not enough, "
    "token remain quota: ¥0.001000'}}"
)


class _FakeBackend:
    """只带 _retry_with_backoff 需要的三个属性，用来验证跨实例共享。"""

    max_retries = 1
    retry_exceptions = (ValueError,)

    def __init__(self):
        self.last_api_call_time = 0.0


class _SharedStateIsolated(unittest.TestCase):
    """健康记录是进程级的，用前存、用后还原，避免用例串味。"""

    def setUp(self):
        self._saved = get_llm_health()
        with summarize_base._llm_health_lock:
            summarize_base._llm_health.update({"ok": False, "ts": 0.0, "msg": ""})

    def tearDown(self):
        with summarize_base._llm_health_lock:
            summarize_base._llm_health.clear()
            summarize_base._llm_health.update(self._saved)


class RetryWithBackoffHealthTest(_SharedStateIsolated):

    def _fail_call(self, *_args):
        raise ValueError(_QUOTA_403)

    def test_exhausted_retries_record_failure_with_reason(self):
        backend = _FakeBackend()
        with patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                AbstractSummarizer._retry_with_backoff(backend, self._fail_call, "AI chat")

        health = get_llm_health()
        self.assertFalse(health["ok"])
        self.assertGreater(health["ts"], 0)
        self.assertIn("AI chat", health["msg"])
        self.assertIn("403", health["msg"])

    def test_success_clears_previous_failure(self):
        backend = _FakeBackend()
        record_llm_failure(_QUOTA_403)
        self.assertFalse(get_llm_health()["ok"])

        AbstractSummarizer._retry_with_backoff(backend, lambda: "ok", "AI chat")
        health = get_llm_health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["msg"], "")

    def test_failure_from_a_fresh_instance_is_still_recorded(self):
        """OA 监视器每次都新建 summarizer，失败必须跨实例可见。"""
        first = _FakeBackend()
        AbstractSummarizer._retry_with_backoff(first, lambda: "ok", "AI chat")

        second = _FakeBackend()
        with patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                AbstractSummarizer._retry_with_backoff(second, self._fail_call, "OA 摘要")

        self.assertFalse(get_llm_health()["ok"])

    def test_success_on_instance_a_does_not_reset_instance_b_failure(self):
        record_llm_failure(_QUOTA_403)
        backend = _FakeBackend()
        AbstractSummarizer._retry_with_backoff(backend, lambda: "ok", "AI chat")
        self.assertTrue(get_llm_health()["ok"])

    def test_failure_message_is_clipped(self):
        record_llm_failure("x" * 500)
        self.assertLessEqual(len(get_llm_health()["msg"]), 200)


class AiStatusPanelTest(_SharedStateIsolated):

    def setUp(self):
        super().setUp()
        from src.web.server import _ServerStatus
        self.status = _ServerStatus()

    def test_never_called_falls_back_to_config_detection(self):
        self.status.update(ai_verified=True)
        snap = self.status.snapshot()
        self.assertTrue(snap["ai_ok"])
        self.assertEqual(snap["ai_error"], "")

        self.status.ai_verified = False
        self.assertFalse(self.status.snapshot()["ai_ok"])

    def test_failure_after_success_turns_panel_red(self):
        record_llm_success()
        self.assertTrue(self.status.snapshot()["ai_ok"])

        record_llm_failure(f"AI chat: {_QUOTA_403}")
        snap = self.status.snapshot()
        self.assertFalse(snap["ai_ok"])
        self.assertIn("403", snap["ai_error"])

    def test_bot_heartbeat_cannot_resurrect_panel(self):
        """心跳会带上 last_api_call_time，不能因此把"不可用"顶回"可达"。"""
        record_llm_success()
        self.status.update(ai_ok=True, last_api_call_time=time.time())
        self.assertTrue(self.status.snapshot()["ai_ok"])

        record_llm_failure(f"AI chat: {_QUOTA_403}")
        self.status.update(ai_ok=True, last_api_call_time=time.time(),
                           model_name="gpt-test")
        snap = self.status.snapshot()
        self.assertFalse(snap["ai_ok"])
        self.assertIn("403", snap["ai_error"])

    def test_direct_ai_ok_write_is_ignored(self):
        """ai_ok 只能由真实调用结果推导，不接受调用方直接置真。"""
        self.status.update(ai_ok=True, ai_verified=False)
        self.assertFalse(self.status.snapshot()["ai_ok"])

    def test_recovery_clears_reason(self):
        record_llm_failure(_QUOTA_403)
        self.status.update()
        self.assertFalse(self.status.snapshot()["ai_ok"])

        record_llm_success()
        self.status.update()
        snap = self.status.snapshot()
        self.assertTrue(snap["ai_ok"])
        self.assertEqual(snap["ai_error"], "")


class StreamSuccessGuardTest(_SharedStateIsolated):
    """流式路径的成功登记不能把 Stub 的"未配置"提示语当成 AI 可达。"""

    def _mark(self, backend_name):
        from src.web.ai_chat import _mark_stream_success

        class _S:
            _backend_name = backend_name

        _mark_stream_success(_S())

    def test_stub_stream_does_not_mark_ai_available(self):
        self._mark("stub")
        self.assertEqual(get_llm_health()["ts"], 0.0)

    def test_real_backend_first_token_marks_available(self):
        self._mark("deepseek")
        health = get_llm_health()
        self.assertTrue(health["ok"])
        self.assertGreater(health["ts"], 0)


class HealthMonitorCheckTest(_SharedStateIsolated):
    def setUp(self):
        super().setUp()
        from src.bot import HealthMonitor
        self.monitor = HealthMonitor(None, None, None, None, None)

    def test_never_called_is_not_ok(self):
        self.assertFalse(self.monitor._check_ai_ok())

    def test_tracks_latest_real_call(self):
        record_llm_success()
        self.assertTrue(self.monitor._check_ai_ok())

        record_llm_failure(_QUOTA_403)
        self.assertFalse(self.monitor._check_ai_ok())


if __name__ == "__main__":
    unittest.main()
