"""上下文摘要（按 token 判据 + 轮次兜底），替代原先的"逐条折叠"。

为什么不做折叠（2026-11-12 与用户确认）：
- 折叠只是把每条老结果砍成一行，**信息一丢就找不回语义**（哪个数字对应哪个口径说不清）；
- 摘要能把"多条结果"压成"一段结论"，语义保留更好，且天然带口径。

判据（用户拍板）：
- **主判据：工具结果的 token 估算**（中文 1.5 字/token，ASCII 4 字符/token）
- **兜底：距上次摘要 ≥ 4 轮**（防止"轮数多但每条都小"时一直不压）

保留策略：
- 最近 3 条工具结果保留原文；
- 最近 2 轮对话不动；
- 摘要写进**最老的那条工具结果**里（保持消息类型/顺序不变，避免某些厂商拒绝中途插 system）。
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

MARKER = "ContextSummary"

# 工具结果累计超过这个 token 数 → 摘要
TOOL_BUDGET_TOKENS = 2000
# 距上次摘要超过这么多**轮** → 强制摘要（兜底）
MAX_ROUNDS_SINCE_SUMMARY = 4
# 最近这么多条工具结果保持原文
KEEP_RECENT_RESULTS = 3
# 最近这么多轮对话保持原文
KEEP_RECENT_ROUNDS = 2
# 摘要本身的长度上限（字数，中文）
SUMMARY_MAX_CHARS = 320

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII = re.compile(r"[\x00-\x7f]")


def est_tokens(text: str) -> int:
    """粗估 token：中文 1.5 字/token，ASCII 4 字符/token。

    只用相对比较，绝对值不必精确；比按"字符"判断稳定得多
    （同样 8K 字符，中文约 5.3K token，英文约 2K token，按字符判断会差 2.5 倍）。
    """
    if not text:
        return 0
    zh = len(_CJK.findall(text))
    ascii_cnt = len(_ASCII.findall(text))
    other = max(0, len(text) - zh - ascii_cnt)
    return int(zh / 1.5 + ascii_cnt / 4 + other / 2) + 1


_SUMMARY_PROMPT = (
    "把下面这些工具调用结果压缩成一段中文摘要，供后续对话直接使用。\n"
    "必须保留：\n"
    "1. 关键数字（含单位）与其口径/时间窗口；\n"
    "2. 表名或数据来源；\n"
    "3. 结论性事实（谁多谁少、有无异常）；\n"
    "4. 若某次查询报错或无结果，写清楚是哪个查询、报什么错。\n"
    f"不要保留：原始表格、重复行、寒暄。控制在 {SUMMARY_MAX_CHARS} 字以内。\n\n"
)


# 摘要调用的硬超时：模型卡住时宁可不要摘要，也不能拖死整轮
DIGEST_TIMEOUT_S = 25.0


def _fallback(texts: list[str]) -> str:
    joined = "\n".join(t[:300] for t in texts)
    return f"（摘要不可用，以下是各结果开头）\n{joined}"[: SUMMARY_MAX_CHARS * 3]


def _with_timeout(fn, fallback: str, timeout_s: float = DIGEST_TIMEOUT_S) -> str:
    """在线程里跑 fn，超时立即返回 fallback。

    注意不能用 `with ThreadPoolExecutor(...)`：退出时会 wait=True 等线程跑完，超时形同虚设。
    """
    pool = None
    try:
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=1)
        out = pool.submit(fn).result(timeout=timeout_s)
        pool.shutdown(wait=False)
        return out
    except Exception:  # noqa: BLE001
        if pool is not None:
            try:
                pool.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
        return fallback


class ContextSummarizerMiddleware(AgentMiddleware):
    """工具结果累计超预算（或轮数兜底）时，把老结果摘成一段。"""

    def __init__(self, model: Any = None,
                 tool_budget_tokens: int = TOOL_BUDGET_TOKENS,
                 max_rounds: int = MAX_ROUNDS_SINCE_SUMMARY,
                 keep_recent: int = KEEP_RECENT_RESULTS,
                 keep_rounds: int = KEEP_RECENT_ROUNDS) -> None:
        super().__init__()
        self.model = model
        self.tool_budget_tokens = tool_budget_tokens
        self.max_rounds = max_rounds
        self.keep_recent = keep_recent
        self.keep_rounds = keep_rounds
        self._rounds_since_summary = 0

    # ---------- 判据 ----------
    def _should_summarize(self, messages: list) -> tuple[bool, list[int]]:
        if self.model is None or len(messages) < self.keep_recent + 2:
            return False, []
        tool_idx = [i for i, m in enumerate(messages)
                    if getattr(m, "type", "") == "tool"
                    and isinstance(getattr(m, "content", None), str)]
        if len(tool_idx) <= self.keep_recent:
            return False, []
        # 至少保留最近 keep_recent 条原文
        base = tool_idx[: -self.keep_recent]
        # 再保护最近 keep_rounds 轮；但如果保护后一个都不剩（所有结果都在本轮），
        # 就退回到"只保护最近 keep_recent 条"，否则本轮内 10 条大结果永远压不掉。
        recent_start = self._recent_round_start(messages)
        candidates = [i for i in base if i < recent_start] if recent_start > 0 else base
        if not candidates:
            candidates = base
        if not candidates:
            return False, []
        total = sum(est_tokens(messages[i].content) for i in candidates)
        if total >= self.tool_budget_tokens or self._rounds_since_summary >= self.max_rounds:
            return True, candidates
        return False, []

    @staticmethod
    def _recent_round_start(messages: list) -> int:
        """最近 keep_rounds 轮（以 human 消息为界）的起始下标。"""
        human_idx = [i for i, m in enumerate(messages) if getattr(m, "type", "") == "human"]
        if len(human_idx) <= KEEP_RECENT_ROUNDS:
            return 0
        return human_idx[-KEEP_RECENT_ROUNDS]

    # ---------- 摘要 ----------
    def _digest(self, texts: list[str]) -> str:
        body = "\n\n".join(f"[结果 {i + 1}] {t[:1200]}" for i, t in enumerate(texts))
        prompt = _SUMMARY_PROMPT + body
        return _with_timeout(lambda: self._call_model(prompt), fallback=_fallback(texts))

    async def _adigest(self, texts: list[str]) -> str:
        body = "\n\n".join(f"[结果 {i + 1}] {t[:1200]}" for i, t in enumerate(texts))
        prompt = _SUMMARY_PROMPT + body
        # 异步路径直接 await，但给整体加超时，避免模型卡住拖死整轮
        try:
            return await asyncio.wait_for(self._acall_model(prompt), timeout=DIGEST_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            return _fallback(texts)

    def _call_model(self, prompt: str) -> str:
        resp = self.model.invoke([HumanMessage(content=prompt)])
        out = getattr(resp, "content", None) or str(resp)
        return (out if isinstance(out, str) else str(out)).strip()[: SUMMARY_MAX_CHARS * 3]

    async def _acall_model(self, prompt: str) -> str:
        resp = await self.model.ainvoke([HumanMessage(content=prompt)])
        out = getattr(resp, "content", None) or str(resp)
        return (out if isinstance(out, str) else str(out)).strip()[: SUMMARY_MAX_CHARS * 3]

    def _apply(self, messages: list, idx: list[int], digest: str) -> dict[str, Any]:
        fresh = list(messages)
        for j, i in enumerate(idx):
            old = messages[i]
            new_content = (
                f"【{MARKER}】以下是此前 {len(idx)} 条工具结果的摘要：\n{digest}"
                if j == 0 else f"（{MARKER}）已并入上一条摘要"
            )
            fresh[i] = ToolMessage(
                content=new_content,
                tool_call_id=getattr(old, "tool_call_id", "") or "",
                name=getattr(old, "name", None) or "",
            )
        self._rounds_since_summary = 0
        return {"messages": fresh}

    # ---------- 钩子 ----------
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = (state or {}).get("messages") or []
        if messages and getattr(messages[-1], "type", "") == "human":
            self._rounds_since_summary += 1
        ok, idx = self._should_summarize(messages)
        if not ok:
            return None
        texts = [messages[i].content for i in idx]
        return self._apply(messages, idx, self._digest(texts))

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = (state or {}).get("messages") or []
        if messages and getattr(messages[-1], "type", "") == "human":
            self._rounds_since_summary += 1
        ok, idx = self._should_summarize(messages)
        if not ok:
            return None
        texts = [messages[i].content for i in idx]
        return self._apply(messages, idx, await self._adigest(texts))
