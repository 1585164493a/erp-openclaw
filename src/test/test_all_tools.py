"""
MCP 工具全量测试。

测试模式：
  - mcp_client：通过 fastmcp.Client 内存模式直接测试 MCP 服务端。
    ★ 前提：ERP API 已在 127.0.0.1:8080 运行（uvicorn erp_api.main:app --port 8080），
      MCP 工具经 ERP_API_BASE_URL（默认 http://localhost:8080/api）访问数据。
  - agent_client：通过 MultiServerMCPClient 模拟 Agent，经 HTTP 获取工具并调用。
    ★ 前提：MCP Server 已启动（python -m mcp_server.server_main，监听 127.0.0.1:8099/mcp）。

运行方式：
  python -m test.test_all_tools            # 默认 mcp 模式
  python -m test.test_all_tools agent      # HTTP 模式（需 MCP 进程）
  python -m test.test_all_tools all        # 两种都跑
"""

import asyncio
import random
import sys
from datetime import datetime

from fastmcp import Client
from langchain_mcp_adapters.client import MultiServerMCPClient

from mcp_server.server_main import mcp
from mcp_server.server_config import ERP_MCP_URL

# ============================================================
# 测试样本（取自已导入的 motorparts_db，保证结果非空可看）
# ============================================================

SUPPLIER_NAMES = ["巴孚", "琦华", "芬美", "涛氏", "诺力安", "赞羽", "万桦", "科来恩"]
PRODUCT_NAMES = ["澜溪", "朗风", "雅致", "悦芙", "洁力", "柔洁", "梦享", "茶悦"]
CATEGORIES = ["洗发护发", "沐浴清洁", "洗衣护理", "织物护理", "冰淇淋", "茶饮冲调"]


def _random_supplier_name() -> str:
    return random.choice(SUPPLIER_NAMES)


def _random_product_name() -> str:
    return random.choice(PRODUCT_NAMES)


def _random_category() -> str:
    return random.choice(CATEGORIES)


def _random_supplier_id() -> int:
    return random.randint(1, 33)


def _random_product_id() -> int:
    return random.randint(1, 67)


def _random_quantity() -> int:
    return random.randint(5, 300)


def _random_price() -> float:
    return round(random.uniform(10.0, 2000.0), 2)


def _random_status() -> int:
    return random.randint(1, 5)


def _now_iso() -> str:
    """yyyy-MM-ddTHH:mm:ss.SSS"""
    now = datetime.now()
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _random_order_detail(num_items: int = 2) -> list:
    items = []
    for i in range(num_items):
        quantity = _random_quantity()
        unit_price = _random_price()
        items.append({
            "productId": _random_product_id(),
            "quantity": quantity,
            "unitPrice": unit_price,
            "subtotal": round(quantity * unit_price, 2),
            "remark": f"测试明细-{i + 1}",
        })
    return items


# ============================================================
# 1. mcp_client 测试（内存模式，参数与 MCP 工具签名一致）
# ============================================================

async def test_mcp_supplier_query():
    """测试 supplier_query — 按名称模糊搜索供应商"""
    name = _random_supplier_name()
    print(f"\n{'=' * 60}")
    print(f"[mcp_client] 测试 supplier_query: name={name}")
    print(f"{'=' * 60}")
    async with Client(mcp) as client:
        result = await client.call_tool("supplier_query", {"name": name})
        print(f"结果: {result}")
        return result


async def test_mcp_product_query():
    """测试 product_query — 分页查询商品（按分类）"""
    category = _random_category()
    print(f"\n{'=' * 60}")
    print(f"[mcp_client] 测试 product_query: current=1, size=5, category={category}")
    print(f"{'=' * 60}")
    async with Client(mcp) as client:
        result = await client.call_tool("product_query", {
            "current": 1,
            "size": 5,
            "category": category,
        })
        print(f"结果: {result}")
        return result


async def test_mcp_product_search():
    """测试 product_search — 按名称搜索商品"""
    name = _random_product_name()
    print(f"\n{'=' * 60}")
    print(f"[mcp_client] 测试 product_search: name={name}")
    print(f"{'=' * 60}")
    async with Client(mcp) as client:
        result = await client.call_tool("product_search", {"name": name})
        print(f"结果: {result}")
        return result


