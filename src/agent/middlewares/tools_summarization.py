"""摘要中间件工厂。

两层能力：
1. **自动摘要**：上下文接近窗口上限时自动压缩历史消息；
2. **主动摘要**：暴露 `compact_conversation` 工具，Agent 可在关键节点主动调用。

⚠️ 关于触发阈值（2026-11-12 调整）：
框架默认是 **85%**，对 32K 窗口 ≈ 27K、对 24K 有效窗口 ≈ 20.4K。
但实测单次调用的输入峰值已经到 20.6K —— 也就是说**默认阈值几乎等于永不触发**，
下一次工具结果就直接爆窗口。这里改成 **70%**（32K → 22.4K；有效 24K → 16.8K），
留出"摘要调用本身也要占窗口"的余量。
"""

from typing import Union

from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
)
from deepagents.backends import CompositeBackend
from langchain_core.language_models import BaseChatModel

# 上下文占用到这个比例就触发自动摘要（框架默认 0.85，太晚）
# 上下文到这个 token 数就触发自动摘要。
# 为什么不用框架默认的 85%：默认对 32K 窗口 ≈ 27K，而实测单次输入峰值已到 20.6K，
# 基本等于永不触发；17K 更早，能留出摘要调用本身占用的空间。
# 为什么不用 fraction：那需要模型带 profile={"max_input_tokens":...}，
# 我们的 DeepSeek 接入没有，会直接报错。
SUMMARY_TRIGGER_TOKENS = 17000
# 摘要后保留最近多少条消息原文
SUMMARY_KEEP_MESSAGES = 20


def build_summarization_middleware(
    backend: CompositeBackend,
    model: Union[str, BaseChatModel] = "gpt-4o-mini",
    trigger_tokens: int | None = None,
) -> SummarizationToolMiddleware:
    """构建摘要中间件（自动摘要 + 主动压缩工具）。

    Args:
        backend: 沙箱后端（压缩前的完整历史会落盘，便于事后追溯）。
        model: 生成摘要用的模型，建议用小模型。
        trigger_tokens: 触发阈值（**绝对 token 数**）。不传则按当前
            `CONTEXT_PROFILE` 档位里的 `summary_trigger_tokens`（本地 32K 与大窗口
            API 的最优阈值不同，所以做成档位而不是常量）。
    """
    if trigger_tokens is None:
        from agent.context_profile import get_profile

        trigger_tokens = get_profile().summary_trigger_tokens
    inner = SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=("tokens", trigger_tokens),
        keep=("messages", SUMMARY_KEEP_MESSAGES),
    )
    return SummarizationToolMiddleware(summarization=inner)
