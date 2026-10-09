from fastmcp import FastMCP, Context
from mcp_server.http_base import get_http_client
from typing import Optional

GROUP_NAME = "meta"


def register_meta_tools(mcp: FastMCP):
    """注册元数据查询分组的所有工具（结构 + 业务字典）"""

    @mcp.tool(name=f"{GROUP_NAME}_schema")
    async def meta_schema(table: Optional[str] = None, ctx: Context = None) -> dict:
        """
        查询数据库结构元数据：系统里有哪些表、某张表有哪些字段。

        不传 table → 返回全部表清单（含每张表的用途说明与字段数）
        传 table   → 返回该表的字段清单（字段名、类型、是否可空、键、注释）

        结构信息实时读取数据库，永远是最新的。当你不确定"数据在哪张表、
        某个字段叫什么名字"时，**先查这里，不要凭猜测写字段名**。

        Args:
            table: 表名，可选；不传则返回表清单
        """
        http_client = await get_http_client()

        params: dict = {}
        if table:
            params["table"] = table

        try:
            response = await http_client.get("/meta/schema", params=params)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 200:
                return {"error": f"API error: code={result.get('code')}",
                        "message": result.get("message")}
            return result.get("data", {})
        except Exception as e:
            return {"error": f"没有查询到任何信息，而且报错: {e}"}

    @mcp.tool(name=f"{GROUP_NAME}_dict")
    async def meta_dict(
        table: str,
        column: Optional[str] = None,
        ctx: Context = None,
    ) -> dict:
        """
        查询字段取值的业务含义（数据字典）。

        系统里有些字段存的是数字/字母编码，含义只有业务侧知道，例如：
          · customer.customer_type  1=KA商超 / 2=经销商 / 3=电商
          · purchase_order.status   1=待审核 … 5=已取消
          · supplier.credit_rating  A/B/C/D 的评级规则

        **遇到陌生字段或数字取值时，必须先查本工具，绝不自行推测含义。**

        不传 column → 返回该表已维护的全部枚举字段及其取值
        传 column   → 返回该字段的取值含义 + 每个取值在数据中的条数
                     （字典里没有的自描述字段会实时返回实际取值分布）

        Args:
            table: 表名（必填）
            column: 字段名，可选
        """
        http_client = await get_http_client()

        params: dict = {"table": table}
        if column:
            params["column"] = column

        try:
            response = await http_client.get("/meta/dict", params=params)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 200:
                return {"error": f"API error: code={result.get('code')}",
                        "message": result.get("message")}
            return result.get("data", {})
        except Exception as e:
            return {"error": f"没有查询到任何信息，而且报错: {e}"}
