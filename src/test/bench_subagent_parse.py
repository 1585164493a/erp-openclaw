"""量一量"每次请求解析工具/skill"到底花多少时间。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agent.subagents.loader import load_subagent_configs  # noqa: E402

# 构造一个假的工具池（模拟 MCP + 本地工具的规模），只为测量匹配开销
class FakeTool:
    def __init__(self, name):
        self.name = name


def main() -> int:
    names = [f"tool_{i}" for i in range(120)] + [
        "sql_list_tables", "sql_describe_table", "sql_sample_rows", "sql_query",
        "py_exec", "py_reset", "supplier_query", "inventory_query",
    ]
    tools = [FakeTool(n) for n in names]

    # 1) 只读 YAML 配置
    t0 = time.perf_counter()
    configs = load_subagent_configs()
    t1 = time.perf_counter()
    print(f"load_subagent_configs: {(t1 - t0) * 1000:.1f} ms（{len(configs)} 个子 Agent）")

    # 2) 解析工具名（用真实 loader，但跳过中间件）
    from agent.subagents.loader import resolve_subagent_tools

    t0 = time.perf_counter()
    try:
        resolve_subagent_tools(configs, tools)
    except ValueError as e:
        print("解析失败（预期，因为假工具池缺某些名字）:", str(e).splitlines()[0])
    t1 = time.perf_counter()
    print(f"resolve_subagent_tools: {(t1 - t0) * 1000:.1f} ms")

    # 3) 重复 20 次，看在一次请求里的占比
    t0 = time.perf_counter()
    for _ in range(20):
        load_subagent_configs()
    t1 = time.perf_counter()
    print(f"重复 20 次 YAML 解析: {(t1 - t0) * 1000:.1f} ms → 单次 {(t1 - t0) * 50:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
