from fastmcp import FastMCP, Context
from mcp_server.http_base import get_http_client
from typing import Optional

GROUP_NAME = "sales"


def register_sales_tools(mcp: FastMCP):
    """注册销售分组的所有工具"""

    @mcp.tool(name=f"{GROUP_NAME}_daily")
    async def query_sales_daily(
        product_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 200,
        ctx: Context = None,
    ) -> list:
        """
        查询日销售明细（按商品 + 日期的销量与销售额）。

        Args:
            product_id: 商品 ID，可选；不传则返回全部物料
            start_date: 起始日期，格式 yyyy-MM-dd，可选
            end_date: 结束日期，格式 yyyy-MM-dd，可选
            limit: 最多返回条数，默认 200
        """
        http_client = await get_http_client()

        params: dict = {"limit": limit}
        if product_id is not None:
            params["product_id"] = product_id
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        try:
            response = await http_client.get("/sales/daily", params=params)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]
            return result.get("data", {}).get("records", [])
        except Exception as e:
            return [f"没有查询到任何信息，而且报错: {e}"]

    @mcp.tool(name=f"{GROUP_NAME}_anomaly")
    async def scan_sales_anomaly(
        days: int = 30,
        drop_ratio: float = 0.5,
        spike_ratio: float = 2.5,
        limit: int = 100,
        ctx: Context = None,
    ) -> list:
        """
        扫描销量异常：找出骤降与激增的商品。

        以每个物料【自身】在窗口内的均值为基线（而非全局固定阈值），
        因此不同销量量级的物料都能被公平判断：
          · 骤降：当日销量 <= 均值 × drop_ratio
          · 激增：当日销量 >= 均值 × spike_ratio
        返回值按偏离程度从大到小排序，最异常的排在最前。

        Args:
            days: 统计窗口天数，默认 30
            drop_ratio: 骤降阈值比例，默认 0.5（即低于均值一半算骤降）
            spike_ratio: 激增阈值倍数，默认 2.5（即超过均值 2.5 倍算激增）
            limit: 最多返回条数，默认 100
        """
        http_client = await get_http_client()

        params = {"days": days, "drop_ratio": drop_ratio,
                  "spike_ratio": spike_ratio, "limit": limit}

        try:
            response = await http_client.get("/sales/anomaly", params=params)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]
            return result.get("data", {}).get("records", [])
        except Exception as e:
            return [f"没有查询到任何信息，而且报错: {e}"]

    @mcp.tool(name=f"{GROUP_NAME}_summary")
    async def sales_summary(
        dimension: str = "day",
        days: int = 30,
        product_id: Optional[int] = None,
        limit: int = 100,
        ctx: Context = None,
    ) -> list:
        """
        销量汇总 / 趋势：按指定维度聚合销量与销售额（趋势与结构分析共用本工具）。

        dimension 取值：
          · day / week   → 时间趋势（按时间升序）
          · channel      → 渠道结构（KA商超 / 经销商 / 电商）
          · category     → 品类结构
          · warehouse    → 区域 DC 结构
        分类维度按金额降序返回；每条附带 quantityShare / amountShare 占比。

        需要「某维度占比、趋势、结构」时一律用本工具，
        不要自己拉 sales_daily 明细再聚合（口径会漂移）。

        Args:
            dimension: 聚合维度，默认 day
            days: 统计窗口天数，默认 30
            product_id: 限定某个商品，可选
            limit: 最多返回条数，默认 100
        """
        http_client = await get_http_client()

        params: dict = {"dimension": dimension, "days": days, "limit": limit}
        if product_id is not None:
            params["product_id"] = product_id

        try:
            response = await http_client.get("/sales/summary", params=params)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 200:
                return [f"API error: code={result.get('code')}"]
            return result.get("data", {}).get("records", [])
        except Exception as e:
            return [f"没有查询到任何信息，而且报错: {e}"]

    @mcp.tool(name=f"{GROUP_NAME}_bonus")
    async def sales_bonus(
        dimension: str = "channel",
        month: Optional[str] = None,
        ctx: Context = None,
    ) -> dict:
        """
        销售奖金解释：目标 vs 实际 → 达成率 → 奖金档位。

        返回整体汇总（总目标 / 总实际 / 达成率 / 预计达成率）+ 各维度明细：
        目标金额、实际金额、达成率、预计达成率、奖金档位、奖金系数。

        口径（固定，不要自行推算）：
          · 实际 = 该月 1 日至今的销售额（未过完的月份只统计已发生部分）
          · 达成率 = 实际 / 目标；预计达成率 = 达成率 ÷ 时间进度
          · 奖金档位按达成率在固定规则表中匹配（左闭右开区间）
          · 同时返回两套档位：bonusTier（按当前达成率）与
            projectedTier（按预计达成率）

        解释奖金高低时**务必同时给出「达成率」与「时间进度」** ——
        月中完成率低不代表落后。月份未过完时，回答"最终大概能拿到什么档位"
        应引用 projectedTier，并说明最终以月末结算为准。

        Args:
            dimension: channel（KA/经销商/电商）或 category（品类），默认 channel
            month: 目标月份 YYYY-MM，默认当前月
        """
        http_client = await get_http_client()

        params: dict = {"dimension": dimension}
        if month:
            params["month"] = month

        try:
            response = await http_client.get("/sales/bonus", params=params)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 200:
                return {"error": f"API error: code={result.get('code')}"}
            return result.get("data", {})
        except Exception as e:
            return {"error": f"没有查询到任何信息，而且报错: {e}"}
