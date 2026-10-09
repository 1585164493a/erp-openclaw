from fastmcp import FastMCP, Context
from mcp_server.http_base import get_http_client

from typing import Optional

# 分组名称
GROUP_NAME = "supplier"
def register_supplier_tools(mcp: FastMCP):
    """注册供应商分组的所有工具"""

    @mcp.tool(name=f"{GROUP_NAME}_query")
    async def query_suppliers(name: str, ctx: Context) -> list:
        """
        按名称模糊搜索供应商。

        Args:
            name: 供应商名称（模糊查询），必填
        """
        http_client = await get_http_client()

        try:
            response = await http_client.get("/suppliers/search", params={"name": name})
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]
            return result.get("data", [])
        except Exception as e:
            return [f'没有查询到任何信息，而且报错: {e}']

    @mcp.tool(name=f"{GROUP_NAME}_create")
    async def create_supplier(
        name: str,
        supplier_code: Optional[str] = None,
        contact_person: Optional[str] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        address: Optional[str] = None,
        credit_rating: Optional[str] = None,
        status: Optional[int] = None,
        ctx: Context = None,
    ) -> dict:
        """
        新增供应商主数据（POST /suppliers/create）。

        用于「供应商准入」：引入新的原料 / 包材供应商或代工厂。
        注意：这是写操作，执行前会触发人工审批（HITL），审批通过后才真正落库。

        Args:
            name: 供应商名称，必填
            supplier_code: 供应商编码（全局唯一）。不传则自动生成 SUP+3 位序号（如 SUP034）
            contact_person: 联系人
            phone: 联系电话
            email: 邮箱
            address: 地址
            credit_rating: 信用评级，只能填 A（优秀）/ B（良好）/ C（一般）/ D（较差）
            status: 合作状态，1=合作中（默认），0=已停用
        """
        http_client = await get_http_client()

        request_data: dict = {"name": name}
        if supplier_code is not None:
            request_data["supplierCode"] = supplier_code
        if contact_person is not None:
            request_data["contactPerson"] = contact_person
        if phone is not None:
            request_data["phone"] = phone
        if email is not None:
            request_data["email"] = email
        if address is not None:
            request_data["address"] = address
        if credit_rating is not None:
            request_data["creditRating"] = credit_rating
        if status is not None:
            request_data["status"] = status

        try:
            response = await http_client.post("/suppliers/create", json=request_data)
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return {
                    "error": f"API error: code={result.get('code')}, message={result.get('message')}"
                }

            return result.get("data", {})
        except Exception as e:
            return {"error": str(e)}