from fastmcp import FastMCP, Context
from mcp_server.http_base import get_http_client
from typing import Optional

GROUP_NAME = "inventory"


def register_inventory_tools(mcp: FastMCP):
    """注册库存管理分组的所有工具"""

    @mcp.tool(name=f"{GROUP_NAME}_warning")
    async def list_inventory_warnings(ctx: Context) -> list:
        """
        查询库存预警列表。
        返回所有库存不足（当前库存低于安全库存）的物料及对应的商品详情。

        无需传参。
        """
        http_client = await get_http_client()

        try:
            response = await http_client.get("/inventory/warning")
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]

            return result.get("data", [])
        except Exception as e:
            return [f'没有查询到任何信息，而且报错: {e}']

    @mcp.tool(name=f"{GROUP_NAME}_query")
    async def query_inventory(
        product_id: Optional[int] = None,
        name: Optional[str] = None,
        warehouse_location: Optional[str] = None,
        only_warning: bool = False,
        limit: int = 50,
        ctx: Context = None,
    ) -> list:
        """
        查询库存明细（比 inventory_warning 更通用：不限于预警项）。

        返回结果把「低于安全库存」的物料排在最前面，便于直接取前 N 条下结论。

        Args:
            product_id: 商品 ID，可选
            name: 商品名称（模糊查询），可选
            warehouse_location: 仓库位置，可选
            only_warning: 设为 true 时只返回低于安全库存的物料
            limit: 最多返回条数，默认 50
        """
        http_client = await get_http_client()

        params: dict = {"limit": limit}
        if product_id is not None:
            params["product_id"] = product_id
        if name:
            params["name"] = name
        if warehouse_location:
            params["warehouse_location"] = warehouse_location
        if only_warning:
            params["only_warning"] = "true"

        try:
            response = await http_client.get("/inventory/query", params=params)
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]

            return result.get("data", {}).get("records", [])
        except Exception as e:
            return [f'没有查询到任何信息，而且报错: {e}']

    @mcp.tool(name=f"{GROUP_NAME}_coverage")
    async def inventory_coverage(
        product_id: Optional[int] = None,
        max_days: Optional[float] = None,
        limit: int = 50,
        ctx: Context = None,
    ) -> list:
        """
        库存覆盖分析：判断「还能卖几天、要不要补货、补多少」。

        对每个商品返回：当前库存、安全库存、近 30 天日均销量、可售天数、
        建议补货量，以及覆盖状态（紧急 <7天 / 偏低 <14天 / 充足 / 无销量数据）。
        结果按可售天数升序，最紧俏的排在最前。

        这是补货与生产预测的【唯一口径来源】—— 不要自己拉日销明细再推算，
        否则口径会漂移（这次按 14 天、下次按 30 天）。

        Args:
            product_id: 指定商品 ID，可选；不传则返回全部商品
            max_days: 只返回可售天数小于该值的商品，可选（如 14 表示只看不足两周的）
            limit: 最多返回条数，默认 50
        """
        http_client = await get_http_client()

        params: dict = {"limit": limit}
        if product_id is not None:
            params["product_id"] = product_id
        if max_days is not None:
            params["max_days"] = max_days

        try:
            response = await http_client.get("/inventory/coverage", params=params)
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]

            return result.get("data", {}).get("records", [])
        except Exception as e:
            return [f'没有查询到任何信息，而且报错: {e}']
