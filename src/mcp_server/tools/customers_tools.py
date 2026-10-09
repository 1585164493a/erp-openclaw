from fastmcp import FastMCP, Context
from mcp_server.http_base import get_http_client
from typing import Optional

GROUP_NAME = "customer"


def register_customer_tools(mcp: FastMCP):
    """注册客户（主数据）分组的所有工具"""

    @mcp.tool(name=f"{GROUP_NAME}_search")
    async def search_customers(
        name: Optional[str] = None,
        customer_type: Optional[int] = None,
        discount_level: Optional[int] = None,
        limit: int = 50,
        ctx: Context = None,
    ) -> list:
        """
        查询客户档案（主数据管理）。

        Args:
            name: 客户名称或客户编码（模糊匹配），可选
            customer_type: 客户类型，可选
            discount_level: 折扣等级，可选
            limit: 最多返回条数，默认 50
        """
        http_client = await get_http_client()

        params: dict = {"limit": limit}
        if name:
            params["name"] = name
        if customer_type is not None:
            params["customer_type"] = customer_type
        if discount_level is not None:
            params["discount_level"] = discount_level

        try:
            response = await http_client.get("/customers/search", params=params)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]
            return result.get("data", {}).get("records", [])
        except Exception as e:
            return [f"没有查询到任何信息，而且报错: {e}"]

    @mcp.tool(name=f"{GROUP_NAME}_create")
    async def create_customer(
        name: str,
        customer_code: Optional[str] = None,
        contact_person: Optional[str] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        address: Optional[str] = None,
        customer_type: Optional[int] = None,
        discount_level: Optional[int] = None,
        ctx: Context = None,
    ) -> dict:
        """
        新增客户主数据（POST /customers/create）。

        用于「客户开户」：新签 KA 商超、区域经销商或电商平台。
        注意：这是写操作，执行前会触发人工审批（HITL），审批通过后才真正落库。

        Args:
            name: 客户名称，必填
            customer_code: 客户编码（全局唯一）。不传则自动生成 CUS+5 位序号（如 CUS00041）
            contact_person: 联系人
            phone: 联系电话
            email: 邮箱
            address: 地址
            customer_type: 客户类型，1=KA商超 / 2=经销商 / 3=电商
            discount_level: 折扣等级，1=一级客户（折扣力度最大）/ 2=二级 / 3=三级
        """
        http_client = await get_http_client()

        request_data: dict = {"name": name}
        if customer_code is not None:
            request_data["customerCode"] = customer_code
        if contact_person is not None:
            request_data["contactPerson"] = contact_person
        if phone is not None:
            request_data["phone"] = phone
        if email is not None:
            request_data["email"] = email
        if address is not None:
            request_data["address"] = address
        if customer_type is not None:
            request_data["customerType"] = customer_type
        if discount_level is not None:
            request_data["discountLevel"] = discount_level

        try:
            response = await http_client.post("/customers/create", json=request_data)
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return {
                    "error": f"API error: code={result.get('code')}, message={result.get('message')}"
                }

            return result.get("data", {})
        except Exception as e:
            return {"error": str(e)}
