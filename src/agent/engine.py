"""ReAct Loop engine — the core reasoning + acting cycle.

Usage:
    engine = AgentEngine(summarizer, tool_executor)
    reply = engine.run("帮我查下系统状态")
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Memory ──────────────────────────────────────────────────────────
AGENT_MEMORY_DB = Path("data/agent_memory.db")
MEMORY_HISTORY_LIMIT = 10   # 短期记忆达到此阈值后触发总结
MEMORY_LOAD_COUNT = 3       # 每次注入 system prompt 的长期记忆条数

# ── System prompt for the Agent ────────────────────────────────────

AGENT_SYSTEM_PROMPT = """\
你是微信助手 Agent，运行在用户的个人微信机器人上。你不仅能聊天，还能调用工具完成用户的需求。

## 你的身份
- 用户通过微信私聊与你对话
- 你可以调用系统工具获取信息或执行操作

## 可用工具
{tool_descriptions}

## 工作方式
你运行在 ReAct（推理 + 行动）循环中：

1. **Thought** — 分析用户想要什么，需要哪些信息
2. **Action** — 选择最合适的工具来获取信息或执行操作
3. **Observation** — 查看工具返回的结果
4. **Repeat** — 重复直到任务完成，然后回复用户

## 安全规则
- 执行写操作（生成摘要、配置预警、配置定时任务等）之前，必须先调用 confirm_action 获取用户确认
- 用户确认后，才能继续执行写操作工具
- 如果用户取消，回复用户操作已取消
- 查询类操作（查状态、搜索记录、查看配置）不需要确认

## 规则
- 只在确实需要信息时才调用工具，不要过度使用
- 调用工具时必须使用正确的参数
- 如果用户指令不清晰，直接询问澄清
- 不要编造工具返回的结果
- 任务完成后，用自然语言回复用户

## 停止条件（必须遵守）

你的行动轮数上限为 {max_steps} 步，每一步都在消耗资源，不要浪费。

**⚡ 强制规则：连续 3 次调用同一个工具仍未取得有效进展时，必须立即停止。**
所谓有效进展是指：返回了新的、有用的、可以推进任务的数据。
如果返回空、返回无关内容、反复报错、或每次都是差不多的结果，都视为无进展。

