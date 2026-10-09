"""MCP 工具的 HTTP 客户端管理。

历史：原实现把 httpx.AsyncClient 放进 FastMCP lifespan_context，
工具通过 ctx.request_context.lifespan_context 获取。该方式依赖 FastMCP
独立运行时注入 lifespan；在“内存模式测试 / 其它挂载方式”下拿不到客户端。

现改为模块级懒加载单例 AsyncClient：
- base_url 默认 http://localhost:8080/api（原 Java 后端地址），
- 环境变量 ERP_API_BASE_URL 可覆盖为本仓库 Python ERP API（src/erp_api）。
"""

from contextlib import asynccontextmanager

import httpx

from mcp_server.server_config import ERP_API_BASE_URL

_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    """懒加载全局 HTTP 客户端（进程内复用连接池）。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=ERP_API_BASE_URL,
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
    return _client


async def aclose_http_client() -> None:
    """关闭全局客户端（进程退出时调用）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@asynccontextmanager
async def mcp_lifespan(server):
    """兼容占位：工具已不依赖 lifespan_context，保留以便独立运行/测试。"""
    yield {}