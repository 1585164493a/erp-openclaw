"""离线校验子 Agent 配置（不需要 VM / OpenSandbox / LLM）。

做三件事：
1. 在**进程内**触发 MCP Server 的工具注册，拿到真实的工具名集合（不连网络）；
2. 用真实工具名跑 `load_subagent_configs` + `resolve_subagent_tools`
   —— 这一步等价于线上启动流程的 Phase 5 / Phase 7；
3. 逐个跑启动期校验：middleware_profile / skill scope / interrupt_on / 写工具登记表。

为什么需要它：Phase 5.5 的校验要等服务真正起来才跑到，而完整启动依赖 VM 上的
MongoDB 与 OpenSandbox。本脚本把「配置是否自洽」这部分单独拎出来，
没有外部依赖也能验证 —— **改完 YAML 先跑这个**，比等启动失败快得多。

用法（在 src 目录下）：
    python test/validate_configs.py
退出码 0 = 全部通过；非 0 = 有问题，错误信息已打印。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.middleware_config import MIDDLEWARE_PROFILES, validate_middleware_profile
from agent.subagents.loader import (
    WRITE_TOOLS,
    build_skill_scope_map,
    cross_check_write_registry,
    load_subagent_configs,
    resolve_subagent_tools,
    validate_interrupt_on,
    validate_skill_scope,
)


class _ToolStub:
    """占位工具：resolve_subagent_tools 只依赖 .name，故无需真实可调用对象。"""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"<ToolStub {self.name}>"


def _real_mcp_tool_names() -> list:
    """在进程内注册 MCP 工具并取回真实工具名（不发起网络连接）。"""
    import mcp_server.server_main as sm

    result = sm.mcp.list_tools()
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    if isinstance(result, dict):
        return sorted(result.keys())
    return sorted(getattr(t, "name", str(t)) for t in result)


# 由工厂函数产出、无法直接 import 的本地工具名（改这些工厂时需同步维护）
_LOCAL_FACTORY_TOOLS = ("generate_visualization", "assign_skill", "download_sandbox_file")


def _local_tool_names() -> list:
    names = list(_LOCAL_FACTORY_TOOLS)
    try:
        from agent.tools.web_search import web_search
        from agent.tools.hitl_tools import request_order_info

        names += [web_search.name, request_order_info.name]
    except Exception as e:  # pragma: no cover — 依赖缺失时降级，不阻断校验
        print(f"[WARN] 本地工具名加载不完整（{type(e).__name__}: {e}）")
    return names


def main() -> int:
    print("=" * 78)
    print("1) 工具命名空间")
    print("=" * 78)
    mcp_names = _real_mcp_tool_names()
    local_names = _local_tool_names()
    all_names = sorted(set(mcp_names) | set(local_names))
    print(f"  MCP 工具 {len(mcp_names)} 个 + 本地工具 {len(local_names)} 个 = {len(all_names)} 个")

    # 反向核对：名字像写工具却没登记进 WRITE_TOOLS 的，这里就会炸
    cross_check_write_registry(all_names)
    print(f"  写工具登记表核对通过（已登记 {len(WRITE_TOOLS)} 个写工具）")

    print()
    print("=" * 78)
    print("2) 加载 YAML 配置")
    print("=" * 78)
    configs = load_subagent_configs()

    print()
    print("=" * 78)
    print("3) 启动期校验（等价 Phase 5.5）")
    print("=" * 78)
    print(f"  已注册中间件档位: {sorted(MIDDLEWARE_PROFILES)}")
    for cfg in configs:
        validate_middleware_profile(cfg.get("middleware_profile"), cfg["name"])
        validate_skill_scope(cfg)
        validate_interrupt_on(cfg)
    print(f"  {len(configs)} 个配置全部通过")

    print()
    print("=" * 78)
    print("4) 工具解析（等价 Phase 7）")
    print("=" * 78)
    pool = [_ToolStub(n) for n in all_names]
    subagents = resolve_subagent_tools(configs, pool)

    print()
    print("=" * 78)
    print("5) 解析结果")
    print("=" * 78)
    for sa in subagents:
        names = sorted(t.name for t in sa["tools"])
        writes = [n for n in names if n in WRITE_TOOLS]
        io = sorted((sa.get("interrupt_on") or {}).keys())
        kind = "写 Agent" if writes else "只读域"
        print(f"  {sa['name']:<24} [{kind}]  工具 {len(names):>2} 个")
        print(f"      技能={sa.get('skills')}")
        if writes:
            print(f"      写工具={writes}")
            print(f"      审批配置={io}  {'一致' if set(writes) == set(io) else '!!! 不一致'}")
    print()
    print(f"  scope 映射: {build_skill_scope_map(configs)}")
    print()
    print("全部校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
