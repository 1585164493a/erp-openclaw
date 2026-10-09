"""
子 Agent 中间件配置。

按「中间件档位（profile）」组织：
子 Agent 在自己的 YAML 里声明 `middleware_profile: analyst`，
由 build_subagent_middleware() 查本模块的注册表构建中间件列表。

这样新增子 Agent 只需选择一个已有档位，不需要改本模块；
档位名写错 → 启动期 fail-fast。
"""

import os
from typing import Callable, Dict, List, Optional

from deepagents.middleware.summarization import (
    create_summarization_tool_middleware,
)
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)

def _available_profiles() -> str:
    return ", ".join(sorted(MIDDLEWARE_PROFILES))


def validate_middleware_profile(profile: Optional[str], agent_name: str) -> None:
    """校验档位声明；不合法直接抛错。

    应在启动期（precompute）调用，让配置错误在服务起来时就暴露，
    而不是等到第一个对话请求。

    Raises:
        ValueError: 未声明档位，或档位名未注册
    """
    if not profile:
        raise ValueError(
            f"子 Agent '{agent_name}' 未声明 middleware_profile；"
            f"可选档位: {_available_profiles()}"
        )
    if profile not in MIDDLEWARE_PROFILES:
        raise ValueError(
            f"子 Agent '{agent_name}' 的 middleware_profile {profile!r} 未注册；"
            f"可选档位: {_available_profiles()}"
        )

# ============================================================
# 档位工厂
# 统一签名 (model, backend) -> list；用不到的参数接受并忽略。
# 这样注册表才能统一调用 —— 各档位依赖不同，但调用方不该关心。
# ============================================================

def create_analyst_middleware(model, backend) -> list:
    """分析型档位：长链路多步任务，需要摘要工具压缩上下文。

    - SummarizationToolMiddleware: 阶段完成后主动压缩上下文
    - ModelCallLimitMiddleware: 防止无限循环（最多 50 次模型调用）
    - ToolCallLimitMiddleware: 防止工具调用爆炸（最多 200 次）

    A/B 开关（默认关闭，行为与原来完全一致）：
        设 AB_DISABLE_TOOLS_SUMMARIZATION=1 时**不挂**摘要中间件，
        用于量化「摘要压缩到底省了多少 token」。A/B 开关见 `AB_DISABLE_TOOLS_SUMMARIZATION`。

    Args:
        model: 用于摘要生成的模型（建议用小模型如 deepseek-v4-flash）
        backend: 文件系统后端（用于摘要持久化）
    """
    limits = [
        ModelCallLimitMiddleware(run_limit=50),
        ToolCallLimitMiddleware(run_limit=200),
    ]
    if os.getenv("AB_DISABLE_TOOLS_SUMMARIZATION") == "1":
        return limits
    return [create_summarization_tool_middleware(model, backend)] + limits


def create_write_middleware(model=None, backend=None) -> list:
    """写操作档位：简单 API 调用，不需要摘要工具，只需防异常循环。

    接受 model/backend 仅为满足注册表的统一签名，本档位用不到。

    为什么写操作要用**更紧**的上限（20 次模型调用 / 50 次工具调用）：
    - 写工具挂了 interrupt_on，每次调用都要人来审批，循环次数多等于反复打扰人；
    - 写操作出错代价高，跑飞了不像查询那样可以重来 —— 尽早刹车。

    - ModelCallLimitMiddleware: 最多 20 次模型调用
    - ToolCallLimitMiddleware: 最多 50 次工具调用
    """
    return [
        ModelCallLimitMiddleware(run_limit=20),
        ToolCallLimitMiddleware(run_limit=50),
    ]


# ============================================================
# 注册表：档位名 → 工厂
# 子 Agent YAML 通过 middleware_profile 引用这里的 key。
# ============================================================
# 档位按「任务形态」划分，不按业务域：
#   analyst = 只读分析（长链路、多步、需压缩上下文）
#   write   = 写操作（短链路、需审批、上限更紧）
# 因此 procurement-order 与 master-data-maintain 共用 write 档位是预期内的。
MIDDLEWARE_PROFILES: Dict[str, Callable[..., List]] = {
    "analyst": create_analyst_middleware,
    "write": create_write_middleware,
}


def build_subagent_middleware(
    profile: str, model, backend, agent_name: str = "未知"
) -> List:
    """按档位名构建子 Agent 中间件列表。

    Args:
        profile: YAML 里声明的 middleware_profile
        model: 摘要模型
        backend: 文件系统后端

    Returns:
        中间件实例列表

    Raises:
        ValueError: 档位名未注册（启动期 fail-fast，不静默降级）
    """
    validate_middleware_profile(profile, agent_name)
    return MIDDLEWARE_PROFILES[profile](model, backend)   # 已校验，可直接索引