from fastmcp import FastMCP, Context
from mcp_server.http_base import get_http_client
from typing import Optional

# 分组名称
GROUP_NAME = "product"   # 分组名称，最终工具名称为 product_query / product_search
def register_products_tools(mcp: FastMCP):
    """注册商品分组的所有工具"""

    @mcp.tool(name=f"{GROUP_NAME}_query")
    async def query_products(
        current: Optional[int] = 1,
        size: Optional[int] = 10,
        name: Optional[str] = None,
        category: Optional[str] = None,
        supplier_id: Optional[int] = None,
        ctx: Context = None,
    ) -> list:
        """
        分页查询商品列表。
        支持按名称模糊查询、按分类筛选、按供应商ID筛选。

        Args:
            current: 当前页码，从1开始，默认1
            size: 每页大小，默认10
            name: 商品名称（模糊查询），可选
            category: 品类(洗发护发/沐浴清洁/洗衣护理/织物护理/家居清洁/护肤护理/口腔护理/冰淇淋/茶饮冲调/调味食品)，可选
            supplier_id: 供应商ID，可选
        """
        http_client = await get_http_client()

        # 构建请求参数（过滤 None 值，映射到 API 字段名）
        request_params = {}
        if current is not None:
            request_params["current"] = current
        if size is not None:
            request_params["size"] = size
        if name is not None:
            request_params["name"] = name
        if category is not None:
            request_params["category"] = category
        if supplier_id is not None:
            request_params["supplierId"] = supplier_id

        try:
            response = await http_client.get("/products/page", params=request_params)
            response.raise_for_status()
            result = response.json()

            # 检查业务状态码
            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]

            # 返回 data 字段，通常包含 records, total, current, size 等
            return result.get("data", {}).get("records", [])

        except Exception as e:
            return [f'没有查询到任何信息，而且报错: {e}']

    @mcp.tool(name=f"{GROUP_NAME}_search")
    async def search_products(name: str, ctx: Context) -> list:
        """
        按名称搜索商品。
        与 product_query 不同，此接口直接搜索，name 为必填参数。

        Args:
            name: 商品名称（模糊查询），必填
        """
        http_client = await get_http_client()

        try:
            response = await http_client.get("/products/search", params={"name": name})
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]

            # data 为数组
            return result.get("data", [])
        except Exception as e:
            return [f'没有查询到任何信息，而且报错: {e}']

    @mcp.tool(name=f"{GROUP_NAME}_quotes")
    async def quotes_for_product(product_id: int, ctx: Context) -> dict:
        """
        查询某商品的多供应商报价，用于采购比价。

        返回该商品的全部有效报价（按报价从低到高），并列出来源供应商的名称、
        信用评级、交期(lead_days)、最小起订量(moq) 与是否为主供应商(is_primary)。
        同时给出最低报价与相对当前采购价的差额，便于直接判断是否有降本空间。

        Args:
            product_id: 商品 ID（必填）
        """
        http_client = await get_http_client()

        try:
            response = await http_client.get(f"/products/{product_id}/quotes")
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]

            return result.get("data", {})
        except Exception as e:
            return [f'没有查询到任何信息，而且报错: {e}']

    @mcp.tool(name=f"{GROUP_NAME}_by_supplier")
    async def list_products_by_supplier(supplier_id: int, ctx: Context) -> list:
        """
        根据供应商 ID 查询该供应商下有采购记录的商品列表。

        Args:
            supplier_id: 供应商 ID（路径参数，必填）
        """
        http_client = await get_http_client()

        try:
            response = await http_client.get(f"/products/supplier/{supplier_id}")
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]

            return result.get("data", [])
        except Exception as e:
            return [f'没有查询到任何信息，而且报错: {e}']

    @mcp.tool(name=f"{GROUP_NAME}_create")
    async def create_product(
        product_code: str,
        name: str,
        purchase_price: float,
        model: Optional[str] = None,
        specification: Optional[str] = None,
        unit: Optional[str] = None,
        suggested_retail_price: Optional[float] = None,
        stock_warning_value: Optional[int] = None,
        supplier_id: Optional[int] = None,
        category: Optional[str] = None,
        description: Optional[str] = None,
        ctx: Context = None,
    ) -> dict:
        """
        新增商品主数据（POST /products/create）。

        用于「新 SKU 建档」：引入新的华联日化商品。
        注意：这是写操作，执行前会触发人工审批（HITL），审批通过后才真正落库。

        product_code 必须显式提供，不支持自动生成 —— 现有编码带业务含义
        （如 HFS-400-01 = 澜溪 400ml 第 1 号、MG-6-01 = 梦享），
        自动编号会产出无意义的编码。
        若填写 supplier_id，该供应商必须真实存在（系统会校验）。

        Args:
            product_code: 商品编码（唯一），必填。建议格式「品牌缩写-规格-序号」，如 HFS-750-02
            name: 商品名称，必填
            purchase_price: 采购价，必填且不能为负
            model: 型号，可选
            specification: 包装规格，如 400ml / 1L / 100g
            unit: 计量单位，如 瓶 / 盒 / 箱
            suggested_retail_price: 建议零售价，可选
            stock_warning_value: 安全库存阈值（低于该值触发库存预警），可选
            supplier_id: 供应商 ID，可选；传入则必须真实存在
            category: 品类，如 洗发护发 / 家庭清洁 / 冰淇淋 / 食品
            description: 商品描述，可选
        """
        http_client = await get_http_client()

        request_data: dict = {
            "productCode": product_code,
            "name": name,
            "purchasePrice": purchase_price,
        }
        if model is not None:
            request_data["model"] = model
        if specification is not None:
            request_data["specification"] = specification
        if unit is not None:
            request_data["unit"] = unit
        if suggested_retail_price is not None:
            request_data["suggestedRetailPrice"] = suggested_retail_price
        if stock_warning_value is not None:
            request_data["stockWarningValue"] = stock_warning_value
        if supplier_id is not None:
            request_data["supplierId"] = supplier_id
        if category is not None:
            request_data["category"] = category
        if description is not None:
            request_data["description"] = description

        try:
            response = await http_client.post("/products/create", json=request_data)
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                return {
                    "error": f"API error: code={result.get('code')}, message={result.get('message')}"
                }

            return result.get("data", {})
        except Exception as e:
            return {"error": str(e)}