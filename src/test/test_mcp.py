import asyncio

from fastmcp import Client

from mcp_server.server_main import mcp


async def mcp_client():
    """创建内存模式客户端：直接验证 MCP 工具注册与调用（工具经 ERP API 取数）"""
    async with Client(mcp) as client:
        # 示例 1：按名称查供应商（对应 GET /api/suppliers/search?name=巴孚）
        result = await client.call_tool("supplier_query", {
            "name": "巴孚"
        })
        print("supplier_query(巴孚) =>", result)

        # 示例 2：按分类分页查商品
        result2 = await client.call_tool("product_query", {
            "current": 1,
            "size": 3,
            "category": "洗发护发"
        })
        print("product_query(洗发护发) =>", result2)


async def agent_client():
    """HTTP 模式：模拟 Agent 通过 MultiServerMCPClient 拉取工具（需 MCP Server 运行在 8099）"""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from mcp_server.server_config import ERP_MCP_URL

    mcp_server_config = {
        "url": ERP_MCP_URL,
        "transport": "streamable_http",
    }
    client = MultiServerMCPClient({"erp": mcp_server_config})
    all_tools = await client.get_tools(server_name="erp")
    print(f"共加载 {len(all_tools)} 个工具")
    product_tools = [t for t in all_tools if t.name.startswith("product_")]
    print("product_* 工具:", [t.name for t in product_tools])


if __name__ == '__main__':
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "mcp"
    if mode == "agent":
        asyncio.run(agent_client())
    else:
        asyncio.run(mcp_client())
