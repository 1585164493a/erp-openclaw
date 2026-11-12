"""工具调用循环熔断 + 优雅降级 + 瞬时错误退避重试。

背景（2026-11-12 实测）：
- H21「每个月采购单数量」被路由到 `procurement-order`（只有 order_* 工具，给不了聚合），
  它反复"换姿势再试"，模型调用被烧到 20 次上限后整轮失败；
- 现有 `SandboxCircuitBreakerMiddleware` 只管沙箱故障，**不管"同一件事反复做"**。

三道防线：
1. **同名同参 连续 2 次** → 降级：不再执行，直接回上次结果 + 提示换路子；
2. **同名 连续 3 次**（参数在变）→ 同样降级（防止"换个关键词再试"式打转）；
3. 到了 5 次还在打转 → `before_model` 里 jump_to=end 硬中止。

另外补"失败分类 + 退避"：
- 只有**瞬时错误**（超时/限流/锁等待/连接重置）才重试，指数退避 1s→2s（带抖动）；
- **终态错误**（SQL 语法、字段不存在、权限不足）立即返回，重试没有意义。

降级的方式很关键：返回的是一个**假 ToolMessage**（冒充工具结果），
所以模型看到的是"结果 + 建议"，而不是异常 —— 整轮不会被打断，它会自然换工具。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.runtime import Runtime

from agent.middlewares.sandbox_breaker import AgentState

IDENTICAL_LIMIT = 2       # 同名同参 2 次 → 降级
SAME_NAME_LIMIT = 3       # 同名 3 次（参数在变）→ 降级
HARD_STOP_LIMIT = 5       # 同名 5 次 → 硬中止

_MAX_RETRIES = 2          # 瞬时错误最多重试 2 次
_BASE_BACKOFF = 1.0       # 1s、2s（带抖动）

MARKER = "ToolLoopGuard"

_TRANSIENT = re.compile(
    r"(timeout|timed out|connection (reset|refused|aborted)|temporarily unavailable|"
    r"rate limit|too many connections|deadlock|lock wait timeout|"
    r"server has gone away|lost connection|\b(429|500|502|503|504)\b)",
    re.IGNORECASE,
)
_TERMINAL = re.compile(
    r"(unknown column|syntax error|doesn't exist|does not exist|no such table|"
    r"permission denied|access denied|unauthorized|forbidden|invalid (argument|parameter)|"
    r"validation|keyerror|not found|\b(400|401|403|404|422)\b)",
    re.IGNORECASE,
)


def classify_error(text: str) -> str:
    """把工具输出分成 transient / terminal / unknown。

    - transient：网络抖动、限流、锁等待 —— 值得退避重试
    - terminal：SQL 写错、字段不存在、权限不足 —— 重试只会再错一次
    """
    if not text:
        return "unknown"
    if _TERMINAL.search(text):
        return "terminal"
    if _TRANSIENT.search(text):
        return "transient"
    return "unknown"


def _text_of(result: Any) -> str:
    content = getattr(result, "content", result)
    return content if isinstance(content, str) else str(content)


def _looks_failed(result: Any) -> bool:
    """工具失败通常是"返回一段错误文本"而不是抛异常，这里两种都认。"""
    if getattr(result, "status", None) == "error":
        return True
    text = _text_of(result).lstrip()
    return (
        text.startswith("[SQL 执行失败]")
        or text.startswith("[SQL 被拒绝]")
        or text.startswith("[错误]")
        or "Traceback (most recent call last)" in text
    )


def _make_tool_message(content: str, tool_call: dict) -> ToolMessage:
    """构造一个"假的"工具结果，冒充真实调用返回，避免整轮中断。"""
    return ToolMessage(
        content=content,
        tool_call_id=tool_call.get("id") or "",
        name=tool_call.get("name") or "",
    )


def _signature(tool_call: dict) -> tuple[str, str]:
    name = tool_call.get("name") or "?"
    args = tool_call.get("args")
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        blob = str(args)
    return name, blob[:400]


class ToolLoopGuardMiddleware(AgentMiddleware):
    """同名同参 / 同名连续 → 降级；瞬时错误 → 退避重试；打转到底 → 硬中止。"""

    state_schema = AgentState

    def __init__(self, identical_limit: int = IDENTICAL_LIMIT,
                 same_name_limit: int = SAME_NAME_LIMIT,
                 hard_stop_limit: int = HARD_STOP_LIMIT,
                 max_retries: int = _MAX_RETRIES) -> None:
        super().__init__()
        self.identical_limit = identical_limit
        self.same_name_limit = same_name_limit
        self.hard_stop_limit = hard_stop_limit
        self.max_retries = max_retries
        # 一次请求（= 一个中间件实例）内的**失败**计数
        self._fail_sig: dict[tuple[str, str], int] = {}
        self._fail_name: dict[str, int] = {}
        self._last_result: dict[tuple[str, str], Any] = {}

    # ---------------- 核心：只对"失败"计数 ----------------
    def _precheck(self, tool_call: dict) -> tuple[tuple[str, str], int, int, str]:
        name = tool_call.get("name") or "?"
        sig = _signature(tool_call)
        n_sig = self._fail_sig.get(sig, 0) + 1
        n_name = self._fail_name.get(name, 0) + 1
        return sig, n_sig, n_name, name

    def _on_success(self, tool_call: dict) -> None:
        """成功即清零 —— 连续 3 次不同月份的成功查询是正常业务，不是打转。"""
        name = tool_call.get("name") or "?"
        self._fail_sig.pop(_signature(tool_call), None)
        self._fail_name.pop(name, None)

    def _on_failure(self, tool_call: dict) -> None:
        name = tool_call.get("name") or "?"
        sig = _signature(tool_call)
        self._fail_sig[sig] = self._fail_sig.get(sig, 0) + 1
        self._fail_name[name] = self._fail_name.get(name, 0) + 1

    def _should_degrade(self, sig, n_sig, n_name, name) -> str | None:
        """只有在"失败"累积到阈值时才降级。"""
        if n_sig >= self.identical_limit:
            return f"同一调用（{name} 相同参数）已连续失败 {n_sig} 次"
        if n_name >= self.same_name_limit:
            return f"「{name}」已连续失败 {n_name} 次（参数在变也没成）"
        return None

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        tool_call = getattr(request, "tool_call", {}) or {}
        sig, n_sig, n_name, name = self._precheck(tool_call)

        # 先看历史上这个调用失败过几次；达到阈值才降级
        trigger = self._should_degrade(sig, self._fail_sig.get(sig, 0) + 1,
                                       self._fail_name.get(name, 0) + 1, name)
        if trigger:
            prev = self._last_result.get(sig)
            prev_text = f"\n\n上次的结果：\n{_text_of(prev)[:1200]}" if prev is not None else ""
            return _make_tool_message(
                f"{MARKER}: {trigger}，已停止调用该工具。"
                "**不要再用同样思路重试**：换工具（如 sql_query / sql_schema_overview），"
                f"或直接基于已有结论回答。{prev_text}",
                {"id": tool_call.get("id") or "", "name": name},
            )

        # 瞬时错误退避重试；终态错误直接返回
        delay = _BASE_BACKOFF
        for attempt in range(self.max_retries + 1):
            try:
                result = handler(request)
            except Exception as e:  # noqa: BLE001 —— 工具抛异常也要分类
                kind = classify_error(str(e))
                if kind == "transient" and attempt < self.max_retries:
                    time.sleep(delay + random.uniform(0, 0.3))
                    delay *= 2
                    continue
                self._on_failure(tool_call)
                raise
            if not _looks_failed(result):
                self._on_success(tool_call)
                self._last_result[sig] = result
                return result
            kind = classify_error(_text_of(result))
            if kind == "transient" and attempt < self.max_retries:
                time.sleep(delay + random.uniform(0, 0.3))
                delay *= 2
                continue
            # 终态错误：原样返回，让模型据此改；同时记一次失败
            self._on_failure(tool_call)
            self._last_result[sig] = result
            return result

        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        tool_call = getattr(request, "tool_call", {}) or {}
        sig, n_sig, n_name, name = self._precheck(tool_call)

        trigger = self._should_degrade(sig, self._fail_sig.get(sig, 0) + 1,
                                       self._fail_name.get(name, 0) + 1, name)
        if trigger:
            prev = self._last_result.get(sig)
            prev_text = f"\n\n上次的结果：\n{_text_of(prev)[:1200]}" if prev is not None else ""
            return _make_tool_message(
                f"{MARKER}: {trigger}，已停止调用该工具。"
                "**不要再用同样思路重试**：换工具（如 sql_query / sql_schema_overview），"
                f"或直接基于已有结论回答。{prev_text}",
                {"id": tool_call.get("id") or "", "name": name},
            )

        delay = _BASE_BACKOFF
        for attempt in range(self.max_retries + 1):
            try:
                result = await handler(request)
            except Exception as e:  # noqa: BLE001
                kind = classify_error(str(e))
                if kind == "transient" and attempt < self.max_retries:
                    await asyncio.sleep(delay + random.uniform(0, 0.3))
                    delay *= 2
                    continue
                self._on_failure(tool_call)
                raise
            if not _looks_failed(result):
                self._on_success(tool_call)
                self._last_result[sig] = result
                return result
            kind = classify_error(_text_of(result))
            if kind == "transient" and attempt < self.max_retries:
                await asyncio.sleep(delay + random.uniform(0, 0.3))
                delay *= 2
                continue
            self._on_failure(tool_call)
            self._last_result[sig] = result
            return result

        return await handler(request)

    # ---------------- 兜底：还不收敛就中止 ----------------
    @hook_config(can_jump_to=["end"])
    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = state.get("messages", []) or []
        if not messages:
            return None
        last_name, n = _trailing_failures(messages)
        if n < self.hard_stop_limit:
            return None
        content = (
            f"{MARKER}: 「{last_name}」已连续失败 {n} 次仍未收敛，本轮已中止。"
            "请换一种方法（如 sql_query 聚合），或者如实说明拿不到该数据。"
        )
        return {"jump_to": "end", "messages": [AIMessage(content=content)]}

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        return self.before_model(state, runtime)


def _trailing_failures(messages: list) -> tuple[str, int]:
    """从后往前数"同一个工具连续失败"的次数（成功的调用不计数）。"""
    last = None
    count = 0
    for msg in reversed(messages):
        mtype = getattr(msg, "type", "")
        if mtype in {"human", "system"}:
            break
        if mtype != "tool":
            continue
        if not _looks_failed(msg):
            break          # 遇到成功就停：成功即归零
        name = getattr(msg, "name", None) or ""
        if last is None:
            last, count = name, 1
        elif name == last:
            count += 1
        else:
            break
    return last or "", count