async def test_mcp_product_by_supplier():
    """测试 product_by_supplier — 按供应商 ID 查其有采购记录的商品"""
    supplier_id = _random_supplier_id()
    print(f"\n{'=' * 60}")
    print(f"[mcp_client] 测试 product_by_supplier: supplier_id={supplier_id}")
    print(f"{'=' * 60}")
    async with Client(mcp) as client:
        result = await client.call_tool("product_by_supplier", {"supplier_id": supplier_id})
        print(f"结果: {result}")
        return result


async def test_mcp_order_create():
    """测试 order_create — 创建采购订单（写入本地 motorparts_db）"""
    order_detail = _random_order_detail(2)
    total_amount = sum(item["subtotal"] for item in order_detail)
    print(f"\n{'=' * 60}")
    print(f"[mcp_client] 测试 order_create")
    print(f"{'=' * 60}")
    async with Client(mcp) as client:
        result = await client.call_tool("order_create", {
            "order_detail": order_detail,
            "order_time": _now_iso(),
            "total_amount": round(total_amount, 2),
            "status": 1,
            "remark": "mcp_client 测试订单",
        })
        print(f"结果: {result}")
        return result


async def test_mcp_order_update():
    """测试 order_update — 更新订单（含明细整体替换场景）"""
    order_id = random.randint(1, 30)
    order_detail = _random_order_detail(1)
    print(f"\n{'=' * 60}")
    print(f"[mcp_client] 测试 order_update (含明细): order_id={order_id}")
    print(f"{'=' * 60}")
    async with Client(mcp) as client:
        result1 = await client.call_tool("order_update", {
            "order_id": order_id,
            "order_detail": order_detail,
            "status": 3,
            "remark": "mcp_client 测试-更新订单(含明细)",
        })
        print(f"结果(含明细): {result1}")

        print(f"\n[mcp_client] 测试 order_update (仅状态/备注): order_id={order_id}")
        result2 = await client.call_tool("order_update", {
            "order_id": order_id,
            "status": 2,
            "remark": "mcp_client 测试-仅更新状态和备注",
        })
        print(f"结果(无明细): {result2}")
        return result1


async def test_mcp_order_search_details():
    """测试 order_search_details — 搜索订单明细（名称 + 日期范围）"""
    product_name = _random_product_name()
    print(f"\n{'=' * 60}")
    print(f"[mcp_client] 测试 order_search_details: product_name={product_name}")
    print(f"{'=' * 60}")
    async with Client(mcp) as client:
        result = await client.call_tool("order_search_details", {"product_name": product_name})
        print(f"结果(按名称): {result}")

        result2 = await client.call_tool("order_search_details", {
            "start_date": "2025-01-01",
            "end_date": "2026-06-30",
        })
        print(f"结果(按日期): {result2}")
        return result


async def test_mcp_inventory_warning():
    """测试 inventory_warning — 库存预警列表（无参）"""
    print(f"\n{'=' * 60}")
    print(f"[mcp_client] 测试 inventory_warning (无参调用)")
    print(f"{'=' * 60}")
    async with Client(mcp) as client:
        result = await client.call_tool("inventory_warning", {})
        print(f"结果: {result}")
        return result


# ============================================================
# 2. agent_client 测试（HTTP 模式，模拟 Agent 调用）
# ============================================================

MCP_SERVER_CONFIG = {
    "erp": {
        "url": ERP_MCP_URL,
        "transport": "streamable_http",
    }
}