此时：
1. **禁止**再调用任何工具
2. **必须**直接回复用户当前情况
3. 即使用户的问题没解决，也要说"我没找到，请提供更精确的描述"
4. 不要换参数重试——换个关键词搜还是同一个工具，算连续调用
"""


class AgentEngine:
    """ReAct Loop engine with confirm_action state machine.

    All operations are synchronous.  No async/await needed.
    Runs in iLink receiver's background thread.

    Args:
        summarizer: AbstractSummarizer instance with agent_chat() support.
        tool_executor: ToolExecutor instance.
        max_steps: Maximum ReAct iterations before giving up.
        progress_callback: Optional. Called with (step, max_steps, tool_names, reasoning) after
                          each tool-calling step, so callers can push progress
                          to the user via iLink or other channels.
    """

    def __init__(self, summarizer, tool_executor,
                 max_steps: int = 8,
                 progress_callback=None):
        self._llm = summarizer
        self._tools = tool_executor
        self._max_steps = max_steps
        self._progress_callback = progress_callback
        # Per-conversation state: IM platforms must not share user context.
        self._state_lock = threading.RLock()
        self._conversation_locks: dict[str, threading.RLock] = {}
        self._histories: dict[str, list[tuple[str, str]]] = {"default": []}
        self._pending_confirms: dict[str, dict] = {}
        self._bypass_confirms: set[str] = set()
        self._init_memory_db()

    # ── Public API ─────────────────────────────────────────────────

    def run(self, user_message: str, *, source_platform: str = "wechat",
            source_target: str | None = None,
            conversation_key: str = "default") -> str:
        """Run the ReAct loop and return the final reply.

        If there is a pending confirmation from a previous run(),
        handles the user's response first.

        Args:
            user_message: The user's message content.

        Returns:
            Final reply text to send back to the user.
        """

        key = conversation_key or "default"
        with self._state_lock:
            conversation_lock = self._conversation_locks.setdefault(key, threading.RLock())
        with conversation_lock:
            return self._run_conversation(
                user_message, source_platform=source_platform,
                source_target=source_target, conversation_key=key,
            )

    def _run_conversation(self, user_message: str, *, source_platform: str,
                          source_target: str | None, conversation_key: str) -> str:
        key = conversation_key or "default"
        with self._state_lock:
            pending = self._pending_confirms.get(key)

        # ── Phase 1: Handle pending confirmation ─────────────────────
        if pending is not None:
            logger.info("[Agent] Pending confirm exists, routing to _handle_confirm_response")
            messages = [{"role": "user", "content": user_message}]
            return self._handle_confirm_response(
                user_message,
                messages,
                source_platform=source_platform,
                source_target=source_target,
                conversation_key=conversation_key,
            )

        # ── Phase 2: Build messages with memory context ──────────────
        messages: list[dict] = []

        with self._state_lock:
            history = self._histories.setdefault(key, [])
            history_snapshot = list(history[-MEMORY_HISTORY_LIMIT:])
        # Inject short-term history (last N exchanges)
        for user_msg, agent_reply in history_snapshot:
            messages.append({"role": "user", "content": user_msg})
            messages.append({"role": "assistant", "content": agent_reply})

        messages.append({"role": "user", "content": user_message})

        # ── Phase 3: ReAct loop ──────────────────────────────────────
        logger.info("[Agent] run() — user_message='%s', history=%d entries",
                    user_message[:80], len(history_snapshot))

        system = self._build_system_prompt(conversation_key=key)

        logger.info("[Agent] Entering _react_loop — tools=%s",
                    [t["function"]["name"] for t in self._tools.registry.get_all_schemas()])

        reply = self._react_loop(
            system,
            messages,
            source_platform=source_platform,
            source_target=source_target,
            conversation_key=key,
        )

        # Save to per-conversation short-term history.
        with self._state_lock:
            history = self._histories.setdefault(key, [])
            history.append((user_message, reply))
            history_size = len(history)
        logger.info("[Agent] History now %d entries", history_size)

        if history_size >= MEMORY_HISTORY_LIMIT:
            self._consolidate_memory(key)

        return reply

    # ── One-shot execution (for cron agent_task) ──────────────────────

    def run_once(self, prompt: str, system_override: str = "") -> str:
        """一次性执行，不保留历史。供定时任务等外部场景调用。

        跟 run() 用的是同一个 _react_loop，同一个工具集（含 MCP）。
        不装历史，不触发记忆合 consolidation。
        """
        system = system_override or self._build_system_prompt()
        messages = [{"role": "user", "content": prompt}]
        logger.info("[Agent] run_once — prompt='%s'", prompt[:80])
        return self._react_loop(system, messages)

    # ── Streaming execution (for Voice Companion) ─────────────────────

    def clear_conversation(self, conversation_key: str = "voice") -> None:
        """清空指定会话的短期历史和待确认状态（语音"重新开始"用）。"""
        key = conversation_key or "default"
        with self._state_lock:
            self._histories.pop(key, None)
            self._pending_confirms.pop(key, None)
            self._bypass_confirms.discard(key)
        logger.info("[Agent] Cleared conversation: %s", key)

    def run_stream(self, user_message: str, *,
                   conversation_key: str = "voice",
                   cancel_event=None):
        """流式 ReAct 循环（语音助手专用）。

        生成器，yield 事件 dict：
        - {"type":"token","content": str}        最终回复的文本增量
        - {"type":"tool_start","name": str}       即将调用工具
        - {"type":"tool_done","name": str,"preview": str} 工具返回摘要
        - {"type":"confirm","question": str}      写操作需用户语音确认
        - {"type":"final","content": str}         本轮最终回复
        - {"type":"cancelled"}                    被 barge-in 取消
        - {"type":"error","message": str}         失败

        与 run() 共享同一套短期历史 / 长期记忆 / 确认状态机，
        但使用独立 conversation_key（默认 "voice"），与微信私聊互不串上下文。

        Args:
            cancel_event: threading.Event，set 后尽快终止（barge-in 用）。
        """
        key = conversation_key or "voice"
        # 注意：不能在这里 `with conversation_lock: return generator`——
        # with 会在 return（生成器对象创建）时立即释放，迭代根本还没开始。
        # 锁在 _run_stream_conversation 的生成器体内 acquire/release。
        return self._run_stream_conversation(user_message, key, cancel_event)

    def _run_stream_conversation(self, user_message: str, key: str,
                                 cancel_event):
        """实际生成器。

        RLock 必须在迭代线程内获取/释放（整个迭代都在同一个 HTTP 线程）。
        非阻塞获取：上一轮还没结束（barge-in 的 cancel 传到后端有延迟）时
        快速失败，由前端决定忽略还是重试。
        """
        conversation_lock = self._conversation_locks.setdefault(key, threading.RLock())
        acquired = conversation_lock.acquire(blocking=False)
        if not acquired:
            yield {"type": "error",
                   "message": "上一轮对话还在处理中，请稍等一下再说"}
            return
        try:
            yield from self._stream_inner(user_message, key, cancel_event)
        finally:
            conversation_lock.release()

    def _stream_inner(self, user_message: str, key: str, cancel_event):
        # ── 待确认状态：语音回答"确定/取消"走非流式确认状态机 ──
        with self._state_lock:
            has_pending = key in self._pending_confirms

        if has_pending:
            reply = self._handle_confirm_response(
                user_message,
                [{"role": "user", "content": user_message}],
                source_platform="voice",
                conversation_key=key,
            )
            # _handle_confirm_response 可能返回追问（未清 pending），
            # 也可能已执行完动作进入新一轮 ReAct；两种情况回复都直接播
            with self._state_lock:
                history = self._histories.setdefault(key, [])
                history.append((user_message, reply))
                history_size = len(history)
            if history_size >= MEMORY_HISTORY_LIMIT:
                # 后台总结，避免最后一帧之后多卡几秒
                threading.Thread(
                    target=self._consolidate_memory, args=(key,),
                    daemon=True,
                ).start()
            yield {"type": "final", "content": reply}
            return

        # ── 构建带短期历史的消息序列 ──
        messages: list[dict] = []
        with self._state_lock:
            history = self._histories.setdefault(key, [])
            history_snapshot = list(history[-MEMORY_HISTORY_LIMIT:])
        for u, a in history_snapshot:
            messages.append({"role": "user", "content": u})
            messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": user_message})

        system = self._build_system_prompt(conversation_key=key)
        system += self._voice_system_suffix()

        reply = None
        try:
            for ev in self._react_loop_stream(system, messages, key, cancel_event):
                t = ev.get("type")
                if t == "final":
                    reply = ev.get("content", "")
                yield ev
                if t == "cancelled":
                    return
        except Exception as e:
            logger.exception("[Agent] run_stream failed")
            yield {"type": "error", "message": f"AI 调用失败：{e}"}
            return

        # ── 落短期历史（被取消的半轮回覆不保存，避免污染上下文）──
        if reply:
            with self._state_lock:
                history = self._histories.setdefault(key, [])
                history.append((user_message, reply))
                history_size = len(history)
            if history_size >= MEMORY_HISTORY_LIMIT:
                # 后台总结，避免阻塞 SSE 收尾
                threading.Thread(
                    target=self._consolidate_memory, args=(key,),
                    daemon=True,
                ).start()

    @staticmethod
    def _voice_system_suffix() -> str:
        """语音场景的 system prompt 补充：当前时间 + 口语化风格约束。"""
        weekday_cn = "一二三四五六日"[time.localtime().tm_wday]
        now_str = time.strftime("%Y年%m月%d日（周" + weekday_cn + "）%H:%M",
                                time.localtime())
        return f"""

