"""
MCP 工具客户端。

启动时连接所有 MCP Server 取回全部工具。
此处不再做「哪些工具给哪个子 Agent」的分组 —— 那是子 Agent 自己的事：
每个子 Agent 在自己的 YAML 里声明 tools（精确名或 `前缀*`），
由 subagents/loader.py 运行期解析并校验。

使用方式:
    from agent.tools.mcp_client import load_mcp_tools

    toolset = await load_mcp_tools()
    toolset.all_tools     # 全量工具 → 进工具池
    toolset.chart_tools   # 仅可视化工具 → 供 26→1 合并
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp_server.server_config import ERP_MCP_URL

from agent.env_utils import DASHSCOPE_API_KEY

# 图表 MCP（可视化增强）—— 阿里云百炼托管的 AntV 图表服务。
#
# 地址走环境变量、不硬编码：早先用的是魔搭社区的免费试用地址，
# 那种地址会过期（实测返回 410 {"error":{"message":"Url is expired"}}），
# 硬编码就意味着每次过期都要改代码。现在换源只改 .env 一行。
ANALYSIS_MCP_URL = os.getenv(
    "ANALYSIS_MCP_URL",
    "https://dashscope.aliyuncs.com/api/v1/mcps/antv-visualization-chart/sse",
)

# MCP Server 连接配置
MCP_SERVER_CONFIG = {
    "erp-api": {
        "url": ERP_MCP_URL,
        "transport": "streamable_http",
    },
    "analysis": {
        "url": ANALYSIS_MCP_URL,
        # 实测：百炼这个端点【只支持 SSE】—— 往 /mcp 发 streamable_http 的
        # initialize 会返回 405 "current mcp not support streamableHttp"。
        "transport": "sse",
        # 百炼托管的 MCP 需要 Bearer 鉴权，用的是百炼通用 API Key。
        # 不配会被拒：401 {"code":"InvalidApiKey","message":"No API-key provided."}
        "headers": {"Authorization": f"Bearer {DASHSCOPE_API_KEY}"},
    },
}

# 唯一保留的前缀常量：从 analysis MCP 里挑出可视化工具，
# 供 create_generate_chart_tool() 做 26→1 合并用。
CHART_TOOL_PREFIXES = ("generate_",)


@dataclass
class McpToolset:
    """MCP 工具加载结果。

    刻意不按子 Agent 分组 —— 分组由 YAML 声明 + loader 解析完成，
    这样新增领域子 Agent 不需要改本模块。
    """

    all_tools: List = field(default_factory=list)
    chart_tools: List = field(default_factory=list)


async def load_mcp_tools(server_config: Optional[dict] = None) -> McpToolset:
    """连接所有 MCP Server，取回全量工具。

    Returns:
        McpToolset(all_tools, chart_tools)
    """
    if server_config is None:
        server_config = MCP_SERVER_CONFIG

    print("[INFO] 正在连接 MCP Server...")
    mcp_client = MultiServerMCPClient(server_config)

    # ERP 业务工具（必需）
    erp_tools = await mcp_client.get_tools(server_name="erp-api")
    print(f"[INFO] 已从 ERP MCP Server 加载 {len(erp_tools)} 个工具")

    # 可视化工具（可选，挂了只降级不致命）
    try:
        analysis_tools = await mcp_client.get_tools(server_name="analysis")
        print(f"[INFO] 已从图表 MCP 加载 {len(analysis_tools)} 个工具")
    except Exception as e:
        analysis_tools = []
        print(f"[WARN] 图表 MCP 不可用（{type(e).__name__}），可视化能力将关闭")

    all_tools = list(erp_tools) + list(analysis_tools)
    chart_tools = [
        t for t in analysis_tools
        if getattr(t, "name", "").startswith(CHART_TOOL_PREFIXES)
    ]

    print(
        f"[INFO] 工具加载完成: 全量 {len(all_tools)} 个"
        f"（其中可视化 {len(chart_tools)} 个）"
    )
    return McpToolset(all_tools=all_tools, chart_tools=chart_tools)