async def test_agent_order_create():
    """模拟 Agent 经 HTTP 获取 order_create 工具并调用。"""
    print(f"\n{'=' * 60}")
    print(f"[agent_client] 模拟 Agent 测试 order_create（URL: {ERP_MCP_URL}）")
    print(f"{'=' * 60}")
    client = MultiServerMCPClient(MCP_SERVER_CONFIG)
    try:
        all_tools = await client.get_tools(server_name="erp")
        print(f"[agent_client] 已加载 {len(all_tools)} 个 MCP 工具")
        order_tools = [t for t in all_tools if t.name == "order_create"]
        if not order_tools:
            print("[agent_client] 错误: 未找到 order_create 工具")
            return
        order_create_tool = order_tools[0]
        print(f"[agent_client] 工具: {order_create_tool.name} / {order_create_tool.description[:60]}...")
        order_detail = _random_order_detail(2)
        total_amount = sum(item["subtotal"] for item in order_detail)
        result = await order_create_tool.ainvoke({
            "order_detail": order_detail,
            "order_time": _now_iso(),
            "total_amount": round(total_amount, 2),
            "status": 1,
            "remark": "agent_client 模拟测试订单",
        })
        print(f"[agent_client] 执行结果: {result}")
        return result
    finally:
        pass


async def test_agent_inventory_warning():
    """模拟 Agent 经 HTTP 获取 inventory_warning 工具并调用（无参）。"""
    print(f"\n{'=' * 60}")
    print(f"[agent_client] 模拟 Agent 测试 inventory_warning")
    print(f"{'=' * 60}")
    client = MultiServerMCPClient(MCP_SERVER_CONFIG)
    try:
        all_tools = await client.get_tools(server_name="erp")
        inv_tools = [t for t in all_tools if t.name == "inventory_warning"]
        if not inv_tools:
            print("[agent_client] 错误: 未找到 inventory_warning 工具")
            return
        inv_tool = inv_tools[0]
        result = await inv_tool.ainvoke({})
        print(f"[agent_client] 执行结果: {result}")
        return result
    finally:
        pass


# ============================================================
# 批量运行入口
# ============================================================

async def run_all_mcp_client_tests():
    """依次运行全部 8 个 mcp_client 测试"""
    tests = [
        ("supplier_query", test_mcp_supplier_query),
        ("product_query", test_mcp_product_query),
        ("product_search", test_mcp_product_search),
        ("product_by_supplier", test_mcp_product_by_supplier),
        ("order_create", test_mcp_order_create),
        ("order_update", test_mcp_order_update),
        ("order_search_details", test_mcp_order_search_details),
        ("inventory_warning", test_mcp_inventory_warning),
    ]
    results = {}
    for name, test_fn in tests:
        try:
            results[name] = await test_fn()
        except Exception as e:
            results[name] = f"失败: {e}"
            print(f"[ERROR] {name} 测试失败: {e}")
    print(f"\n{'=' * 60}")
    print("[汇总] mcp_client 测试完成")
    print(f"{'=' * 60}")
    for name, result in results.items():
        status = "失败" if isinstance(result, str) and result.startswith("失败") else "完成"
        print(f"  {name}: {status}")


async def run_all_agent_client_tests():
    """依次运行 agent_client 测试：order_create + inventory_warning"""
    results = {}
    for name, test_fn in [
        ("order_create", test_agent_order_create),
        ("inventory_warning", test_agent_inventory_warning),
    ]:
        try:
            results[name] = await test_fn()
        except Exception as e:
            results[name] = f"失败: {e}"
            print(f"[ERROR] agent_client {name} 测试失败: {e}")
    print(f"\n{'=' * 60}")
    print("[汇总] agent_client 测试完成")
    print(f"{'=' * 60}")
    for name, result in results.items():
        status = "失败" if isinstance(result, str) and result.startswith("失败") else "完成"
        print(f"  {name}: {status}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "mcp"
    if mode == "mcp":
        print("运行全部 mcp_client 测试（内存模式，需 ERP API 在 127.0.0.1:8080）...")
        asyncio.run(run_all_mcp_client_tests())
    elif mode == "agent":
        print("运行 agent_client 测试（需 MCP Server 启动在 127.0.0.1:8099/mcp）...")
        asyncio.run(run_all_agent_client_tests())
    elif mode == "all":
        print("=== 第一阶段：mcp_client 测试 ===\n")
        asyncio.run(run_all_mcp_client_tests())
        print("\n=== 第二阶段：agent_client 测试 ===\n")
        asyncio.run(run_all_agent_client_tests())
    else:
        print(f"未知模式: {mode}")
        print("用法: python -m test.test_all_tools [mcp|agent|all]")