## 当前时间
{now_str}

## 语音对话风格（用户正在用打电话的方式与你交流）
- 用口语化、自然、简洁的中文回答，先给结论再补充，像真人通话一样
- 禁止使用 Markdown 表格、代码块、标题符号(#)、星号(**)和长编号列表；
  需要分点时用"第一、第二"或"首先、另外"这种口语表达
- 单次回答尽量控制在 300 字以内；信息量大时先说最重要的，
  用户会继续追问，不要一次把所有细节倒完
- 提到日期用相对说法（今天、昨天、本周），不要念"2026-10-14"这种格式
- 查询类问题优先调用 query_my_feed 等工具拿到事实再回答，禁止编造"""

    def _react_loop_stream(self, system: str, messages: list[dict],
                           conversation_key: str, cancel_event):
        """流式 ReAct 主循环。yield 事件 dict，最后一帧为 final/cancelled。"""
        for step in range(1, self._max_steps + 1):
            if cancel_event is not None and cancel_event.is_set():
                yield {"type": "cancelled"}
                return

            logger.info("[Agent/Voice] Step %d/%d messages=%d",
                        step, self._max_steps, len(messages))

            # ── 流式调用 LLM，累积 content / tool_calls ──
            content_parts: list[str] = []
            tool_calls = None
            reasoning = ""
            try:
                for kind, payload in self._llm.agent_chat_stream(
                    system_prompt=system,
                    messages=messages,
                    tools=self._tools.registry.get_all_schemas(),
                ):
                    if cancel_event is not None and cancel_event.is_set():
                        yield {"type": "cancelled"}
                        return
                    if kind == "delta":
                        content_parts.append(payload)
                        # 工具轮通常没有 delta；一旦最终有 tool_calls，
                        # 前端会按 tool_start 丢弃本轮朗读
                        yield {"type": "token", "content": payload}
                    elif kind == "done":
                        tool_calls = payload.get("tool_calls")
                        if not content_parts and payload.get("content"):
                            # 非流式降级后端：content 只在 done 里
                            content_parts.append(payload["content"])
                            yield {"type": "token", "content": payload["content"]}
                        reasoning = payload.get("reasoning", "") or ""
            except Exception as e:
                logger.exception("[Agent/Voice] LLM stream failed at step %d", step)
                yield {"type": "error", "message": f"AI 调用失败：{e}"}
                return

            content = "".join(content_parts)

            # ── 无工具调用：本轮即最终回复 ──
            if not tool_calls:
                yield {"type": "final", "content": content or "完成。"}
                return

            # ── 分流 confirm_action 与普通工具 ──
            confirm_tc = None
            action_tcs: list[dict] = []
            for tc in tool_calls:
                if tc["function"]["name"] == "confirm_action":
                    confirm_tc = tc
                else:
                    action_tcs.append(tc)

            # 语音链路不触发 IM progress_callback（否则用户微信会收到调试消息）

            if confirm_tc is not None:
                try:
                    args = json.loads(confirm_tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    args = {"action": "执行操作", "details": ""}
                action = args.get("action", "执行操作")
                details = args.get("details", "")
                question = f"需要你确认：{action}"
                if details:
                    question += f"\n{details}"
                question += "\n说「确定」执行，说「取消」放弃。"

                all_tcs = [confirm_tc] + action_tcs
                messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "reasoning_content": reasoning,
                    "tool_calls": all_tcs,
                })
                with self._state_lock:
                    self._pending_confirms[conversation_key] = {
                        "question": question,
                        "messages": messages,
                        "system": system,
                        "confirm_tool_call_id": confirm_tc["id"],
                        "action_tcs": action_tcs,
                    }
                yield {"type": "confirm", "question": question}
                return

            # 记录工具调用轮（DeepSeek 要求保留 reasoning_content）
            messages.append({
                "role": "assistant",
                "content": None,
                "reasoning_content": reasoning,
                "tool_calls": tool_calls,
            })

            # ── 逐个执行工具 ──
            with self._state_lock:
                bypass = conversation_key in self._bypass_confirms
                self._bypass_confirms.discard(conversation_key)

            for tc in action_tcs:
                if cancel_event is not None and cancel_event.is_set():
                    yield {"type": "cancelled"}
                    return

                name = tc["function"]["name"]
                try:
                    args = self._parse_args(tc)
                except json.JSONDecodeError:
                    args = {}

                logger.info("[Agent/Voice] Tool call: %s(%s)", name, args)
                yield {"type": "tool_start", "name": name}

                tool = self._tools.registry.get(name)
                if tool and getattr(tool, "requires_confirm", False) and not bypass:
                    result = f"工具「{name}」是写操作，必须先调用 confirm_action 获取用户确认。"
                else:
                    try:
                        result = self._tools.registry.execute(name, args)
                    except Exception as e:
                        result = f"工具「{name}」执行出错: {e}"
                        logger.warning("[Agent/Voice] tool %s failed: %s", name, e)

                result = result or "（工具返回为空）"
                logger.info("[Agent/Voice] Tool result: %s...", result[:120])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
                yield {"type": "tool_done", "name": name,
                       "preview": result[:160]}

        logger.warning("[Agent/Voice] Exceeded max_steps=%d", self._max_steps)
        yield {"type": "final", "content": "我还在思考中，能再说一遍吗？"}

    # ── ReAct loop core ────────────────────────────────────────────

    def _react_loop(self, system: str,
                    messages: list[dict], *,
                    source_platform: str = "wechat",
                    source_target: str | None = None,
                    conversation_key: str = "default") -> str:
        """The main ReAct reasoning + acting loop."""
        for step in range(1, self._max_steps + 1):
            logger.info("[Agent] Step %d/%d — messages=%d",
                        step, self._max_steps, len(messages))

            # ── Call LLM ─────────────────────────────────────────
            try:
                content, tool_calls, reasoning = self._llm.agent_chat(
                    system_prompt=system,
                    messages=messages,
                    tools=self._tools.registry.get_all_schemas(),
                )
            except Exception as e:
                logger.exception("[Agent] LLM call failed at step %d", step)
                return f"AI 调用失败：{e}"

            logger.info("[Agent] LLM response — content=%s, tool_calls=%d",
                        content[:60] if content else "(null)",
                        len(tool_calls) if tool_calls else 0)
            if reasoning:
                logger.info("[Agent] Step %d reasoning: %s",
                            step, reasoning)

            # ── Push progress via callback (iLink etc.) ─────────────
            if tool_calls and self._progress_callback:
                tool_names = ", ".join(
                    tc["function"]["name"] for tc in tool_calls
                )
                try:
                    self._progress_callback(
                        step,
                        self._max_steps,
                        tool_names,
                        reasoning,
                        source_platform=source_platform,
                        source_target=source_target,
                    )
                except Exception as e:
                    logger.warning("[Agent] progress_callback failed: %s", e)

            # ── LLM chose to reply — task complete ────────────────
            if not tool_calls:
                logger.info("[Agent] Complete after %d steps", step)
                return content or "完成。"

            # ── Process tool calls ────────────────────────────────
            # 1) Check for confirm_action (intercept before execution)
            # 2) Check requires_confirm — only gates config/file-changing writes,
            #    NOT pipeline triggers like run_digest/run_oa_digest

            with self._state_lock:
                bypass_this_round = conversation_key in self._bypass_confirms
                self._bypass_confirms.discard(conversation_key)

            confirm_tc = None
            action_tcs: list[dict] = []
            for tc in tool_calls:
                tc_name = tc["function"]["name"]
                if tc_name == "confirm_action":
                    confirm_tc = tc
                else:
                    action_tcs.append(tc)

            if confirm_tc is not None:
                logger.info("[Agent] Intercepting confirm_action — %d pending action(s): %s",
                            len(action_tcs),
                            [t["function"]["name"] for t in action_tcs])
                return self._intercept_confirm(
                    system, messages, confirm_tc, action_tcs,
                    content or "", reasoning or "", conversation_key=conversation_key,
                )

            # ── Execute tools ───────────────────────────────────────
            for tc in action_tcs:
                name = tc["function"]["name"]
                try:
                    args = self._parse_args(tc)
                except json.JSONDecodeError:
                    args = {}

                logger.info("[Agent] Tool call: %s(%s)", name, args)

                # Check requires_confirm
                tool = self._tools.registry.get(name)
                if tool and tool.requires_confirm and not bypass_this_round:
                    logger.info("[Agent] Tool '%s' requires confirm but bypass=False — rejecting", name)
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "",
                        "tool_calls": [tc],
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"工具「{name}」是写操作，必须先调用 confirm_action 获取用户确认。",
                    })
                    continue

                # Execute tool
                result = self._tools.registry.execute(name, args)

                logger.info("[Agent] Tool result: %s...",
                            result[:120] if result else "(empty)")

                # Append tool call + result to conversation
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": reasoning,
                    "tool_calls": [tc],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result or "（工具返回为空）",
                })

        # Exceeded max steps
        logger.warning("[Agent] Exceeded max_steps=%d", self._max_steps)
        return "我还在思考中，能再说一遍吗？"

    # ── confirm_action state machine ───────────────────────────────

    CONFIRM_YES_KEYWORDS = (
        "确定", "确认", "好", "是", "嗯", "行",
        "可以", "ok", "yes", "y", "对的", "没错",
        "同意", "要",
    )
    CONFIRM_NO_KEYWORDS = (
        "不", "取消", "算了", "不用", "别",
        "no", "n", "不要",
    )

    def _intercept_confirm(self, system: str,
                           messages: list[dict],
                           confirm_tc: dict,
                           action_tcs: list[dict],
                           llm_content: str = "",
                           reasoning: str = "",
                           conversation_key: str = "default") -> str:
        """Intercept a confirm_action call from the LLM.

        Saves both the confirm state AND any pending action_tcs.
        On user confirm, action_tcs are executed directly without
        going back to the LLM for re-generation.

        Args:
            llm_content: The text content from the LLM's response
                         (may be empty if LLM only returned tool_calls).

        Returns:
            Confirmation question text for the user.
        """
        try:
            args = json.loads(confirm_tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = {"action": "执行操作", "details": ""}

        action = args.get("action", "执行操作")
        details = args.get("details", "")

        question = f"⚠️ 需要确认：{action}"
        if details:
            question += f"\n{details}"
        question += "\n\n回复「确定」执行，回复「取消」放弃。"

        # Store ALL tool_calls (confirm + actions) in assistant message
        all_tcs = [confirm_tc] + (action_tcs or [])
        messages.append({
            "role": "assistant",
            "content": llm_content or None,
            "reasoning_content": reasoning,
            "tool_calls": all_tcs,
        })

        with self._state_lock:
            self._pending_confirms[conversation_key] = {
            "question": question,
            "messages": messages,
            "system": system,
            "confirm_tool_call_id": confirm_tc["id"],
            "action_tcs": action_tcs or [],
        }

        logger.info("[Agent] Pending confirm: action=%s, action_tcs=%s",
                    action, [t["function"]["name"] for t in (action_tcs or [])])
        return question

    def _handle_confirm_response(self,
                                 user_message: str,
                                 fresh_messages: list[dict],
                                 *,
                                 source_platform: str = "wechat",
                                 source_target: str | None = None,
                                 conversation_key: str = "default") -> str:
        """Handle user's response to a pending confirmation question.

        If the user confirms, executes pending action_tcs directly
        (no LLM re-generation needed) and continues the ReAct loop.
        If cancelled, injects cancellation for all pending actions.
        If unclear, re-asks the question.

        Returns:
            Either the continued ReAct loop result, or a re-ask.
        """
        with self._state_lock:
            pending = self._pending_confirms.get(conversation_key)
        if pending is None:
            return "当前没有待确认的操作。"

        clean = user_message.strip().lower()
        logger.info("[Agent] _handle_confirm_response — user_response='%s'", clean[:40])

        # ── User confirmed: execute pending actions directly ─────────
        if self._is_confirm_yes(clean):
            logger.info("[Agent] User CONFIRMED — executing %d pending action(s)",
                        len(pending.get("action_tcs", [])))

            # Inject confirm_action result
            pending["messages"].append({
                "role": "tool",
                "tool_call_id": pending["confirm_tool_call_id"],
                "content": "用户已确认操作，请继续执行。",
            })

            # Execute each pending action directly
            for tc in pending.get("action_tcs", []):
                name = tc["function"]["name"]
                args = self._parse_args(tc)
                logger.info("[Agent] Executing confirmed action: %s(%s)", name, args)
                try:
                    result = self._tools.registry.execute(name, args)
                    logger.info("[Agent] Confirmed action done: %s — result=%s",
                                name, (result or "")[:120])
                except Exception as e:
                    result = f"执行失败: {e}"
                    logger.warning("[Agent] Confirmed action failed: %s — %s", name, e)
                pending["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            # Append user's confirm message so LLM sees it
            pending["messages"].extend(fresh_messages)
            with self._state_lock:
                self._pending_confirms.pop(conversation_key, None)
                self._bypass_confirms.add(conversation_key)
            return self._react_loop(
                pending["system"], pending["messages"],
                source_platform=source_platform,
                source_target=source_target,
                conversation_key=conversation_key,
            )

        # ── User cancelled: inject cancellation for all actions ──────
        if self._is_confirm_no(clean):
            logger.info("[Agent] User CANCELLED — cancelling %d pending action(s)",
                        len(pending.get("action_tcs", [])))
            pending["messages"].append({
                "role": "tool",
                "tool_call_id": pending["confirm_tool_call_id"],
                "content": "用户取消了操作。请告知用户操作已取消。",
            })
            for tc in pending.get("action_tcs", []):
                pending["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "用户取消了操作，该操作未执行。",
                })
            # Append user's cancel message so LLM sees it
            pending["messages"].extend(fresh_messages)
            with self._state_lock:
                self._pending_confirms.pop(conversation_key, None)
            return self._react_loop(
                pending["system"], pending["messages"],
                source_platform=source_platform,
                source_target=source_target,
                conversation_key=conversation_key,
            )

        # ── Unclear — re-ask ─────────────────────────────────────
        logger.info("[Agent] Confirm response unclear, re-asking: '%s'", clean[:60])
        return pending.get("question", "请回复「确定」执行，回复「取消」放弃。")

    @staticmethod
    def _is_confirm_yes(text: str) -> bool:
        """Check if user text means 'yes' to a confirmation."""
        lower = text.lower()
        for kw in AgentEngine.CONFIRM_YES_KEYWORDS:
            if kw in lower:
                return True
        return False

    @staticmethod
    def _is_confirm_no(text: str) -> bool:
        """Check if user text means 'no' to a confirmation."""
        lower = text.lower()
        for kw in AgentEngine.CONFIRM_NO_KEYWORDS:
            if kw in lower:
                return True
        return False

    # ── Internal helpers ───────────────────────────────────────────

    @staticmethod
    def _parse_args(tc: dict) -> dict:
        """Parse tool call arguments (handles str or dict)."""
        raw = tc["function"].get("arguments")
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def _build_system_prompt(self, conversation_key: str = "default") -> str:
        """Build the system prompt with tool descriptions and memories."""
        desc = self._tools.registry.get_descriptions()
        prompt = AGENT_SYSTEM_PROMPT.replace("{tool_descriptions}", desc)
        prompt = prompt.replace("{max_steps}", str(self._max_steps))

        # Append long-term memories
        memories = self._load_memories(MEMORY_LOAD_COUNT, conversation_key=conversation_key)
        if memories:
            memory_lines = "\n".join(f"- {m}" for m in memories)
            prompt += f"\n\n## 对话记忆\n{memory_lines}"

        return prompt

    # ── Public accessors ─────────────────────────────────────────────

    def get_tool_descriptions(self) -> str:
        """Get the current tool descriptions text (for welcome message)."""
        return self._tools.registry.get_descriptions()

    def clear_pending_confirm(self, conversation_key: str = "default") -> None:
        """Clear pending confirmation for one conversation."""
        with self._state_lock:
            self._pending_confirms.pop(conversation_key or "default", None)
            self._bypass_confirms.discard(conversation_key or "default")

    # ── Memory system ────────────────────────────────────────────────

    def _init_memory_db(self) -> None:
        """Create agent_memory table if not exists."""
        try:
            AGENT_MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(AGENT_MEMORY_DB))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS agent_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_key TEXT NOT NULL DEFAULT 'default',
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_memory)")}
            if "conversation_key" not in columns:
                conn.execute("ALTER TABLE agent_memory ADD COLUMN conversation_key TEXT NOT NULL DEFAULT 'default'")
            conn.commit()
            conn.close()
            logger.info("[Agent] Memory DB initialized")
        except Exception as e:
            logger.warning("[Agent] Failed to init memory DB: %s", e)

    def _load_memories(self, limit: int = 3, conversation_key: str = "default") -> list[str]:
        """Load recent long-term memories (oldest first)."""
        try:
            conn = sqlite3.connect(str(AGENT_MEMORY_DB))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT summary FROM agent_memory WHERE conversation_key = ? ORDER BY id DESC LIMIT ?",
                (conversation_key or "default", limit),
            ).fetchall()
            conn.close()
            return [r["summary"] for r in reversed(rows)]  # oldest first
        except Exception as e:
            logger.warning("[Agent] Failed to load memories: %s", e)
            return []

    def _save_memory(self, summary: str, conversation_key: str = "default") -> None:
        """Save a long-term memory entry."""
        try:
            conn = sqlite3.connect(str(AGENT_MEMORY_DB))
            conn.execute(
                "INSERT INTO agent_memory (conversation_key, summary) VALUES (?, ?)",
                (conversation_key or "default", summary),
            )
            conn.commit()
            conn.close()
            logger.info("[Agent] Saved memory: %s", summary[:60])
        except Exception as e:
            logger.warning("[Agent] Failed to save memory: %s", e)

    def _consolidate_memory(self, conversation_key: str = "default") -> None:
        """Summarize one conversation's short-term history."""
        with self._state_lock:
            history = list(self._histories.get(conversation_key, []))
        if not history:
            return

        history_lines = []
        for user_msg, agent_reply in history:
            history_lines.append(f"用户: {user_msg[:100]}")
            history_lines.append(f"助手: {agent_reply[:100]}")
        history_text = "\n".join(history_lines)

        try:
            prompt = (
                "请用一句话总结以下对话中提到的用户需求、偏好或重要信息，"
                "以便后续对话中回顾。如果没有值得记住的内容，回复「无」。\n\n"
                f"{history_text}"
            )
            summary = self._llm.chat(
                message=prompt,
                requester_name="system",
                group_name="记忆总结",
            )
            summary = summary.strip()
            if summary and summary != "无":
                self._save_memory(summary, conversation_key=conversation_key)
            with self._state_lock:
                self._histories[conversation_key] = []
            logger.info("[Agent] Memory consolidated, history cleared for %s", conversation_key)
        except Exception as e:
            logger.warning("[Agent] Memory consolidation failed: %s", e)
