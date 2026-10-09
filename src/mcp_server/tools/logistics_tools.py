from fastmcp import FastMCP, Context
from mcp_server.http_base import get_http_client
from typing import Optional

GROUP_NAME = "logistics"


def register_logistics_tools(mcp: FastMCP):
    """注册物流分组的所有工具"""

    @mcp.tool(name=f"{GROUP_NAME}_search")
    async def search_logistics(
        order_id: Optional[int] = None,
        tracking_number: Optional[str] = None,
        status: Optional[int] = None,
        overdue_only: bool = False,
        limit: int = 50,
        ctx: Context = None,
    ) -> list:
        """
        查询物流信息（发货、在途、签收状态）。

        Args:
            order_id: 采购订单 ID，可选
            tracking_number: 运单号（模糊匹配），可选
            status: 物流状态码，可选
            overdue_only: 设为 true 时只返回「预计到达时间已过、但尚未实际签收」
                          的超期运单 —— 物流监控重点盯的一类
            limit: 最多返回条数，默认 50
        """
        http_client = await get_http_client()

        params: dict = {"limit": limit}
        if order_id is not None:
            params["order_id"] = order_id
        if tracking_number:
            params["tracking_number"] = tracking_number
        if status is not None:
            params["status"] = status
        if overdue_only:
            params["overdue_only"] = "true"

        try:
            response = await http_client.get("/logistics/search", params=params)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]
            return result.get("data", {}).get("records", [])
        except Exception as e:
            return [f"没有查询到任何信息，而且报错: {e}"]

    @mcp.tool(name=f"{GROUP_NAME}_performance")
    async def logistics_performance(
        days: int = 90,
        limit: int = 20,
        ctx: Context = None,
    ) -> list:
        """
        物流时效统计：按承运商看准时率与在途时长（物流监控的核心指标）。

        返回每个承运商的：发货单量、已签收单量、准时单量、准时率(onTimeRate)、
        平均在途天数(avgTransitDays)、超期未签收单量(overdueCount)。

        口径（固定，不要另行解释）：
          · 准时 = 实际到达时间 <= 预计到达时间，仅对**已签收**单量计算准时率
          · 平均在途天数 = 实际到达 − 发货，仅对已签收单
          · 超期 = 尚未签收 且 预计到达时间已过

        Args:
            days: 统计窗口天数，默认 90
            limit: 最多返回几家承运商，默认 20
        """
        http_client = await get_http_client()

        params = {"days": days, "limit": limit}

        try:
            response = await http_client.get("/logistics/performance", params=params)
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]

            return result.get("data", {}).get("records", [])
        except Exception as e:
            return [f"没有查询到任何信息，而且报错: {e}"]
