"""上下文策略档位（ContextProfile）—— 阈值不再写死，改成跟部署形态走。

为什么需要（2026-11-12）：
同一套压缩阈值，在"本地 32K vLLM"和"大窗口 API"上最优解完全不同：
- 本地 32K：显存/并发都紧、prefill 慢 → **越早压越好**
- API 128K：窗口不缺、但每 token 花钱 → **尽量少压**（压一次要付摘要调用的钱，还丢信息）

所以把「窗口大小 / 摘要触发点 / 工具结果预算 / 下沉阈值 / 摘要长度」抽成档案，
中间件读档案而不是读模块常量。

当前状态（用户决定）：先都用同一个 API，两个档位数值先取同一组，
接口留好，将来切成本地 vLLM 只改一个环境变量。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextProfile:
    """一套上下文策略参数。"""

    name: str
    window_tokens: int            # 模型窗口上限（用于算占比、告警）
    summary_trigger_tokens: int   # 上下文到这个 token 数触发自动摘要
    tool_budget_tokens: int       # 工具结果累计超过这个数就摘要
    sink_chars: int               # 单条结果超过这个字符数就下沉
    sink_lines: int               # 或超过这么多行就下沉
    head_lines: int               # 下沉后保留的头行数
    tail_lines: int               # 下沉后保留的尾行数
    digest_max_chars: int         # 摘要正文长度上限
    keep_recent_results: int      # 摘要时保留最近几条工具结果原文
    max_rounds: int               # 轮数兜底：距上次摘要超过这么多轮就压

    @property
    def usage_ratio(self) -> float:
        """摘要触发点占窗口的比例（用于解释"为什么是 17K"）。"""
        return self.summary_trigger_tokens / max(1, self.window_tokens)


# ---------------- 档位定义 ----------------
# api-large：当前实际在用的形态（外部 API，窗口大、按 token 计费）
API_LARGE = ContextProfile(
    name="api-large",
    window_tokens=131_072,
    summary_trigger_tokens=17_000,   # 先沿用已验证的阈值，切大窗口后再放宽
    tool_budget_tokens=2_000,
    sink_chars=2_500,
    sink_lines=60,
    head_lines=12,
    tail_lines=4,
    digest_max_chars=320,
    keep_recent_results=3,
    max_rounds=4,
)

# local-32k：将来本地 vLLM（Qwen3-14B，32K）用。数值更激进：更早压、压得更狠
LOCAL_32K = ContextProfile(
    name="local-32k",
    window_tokens=32_768,
    summary_trigger_tokens=15_000,   # 46%，留够摘要调用自身的空间
    tool_budget_tokens=1_500,
    sink_chars=2_000,
    sink_lines=50,
    head_lines=10,
    tail_lines=3,
    digest_max_chars=280,
    keep_recent_results=3,
    max_rounds=3,
)

PROFILES: dict[str, ContextProfile] = {
    p.name: p for p in (API_LARGE, LOCAL_32K)
}

DEFAULT_PROFILE = "api-large"


def get_profile(name: str | None = None) -> ContextProfile:
    """取档位。优先级：显式参数 > 环境变量 CONTEXT_PROFILE > 默认。

    环境变量写错时**回退默认并打印警告**，不抛异常 —— 配置问题不该让对话起不来。
    """
    key = (name or os.getenv("CONTEXT_PROFILE") or DEFAULT_PROFILE).strip()
    profile = PROFILES.get(key)
    if profile is None:
        print(f"[WARN] 未知 CONTEXT_PROFILE={key!r}，回退 {DEFAULT_PROFILE}；"
              f"可选：{sorted(PROFILES)}")
        return PROFILES[DEFAULT_PROFILE]
    return profile
