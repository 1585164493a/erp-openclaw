"""澄清口径用的 HITL 工具。

背景（2026-11-12 实测）：题目/用户没给时间窗口或统计口径时，agent 会**自己拍一个**
（"最近"取近 30 天、"供货最多"按主供应商算），结果和用户预期不一致却无从纠正。

这里给一个通用澄清工具：缺关键口径时**暂停，把选项抛给用户**，而不是替用户猜。

与 `request_order_info` 的区别：
- request_order_info：订单**字段**缺失（必填项没给）
- request_clarification：**口径/范围**不明确（时间窗口、统计维度、排重规则）
"""
from __future__ import annotations

import json

from langchain_core.tools import tool
from langgraph.types import interrupt


@tool
def request_clarification(question: str, options: str = "", assumed: str = "") -> str:
    """当关键口径不明确时，暂停执行并请用户确认（不要自己猜）。

    什么时候用：
    - 时间窗口不明（"最近""上个月"没有明确自然月/滚动天数）
    - 统计口径有多种合理解读（按订单表还是明细表、按主供应商还是关系表）
    - 排重/计数规则会影响结论（去重 vs 不去重）

    什么时候**不要**用（直接给答案更高效）：
    - 只有一种合理解读
    - 用户已经说清楚了范围/口径
    - 只是措辞含糊但意图明确（如"卖得最好的商品"——给销量和金额两种排序即可）

    Args:
        question: 要问用户的那句话，说清缺什么。
        options: 可选项，用 " | " 分隔，例如 "自然月 10 月 | 近 30 天 | 11-01 至今天"。
        assumed: 你打算默认采用的口径（用户不回答就用它），例如 "近 30 天"。

    Returns:
        用户的补充（JSON 字符串）。
    """
    response = interrupt({
        "type": "clarification_request",
        "question": question,
        "options": options,
        "assumed": assumed,
    })
    return json.dumps(response, ensure_ascii=False)
