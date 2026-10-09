"""离线检查：Agent 会读到的文本里，引用的工具名是否真实存在。

为什么需要它：
  「part_* → product_*」改名之后，多处 prompt / skill 里还留着旧工具名，
  子 Agent 照着调就会报错；而这类错误**启动期校验网拦不住**
  （loader 只校验 YAML 的 tools 字段，管不到 prompt 正文和技能文档）。

  本脚本做一次机械化交叉核对：把 prompt / SKILL.md 里出现的疑似工具名
  与实际注册的工具集比对。它抓到过 `web_fetch`（该项目从未注册过这个工具）。

扫描范围刻意收窄到「Agent 会读到的文本」：
  - src/agent/subagents/configs/*.yaml   （子 Agent 的 prompt 与工具表）
  - src/agent/memory/*.md, *.py          （主 Agent 的 prompt 与准则）
  - src/skills/**/*.md                   （技能文档）
**不含**技能目录下的 .py 实现 —— 那些是代码，函数名会造成大量误报。

用法（在 src 目录下）:
    python test/check_tool_refs.py
退出码 0 = 未发现未知工具引用。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

SRC = Path(__file__).parent.parent
PROJECT = SRC.parent


def _registered_tools() -> set:
    """进程内注册 MCP 工具 + 本地工具 + deepagents 文件系统工具。"""
    import mcp_server.server_main as sm

    tools = sm.mcp.list_tools()
    if asyncio.iscoroutine(tools):
        tools = asyncio.run(tools)
    mcp = set(tools.keys()) if isinstance(tools, dict) else {
        getattr(t, "name", str(t)) for t in tools
    }
    local = {
        "web_search", "generate_visualization", "assign_skill",
        "download_sandbox_file", "request_order_info",
        # deepagents 内置
        "task", "read_file", "write_file", "edit_file", "ls", "glob", "grep",
        "execute", "compact_conversation",
    }
    return mcp | local


# 不是工具名的 snake_case 词：库表名、字段名、内部函数名、技能里的脚本参数
NOT_TOOLS = {
    # 库表 / 视图
    "supplier", "customer", "product", "purchase_order", "order_detail", "inventory",
    "sales_daily", "sales_target", "bonus_rule", "logistics", "meta_dict",
    "product_supplier", "product_price_log", "inventory_warning_view",
    "session_tasks", "session_display_messages", "sandbox_registry",
    # 业务字段
    "product_id", "product_code", "product_name", "supplier_id", "supplier_code",
    "customer_id", "customer_code", "customer_type", "order_id", "order_number",
    "order_detail", "order_time", "order_status", "unit_price", "total_amount",
    "purchase_price", "suggested_retail_price", "stock_warning_value", "credit_rating",
    "discount_level", "contact_person", "created_by", "expected_delivery_date",
    "actual_delivery_date", "start_date", "end_date", "lead_days", "valid_from",
    "valid_to", "quote_price", "is_primary", "safety_stock", "current_quantity",
    "on_hand", "in_transit", "ship_time", "estimated_arrival_time",
    "actual_arrival_time", "overdue_only", "tracking_number", "drop_ratio",
    "spike_ratio", "on_time_rate", "avg_delay_days", "create_time", "update_time",
    "deleted", "sort_no", "value_code",
    # Agent 配置字段
    "system_prompt", "middleware_profile", "interrupt_on", "allowed_decisions",
    "skill_scope", "chart_type", "chart_config", "preferred_output",
    "preferred_chart_type", "preferred_currency", "preferred_language",
    "recent_suppliers", "recent_queries", "user_id", "thread_id", "tool_name",
    "tool_call_id", "action_requests", "review_configs",
    # 内部函数 / 目录名
    "build_chart_params_md", "create_generate_chart_tool", "create_deep_agent",
    "validate_interrupt_on", "validate_skill_scope", "precompute_agent_context",
    "create_main_agent", "mock_site", "url_mapping", "chart_params",
    "fetch_content", "scrape_page", "skill_management", "fetch_webpage",
}


def _targets() -> list:
    out = []
    out += sorted((SRC / "agent" / "subagents" / "configs").glob("*.yaml"))
    mem = SRC / "agent" / "memory"
    out += sorted(mem.glob("*.md")) + sorted(mem.glob("*.py"))
    out += sorted((SRC / "skills").rglob("*.md"))
    return [p for p in out if p.is_file()]


NAME_RE = re.compile(r"`([a-z][a-z0-9]*_[a-z0-9_]+)`|([a-z][a-z0-9]*_[a-z0-9_]+)\(")


def main() -> int:
    known = _registered_tools()
    unknown: dict = {}

    for path in _targets():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(PROJECT).as_posix()
        for m in NAME_RE.finditer(text):
            name = m.group(1) or m.group(2)
            if name in known or name in NOT_TOOLS or name.endswith("_*"):
                continue
            line_no = text[: m.start()].count("\n") + 1
            unknown.setdefault(name, []).append("%s:%d" % (rel, line_no))

    print("已注册工具 %d 个；扫描 %d 个 Agent 可读文件" % (len(known), len(_targets())))
    print()
    if not unknown:
        print("未发现未知工具引用。")
        return 0

    print("以下名字像工具调用，但不在已注册工具集里 —— 请人工确认是否为笔误或旧名：")
    for name in sorted(unknown):
        print("  %-26s %s" % (name, ", ".join(unknown[name])))
    print()
    print("注意：本检查是启发式的，可能仍有误报（如把库表名写成工具调用）。")
    print("      确认是误报就把该名字加进脚本的 NOT_TOOLS。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
