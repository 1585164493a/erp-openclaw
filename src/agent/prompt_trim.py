"""压缩固定开销：把框架默认的冗长工具描述换成精简版。

实测（2026-11-12，从真实 prompt 数出来）：首次调用 18.6K token 里
- system 9,995（AGENTS.md + 技能目录 + 内置指引）
- **工具定义 7,970**，其中 `task` 3,367、`write_todos` 1,036

这两个都是 deepagents / langchain 的默认描述，对我们业务没用那么多字：
- `task` 的默认描述 6,571 字符，讲了一堆"什么时候该用/不该用"；
- `write_todos` 的默认描述 1,000+ token，而我们根本没启用 todo 流程。

做法：**在构造前包一层 __init__**，注入精简描述。
不改框架源码、可随时关（不调用 install_prompt_trims 就恢复默认）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 精简版 task 工具描述：必须保留 {available_agents} 占位符（框架会替换成子 Agent 列表）
SHORT_TASK_DESCRIPTION = (
    "把任务委派给一个子 Agent，它独享上下文、只返回最终结果。\n"
    "适合：需要多步查询/分析、且只关心结论的任务。\n"
    "不适合：一次简单查询（自己调工具更快）。\n\n"
    "可选子 Agent：\n{available_agents}"
)

# 精简版 task 的系统提示（原本 2,140 字符）
SHORT_TASK_SYSTEM_PROMPT = (
    "用 `task` 工具启动子 Agent。给它完整的角色、输入和期望输出；"
    "它看不到你的对话历史，所以关键信息要写全。子 Agent 返回一条结构化结果。"
)

# 精简版 write_todos 描述（原本 1,000+ token）
SHORT_TODO_DESCRIPTION = (
    "维护当前任务的待办清单。每次传入**完整列表**（不要只传增量）。"
    "简单任务可以不用。"
)

_installed = False


def install_prompt_trims() -> None:
    """注入精简描述（幂等）。"""
    global _installed
    if _installed:
        return

    try:
        import deepagents.middleware.subagents as sa

        orig_sub_init = sa.SubAgentMiddleware.__init__

        def _sub_init(self, *args, **kwargs):
            kwargs.setdefault("system_prompt", SHORT_TASK_SYSTEM_PROMPT)
            kwargs.setdefault("task_description", SHORT_TASK_DESCRIPTION)
            return orig_sub_init(self, *args, **kwargs)

        sa.SubAgentMiddleware.__init__ = _sub_init
    except Exception as e:  # noqa: BLE001
        logger.warning("task 描述精简失败（不影响主流程）: %s", e)

    try:
        from langchain.agents.middleware import TodoListMiddleware

        orig_todo_init = TodoListMiddleware.__init__

        def _todo_init(self, *args, **kwargs):
            kwargs.setdefault("tool_description", SHORT_TODO_DESCRIPTION)
            return orig_todo_init(self, *args, **kwargs)

        TodoListMiddleware.__init__ = _todo_init
    except Exception as e:  # noqa: BLE001
        logger.warning("write_todos 描述精简失败（不影响主流程）: %s", e)

    _installed = True
    logger.info("已注入精简工具描述（task / write_todos）")
