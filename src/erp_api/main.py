"""
ERP API — Python 实现的采购域 REST 层（替代原 Java 后端 localhost:8080/api）。

设计要点：
- 数据源：直连本地 MySQL motorparts_db（见 erp_api.db），不依赖外部 Java 服务；
- 返回形状与 MCP 工具（src/mcp_server）期望一致：{code:200, message, data}，
  实体字段统一 camelCase（orderId/productId/unitPrice/...）；
- 只读端点带 soft-delete(deleted=0) 过滤；写端点负责金额重算与事务。
"""
from datetime import datetime
import calendar
import random

import pymysql

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional

from erp_api.db import Connection

app = FastAPI(title="ERP API", version="1.0.0")


# ----------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------
def camel(key: str) -> str:
    """snake_case -> camelCase（保留首字母小写）。"""
    parts = key.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def row_to_camel(row: dict) -> dict:
    return {camel(k): v for k, v in row.items()}


def ok(data):
    return {"code": 200, "message": "success", "data": data}


def err(message: str, code: int = 400):
    return {"code": code, "message": message, "data": None}


def _fetch_product(cur, product_id: int) -> Optional[dict]:
    cur.execute("SELECT * FROM product WHERE id=%s AND deleted=0", (product_id,))
    return cur.fetchone()


def _fetch_order(cur, order_id: int) -> Optional[dict]:
    cur.execute("SELECT * FROM purchase_order WHERE id=%s AND deleted=0", (order_id,))
    return cur.fetchone()


def _fetch_details(cur, order_id: int) -> List[dict]:
    cur.execute("SELECT * FROM order_detail WHERE order_id=%s AND deleted=0", (order_id,))
    return cur.fetchall()


def _ts(iso: Optional[str]):
    """'yyyy-MM-ddTHH:mm:ss' -> 'yyyy-MM-dd HH:mm:ss'，兼容两种分隔符。"""
    if not iso:
        return None
    return iso.replace("T", " ")[:19]


@app.get("/health")
def health():
    return {"status": "healthy", "service": "ERP API"}


# ----------------------------------------------------------------
# 统一异常形状：把 FastAPI 默认的 422 {"detail":[...]} 收敛成 {code,message,data}
# ----------------------------------------------------------------
# 为什么必须做：本项目所有端点都返回 {code, message, data}，MCP 工具按
# result.get("code") != 200 判断成败。若参数校验失败走 FastAPI 默认的
# {"detail": [...]}，MCP 侧拿到的形状就变了，**Agent 看不到「哪个字段缺了」**，
# 也就无法据此自我纠正（补参数后重试）。统一后 Agent 能直接读到中文原因。
@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request, exc: RequestValidationError):
    parts = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e.get("loc", ()) if x != "body")
        parts.append("%s (%s)" % (loc or "body", e.get("msg", "")))
    return JSONResponse(
        status_code=200,
        content=err("参数校验失败: " + "; ".join(parts)),
    )


# ----------------------------------------------------------------
# 商品 product
# ----------------------------------------------------------------
@app.get("/api/products/page")
def products_page(current: int = 1, size: int = 10, name: Optional[str] = None,
               category: Optional[str] = None, supplier_id: Optional[int] = None):
    """分页查询商品，返回 {records,total,current,size}（MyBatis-Plus 风格）。"""
    where = ["p.deleted=0"]
    params: list = []
    if name:
        where.append("p.name LIKE %s")
        params.append("%" + name + "%")
    if category:
        where.append("p.category=%s")
        params.append(category)
    if supplier_id:
        where.append("p.supplier_id=%s")
        params.append(supplier_id)
    cond = " AND ".join(where)
    offset = (current - 1) * size
    with Connection() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM product p WHERE " + cond, tuple(params))
        total = cur.fetchone()["n"]
        cur.execute(
            "SELECT p.*, s.name AS supplier_name FROM product p "
            "LEFT JOIN supplier s ON s.id=p.supplier_id WHERE " + cond +
            " ORDER BY p.id LIMIT %s OFFSET %s",
            tuple(params + [size, offset]),
        )
        records = [row_to_camel(r) for r in cur.fetchall()]
    return ok({"records": records, "total": total, "current": current, "size": size})


@app.get("/api/products/search")
def products_search(name: str):
    """按名称搜索商品，直接返回数组。"""
    if not name:
        return err("name 参数必填")
    with Connection() as cur:
        cur.execute(
            "SELECT p.*, s.name AS supplier_name FROM product p "
            "LEFT JOIN supplier s ON s.id=p.supplier_id "
            "WHERE p.deleted=0 AND p.name LIKE %s ORDER BY p.id LIMIT 50",
            ("%" + name + "%",),
        )
        data = [row_to_camel(r) for r in cur.fetchall()]
    return ok(data)


@app.get("/api/products/supplier/{supplier_id}")
def products_by_supplier(supplier_id: int):
    """按供应商 ID 查该供应商下有采购记录的商品。"""
    with Connection() as cur:
        cur.execute(
            "SELECT DISTINCT p.*, s.name AS supplier_name FROM product p "
            "JOIN order_detail od ON od.product_id=p.id AND od.deleted=0 "
            "JOIN supplier s ON s.id=p.supplier_id "
            "WHERE p.deleted=0 AND p.supplier_id=%s ORDER BY p.id",
            (supplier_id,),
        )
        data = [row_to_camel(r) for r in cur.fetchall()]
    return ok(data)


# ----------------------------------------------------------------
# 供应商 supplier
# ----------------------------------------------------------------
@app.get("/api/suppliers/search")
def suppliers_search(name: str):
    """按名称模糊搜索供应商。"""
    if not name:
        return err("name 参数必填")
    with Connection() as cur:
        cur.execute(
            "SELECT * FROM supplier WHERE deleted=0 AND name LIKE %s ORDER BY id LIMIT 50",
            ("%" + name + "%",),
        )
        data = [row_to_camel(r) for r in cur.fetchall()]
    return ok(data)


# ----------------------------------------------------------------
# 库存 inventory
# ----------------------------------------------------------------
@app.get("/api/inventory/warning")
def inventory_warning():
    """库存预警：current_quantity < safety_stock，附商品详情。"""
    with Connection() as cur:
        cur.execute(
            "SELECT i.id, i.product_id, i.current_quantity, i.safety_stock, i.warehouse_location "
            "FROM inventory i WHERE i.deleted=0 AND i.current_quantity < i.safety_stock "
            "ORDER BY (i.safety_stock - i.current_quantity) DESC",
        )
        rows = cur.fetchall()
        data = []
        for r in rows:
            cur.execute(
                "SELECT p.*, s.name AS supplier_name FROM product p "
                "LEFT JOIN supplier s ON s.id=p.supplier_id "
                "WHERE p.id=%s AND p.deleted=0",
                (r["product_id"],),
            )
            product = cur.fetchone()
            if not product:
                continue
            item = row_to_camel({k: v for k, v in product.items()})
            # 商品名统一叫 productName —— 与 /inventory/query、/sales/* 保持一致
            # （本端点用 p.* 取字段，camel 后会得到歧义的 name）
            item["productName"] = item.pop("name", None)
            item["productId"] = r["product_id"]
            item["currentQuantity"] = r["current_quantity"]
            item["safetyStock"] = r["safety_stock"]
            item["warehouseLocation"] = r["warehouse_location"]
            data.append(item)
    return ok(data)


# ----------------------------------------------------------------
# 供应商比价 product_supplier
# ----------------------------------------------------------------
@app.get("/api/products/{product_id}/quotes")
def products_quotes(product_id: int):
    """多供应商报价比价：返回该商品的全部有效报价（按报价升序）。

    附派生字段 lowestQuote / savingVsCurrent —— 让采购分析子 Agent 直接拿结论，
    不必自己再算（也是「结构化输出收口在工具层」的落点）。
    """
    with Connection() as cur:
        cur.execute(
            "SELECT id, name, purchase_price FROM product WHERE id=%s AND deleted=0",
            (product_id,),
        )
        product = cur.fetchone()
        if not product:
            return err("商品不存在: " + str(product_id))

        cur.execute(
            "SELECT ps.quote_price, ps.lead_days, ps.moq, ps.is_primary, "
            "       ps.valid_from, ps.valid_to, ps.remark, "
            "       s.id AS supplier_id, s.name AS supplier_name, s.credit_rating "
            "FROM product_supplier ps "
            "JOIN supplier s ON s.id = ps.supplier_id AND s.deleted = 0 "
            "WHERE ps.product_id=%s AND ps.deleted=0 "
            "ORDER BY ps.quote_price ASC",
            (product_id,),
        )
        quotes = [row_to_camel(r) for r in cur.fetchall()]

    current = float(product["purchase_price"] or 0)
    lowest = float(quotes[0]["quotePrice"]) if quotes else None
    saving = round(current - lowest, 2) if lowest is not None else None

    return ok({
        "productId": product["id"],
        "productName": product["name"],
        "currentPurchasePrice": current,
        "quoteCount": len(quotes),
        "lowestQuote": lowest,
        "savingVsCurrent": saving,
        "savingRatio": round(saving / current, 4) if (saving is not None and current) else None,
        "quotes": quotes,
    })


# ----------------------------------------------------------------
# 库存查询 inventory（比 /warning 更通用）
# ----------------------------------------------------------------
@app.get("/api/inventory/query")
def inventory_query(
    product_id: Optional[int] = None,
    name: Optional[str] = None,
    warehouse_location: Optional[str] = None,
    only_warning: bool = False,
    limit: int = 50,
):
    """库存查询：按物料/名称/库位筛选，only_warning 只看低于安全库存的。

    排序把「预警项」顶到最前，方便 Agent 直接取前 N 条下结论。
    """
    where = ["i.deleted=0", "p.deleted=0"]
    params: list = []
    if product_id:
        where.append("i.product_id=%s")
        params.append(product_id)
    if name:
        where.append("p.name LIKE %s")
        params.append("%" + name + "%")
    if warehouse_location:
        where.append("i.warehouse_location=%s")
        params.append(warehouse_location)
    if only_warning:
        where.append("i.current_quantity < i.safety_stock")

    size = max(1, min(int(limit), 500))
    with Connection() as cur:
        cur.execute(
            "SELECT i.product_id, i.current_quantity, i.safety_stock, i.warehouse_location, "
            "       i.last_inbound_time, i.last_outbound_time, "
            "       p.name AS product_name, p.product_code, p.category, p.unit, "
            "       s.name AS supplier_name "
            "FROM inventory i "
            "JOIN product p ON p.id = i.product_id "
            "LEFT JOIN supplier s ON s.id = p.supplier_id "
            "WHERE " + " AND ".join(where) +
            " ORDER BY (i.current_quantity < i.safety_stock) DESC, i.product_id "
            "LIMIT %s",
            tuple(params + [size]),
        )
        rows = [row_to_camel(r) for r in cur.fetchall()]

    warnings = sum(1 for r in rows if r["currentQuantity"] < (r["safetyStock"] or 0))
    return ok({"total": len(rows), "warningCount": warnings, "records": rows})


# ----------------------------------------------------------------
# 库存覆盖分析 inventory coverage（补货 / 生产预测的收口口径）
# ----------------------------------------------------------------
@app.get("/api/inventory/coverage")
def inventory_coverage(
    product_id: Optional[int] = None,
    max_days: Optional[float] = None,
    limit: int = 50,
):
    """库存覆盖分析：把销量折算成**库存域口径**，供补货/生产预测直接下结论。

    对每个商品返回：当前库存、安全库存、近 30 天日均销量、可售天数、建议补货量。

    设计要点：把「日均销量 / 可售天数 / 补货量」的计算**收口在本端点** ——
    库存子 Agent 只需调用它，不必自己拉日销明细再写代码推算，
    从根本上避免「这次按 14 天阈值、下次按 30 天」的口径漂移。
    """
    cond = ["i.deleted=0", "p.deleted=0"]
    params: list = []
    if product_id:
        cond.append("i.product_id=%s")
        params.append(product_id)

    size = max(1, min(int(limit), 500))
    outer = ""
    if max_days is not None:
        outer = "WHERE days_of_supply IS NOT NULL AND days_of_supply < %s "
        params.append(max_days)

    with Connection() as cur:
        cur.execute(
            "WITH sales AS ("
            "    SELECT product_id, SUM(quantity) / 30.0 AS avg_daily"
            "    FROM sales_daily"
            "    WHERE deleted=0 AND sales_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)"
            "    GROUP BY product_id"
            "), cov AS ("
            "    SELECT i.product_id, p.name AS product_name, p.unit, p.category,"
            "           i.current_quantity, i.safety_stock, i.warehouse_location,"
            "           ROUND(COALESCE(s.avg_daily, 0), 2) AS avg_daily_sales,"
            "           CASE WHEN COALESCE(s.avg_daily, 0) > 0"
            "                THEN ROUND(i.current_quantity / s.avg_daily, 1) END AS days_of_supply,"
            "           CASE WHEN COALESCE(s.avg_daily, 0) > 0"
            "                THEN GREATEST(0, ROUND(s.avg_daily * 30 - i.current_quantity, 0))"
            "           END AS reorder_qty"
            "    FROM inventory i"
            "    JOIN product p ON p.id = i.product_id"
            "    LEFT JOIN sales s ON s.product_id = i.product_id"
            "    WHERE " + " AND ".join(cond) +
            ") "
            "SELECT * FROM cov " + outer +
            "ORDER BY (days_of_supply IS NULL), days_of_supply ASC LIMIT %s",
            tuple(params + [size]),
        )
        rows = [row_to_camel(r) for r in cur.fetchall()]

    # 覆盖状态分档（阈值口径固定在这里，不让调用方各自解释）
    for r in rows:
        d = r.get("daysOfSupply")
        if d is None:
            r["coverageStatus"] = "无销量数据"
        elif d < 7:
            r["coverageStatus"] = "紧急"
        elif d < 14:
            r["coverageStatus"] = "偏低"
        else:
            r["coverageStatus"] = "充足"

    return ok({
        "total": len(rows),
        "urgentCount": sum(1 for r in rows if r.get("coverageStatus") == "紧急"),
        "records": rows,
    })


# ----------------------------------------------------------------
# 物流查询 logistics
# ----------------------------------------------------------------
@app.get("/api/logistics/search")
def logistics_search(
    order_id: Optional[int] = None,
    tracking_number: Optional[str] = None,
    status: Optional[int] = None,
    overdue_only: bool = False,
    limit: int = 50,
):
    """物流查询：按订单/运单号/状态筛选。

    overdue_only=True 表示「预计到达时间已过、但 actual_arrival_time 仍为空」——
    即超期未签收，这是物流监控最需要盯的一类。
    """
    where = ["lg.deleted=0"]
    params: list = []
    if order_id:
        where.append("lg.order_id=%s")
        params.append(order_id)
    if tracking_number:
        where.append("lg.tracking_number LIKE %s")
        params.append("%" + tracking_number + "%")
    if status is not None:
        where.append("lg.status=%s")
        params.append(status)
    if overdue_only:
        where.append("lg.actual_arrival_time IS NULL AND lg.estimated_arrival_time < NOW()")

    size = max(1, min(int(limit), 500))
    with Connection() as cur:
        cur.execute(
            "SELECT lg.*, po.order_number, po.status AS order_status, "
            "       s.name AS supplier_name "
            "FROM logistics lg "
            "LEFT JOIN purchase_order po ON po.id = lg.order_id "
            "LEFT JOIN supplier s ON s.id = po.supplier_id "
            "WHERE " + " AND ".join(where) +
            " ORDER BY lg.ship_time DESC LIMIT %s",
            tuple(params + [size]),
        )
        rows = [row_to_camel(r) for r in cur.fetchall()]

    return ok({"total": len(rows), "records": rows})


# ----------------------------------------------------------------
# 物流时效统计 logistics performance（物流监控的核心指标）
# ----------------------------------------------------------------
@app.get("/api/logistics/performance")
def logistics_performance(days: int = 90, limit: int = 20):
    """物流时效统计：按承运商聚合单量、准时率、平均在途天数、超期未签收单量。

    设计要点：把「准时率 / 平均在途天数」的聚合**收口在本端点** ——
    物流监控子 Agent 只需调用它拿指标，不必自己写聚合 SQL 或 Python。

    口径：
      · 准时 = actual_arrival_time <= estimated_arrival_time（仅对已签收单计算）
      · 平均在途天数 = DATEDIFF(actual_arrival_time, ship_time)，仅对已签收单
      · 超期 = actual_arrival_time 为空 且 estimated_arrival_time 已过
    """
    win = max(7, min(int(days), 365))
    size = max(1, min(int(limit), 100))
    with Connection() as cur:
        cur.execute(
            "SELECT lg.logistics_company AS company,"
            "       COUNT(*) AS shipment_count,"
            "       SUM(CASE WHEN lg.actual_arrival_time IS NOT NULL"
            "                THEN 1 ELSE 0 END) AS arrived_count,"
            "       SUM(CASE WHEN lg.actual_arrival_time IS NOT NULL"
            "                 AND lg.actual_arrival_time <= lg.estimated_arrival_time"
            "                THEN 1 ELSE 0 END) AS on_time_count,"
            "       SUM(CASE WHEN lg.actual_arrival_time IS NULL"
            "                 AND lg.estimated_arrival_time < NOW()"
            "                THEN 1 ELSE 0 END) AS overdue_count,"
            "       ROUND(AVG(CASE WHEN lg.actual_arrival_time IS NOT NULL"
            "                      THEN DATEDIFF(lg.actual_arrival_time, lg.ship_time)"
            "                 END), 1) AS avg_transit_days"
            " FROM logistics lg"
            " WHERE lg.deleted=0 AND lg.ship_time >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"
            " GROUP BY lg.logistics_company"
            " ORDER BY shipment_count DESC LIMIT %s",
            (win, size),
        )
        rows = [row_to_camel(r) for r in cur.fetchall()]

    for r in rows:
        arrived = r.get("arrivedCount") or 0
        r["onTimeRate"] = round((r.get("onTimeCount") or 0) / arrived, 4) if arrived else None

    return ok({"windowDays": win, "total": len(rows), "records": rows})


# ----------------------------------------------------------------
# 销售查询 sales_daily（P2 新增域）
# ----------------------------------------------------------------
@app.get("/api/sales/daily")
def sales_daily_query(
    product_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 200,
):
    """日销查询：按物料/日期区间返回明细 + 区间汇总。"""
    where = ["sd.deleted=0"]
    params: list = []
    if product_id:
        where.append("sd.product_id=%s")
        params.append(product_id)
    if start_date:
        where.append("sd.sales_date >= %s")
        params.append(start_date)
    if end_date:
        where.append("sd.sales_date <= %s")
        params.append(end_date)
    cond = " AND ".join(where)

    size = max(1, min(int(limit), 2000))
    with Connection() as cur:
        cur.execute(
            "SELECT sd.product_id, sd.warehouse_location, sd.sales_date, "
            "       sd.quantity, sd.amount, p.name AS product_name, p.category "
            "FROM sales_daily sd JOIN product p ON p.id = sd.product_id "
            "WHERE " + cond + " ORDER BY sd.sales_date DESC, sd.product_id LIMIT %s",
            tuple(params + [size]),
        )
        rows = [row_to_camel(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT COUNT(*) AS dayCount, COUNT(DISTINCT sd.product_id) AS productCount, "
            "       SUM(sd.quantity) AS totalQuantity, SUM(sd.amount) AS totalAmount, "
            "       ROUND(AVG(sd.quantity), 2) AS avgQuantityPerRow "
            "FROM sales_daily sd WHERE " + cond,
            tuple(params),
        )
        summary = row_to_camel(cur.fetchone())

    return ok({"summary": summary, "total": len(rows), "records": rows})


@app.get("/api/sales/anomaly")
def sales_anomaly(
    days: int = 30,
    drop_ratio: float = 0.5,
    spike_ratio: float = 2.5,
    limit: int = 100,
):
    """销量异常检测：以每个物料**自身**在窗口内的均值为基线，找

    - 骤降：quantity <= 均值 × drop_ratio
    - 激增：quantity >= 均值 × spike_ratio

    用自身基线而非全局阈值，避免「不同物料销量量级不同」导致的误报。
    """
    win = max(2, min(int(days), 365))
    size = max(1, min(int(limit), 1000))
    with Connection() as cur:
        cur.execute(
            "WITH stat AS ("
            "    SELECT product_id, AVG(quantity) AS avg_q"
            "    FROM sales_daily"
            "    WHERE deleted=0 AND sales_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"
            "    GROUP BY product_id"
            ") "
            "SELECT sd.product_id, p.name AS product_name, p.category, "
            "       sd.sales_date, sd.quantity, sd.amount, "
            "       ROUND(st.avg_q, 1) AS avg_quantity, "
            "       ROUND(sd.quantity / NULLIF(st.avg_q, 0), 2) AS ratio, "
            "       CASE WHEN sd.quantity <= st.avg_q * %s THEN 'drop' ELSE 'spike' END "
            "           AS anomaly_type "
            "FROM sales_daily sd "
            "JOIN stat st ON st.product_id = sd.product_id "
            "JOIN product p ON p.id = sd.product_id "
            "WHERE sd.deleted=0 "
            "  AND sd.sales_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
            "  AND (sd.quantity <= st.avg_q * %s OR sd.quantity >= st.avg_q * %s) "
            "ORDER BY ABS(1 - sd.quantity / NULLIF(st.avg_q, 0)) DESC, "
            "         sd.product_id, sd.sales_date "
            "LIMIT %s",
            (win, drop_ratio, win, drop_ratio, spike_ratio, size),
        )
        rows = [row_to_camel(r) for r in cur.fetchall()]

        # 汇总单独查一次 —— 不能从上面的 rows 里数：
        # 那样会被 LIMIT 截断，导致「只看到 drop、spike 恒为 0」。
        cur.execute(
            "WITH stat AS ("
            "    SELECT product_id, AVG(quantity) AS avg_q"
            "    FROM sales_daily"
            "    WHERE deleted=0 AND sales_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"
            "    GROUP BY product_id"
            ") "
            "SELECT "
            "  SUM(CASE WHEN sd.quantity <= st.avg_q * %s THEN 1 ELSE 0 END) AS drop_count, "
            "  SUM(CASE WHEN sd.quantity >= st.avg_q * %s THEN 1 ELSE 0 END) AS spike_count "
            "FROM sales_daily sd JOIN stat st ON st.product_id = sd.product_id "
            "WHERE sd.deleted=0 AND sd.sales_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)",
            (win, drop_ratio, spike_ratio, win),
        )
        agg = cur.fetchone() or {}

    return ok({
        "windowDays": win,
        "dropThreshold": drop_ratio,
        "spikeThreshold": spike_ratio,
        "summary": {
            "returned": len(rows),
            "drop": int(agg.get("drop_count") or 0),
            "spike": int(agg.get("spike_count") or 0),
        },
        "records": rows,
    })


# ----------------------------------------------------------------
# 销量汇总 / 趋势（P2-b 收口：把聚合口径固定在这里）
# ----------------------------------------------------------------
# 维度 → 分组表达式与取值来源。时间维度按时间升序，分类维度按金额降序。
# ⚠️ 每个维度的「分组表达式」与「取值表达式」必须一致（或分组表达式能覆盖取值表达式），
#    否则在 ONLY_FULL_GROUP_BY 下会报错 1055 —— 例如按 YEARWEEK() 分组却 SELECT YEAR()。
_WEEK_EXPR = ("CONCAT(YEAR(sd.sales_date), '-W', "
              "LPAD(WEEK(sd.sales_date, 1), 2, '0'))")
_SUMMARY_DIMS = {
    "day":       ("sd.sales_date", "sd.sales_date"),
    "week":      (_WEEK_EXPR, _WEEK_EXPR),
    "channel":   ("sd.channel", "sd.channel"),
    "warehouse": ("sd.warehouse_location", "sd.warehouse_location"),
    "category":  ("p.category", "p.category"),
}


@app.get("/api/sales/summary")
def sales_summary(
    dimension: str = "day",
    days: int = 30,
    product_id: Optional[int] = None,
    limit: int = 100,
):
    """销量汇总 / 趋势：按指定维度聚合销量与销售额。

    dimension 取值（趋势与结构分析共用同一个聚合，只是 GROUP BY 不同）：
      · day / week       → 时间趋势
      · channel          → 渠道结构（KA商超 / 经销商 / 电商）
      · category         → 品类结构
      · warehouse        → 区域 DC 结构

    返回每条记录的销量、销售额与占比；时间维度按时间升序，分类维度按金额降序。
    """
    if dimension not in _SUMMARY_DIMS:
        return err("dimension 取值必须是: " + " / ".join(_SUMMARY_DIMS))

    grp, label = _SUMMARY_DIMS[dimension]
    win = max(1, min(int(days), 365))
    size = max(1, min(int(limit), 1000))

    cond = ["sd.deleted=0", "sd.sales_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"]
    params: list = [win]
    if product_id:
        cond.append("sd.product_id=%s")
        params.append(product_id)

    order = "1 ASC" if dimension in ("day", "week") else "amount DESC"

    with Connection() as cur:
        cur.execute(
            "SELECT " + label + " AS key_name, "
            "       SUM(sd.quantity) AS quantity, "
            "       ROUND(SUM(sd.amount), 2) AS amount "
            "FROM sales_daily sd "
            "JOIN product p ON p.id = sd.product_id "
            "WHERE " + " AND ".join(cond) +
            " GROUP BY " + grp +
            " ORDER BY " + order + " LIMIT %s",
            tuple(params + [size]),
        )
        rows = [row_to_camel(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT SUM(sd.quantity) AS total_quantity, "
            "       ROUND(SUM(sd.amount), 2) AS total_amount, "
            "       COUNT(DISTINCT sd.sales_date) AS day_count, "
            "       COUNT(DISTINCT sd.product_id) AS product_count "
            "FROM sales_daily sd JOIN product p ON p.id = sd.product_id "
            "WHERE " + " AND ".join(cond),
            tuple(params),
        )
        summary = row_to_camel(cur.fetchone() or {})

    total_q = summary.get("totalQuantity") or 0
    total_a = float(summary.get("totalAmount") or 0)
    for r in rows:
        r["quantityShare"] = round((r.get("quantity") or 0) / total_q, 4) if total_q else None
        r["amountShare"] = round(float(r.get("amount") or 0) / total_a, 4) if total_a else None

    summary["avgDailyAmount"] = round(total_a / (summary.get("dayCount") or 1), 2)
    return ok({"dimension": dimension, "windowDays": win,
               "summary": summary, "total": len(rows), "records": rows})


# ----------------------------------------------------------------
# 销售奖金解释（目标 vs 实际 → 达成率 → 奖金档位）
# ----------------------------------------------------------------
def _bonus_tier(rate: Optional[float], rules: List[dict]) -> Optional[dict]:
    """按达成率匹配奖金档位；区间为左闭右开 [min, max)。"""
    if rate is None:
        return None
    for r in rules:
        lo = float(r["achieve_min"])
        hi = r["achieve_max"]
        if rate >= lo and (hi is None or rate < float(hi)):
            return row_to_camel(r)
    return None


@app.get("/api/sales/bonus")
def sales_bonus(
    month: Optional[str] = None,
    dimension: str = "channel",
):
    """销售奖金解释：目标 vs 实际 → 达成率 → 奖金档位。

    口径（固定，调用方不要自行解释）：
      · 目标：`sales_target` 中该月、该维度的月度目标
      · 实际：该月 1 日至今的销售额合计（未过完的月份只统计已发生部分）
      · 达成率 = 实际 / 目标
      · 时间进度 = 该月已过天数 / 该月总天数；预计达成率 = 达成率 / 时间进度
      · 奖金档位：按达成率在 `bonus_rule` 中匹配左闭右开区间

    Args:
        month: 目标月份 YYYY-MM，默认当前月
        dimension: channel（KA/经销商/电商）或 category（品类）
    """
    if dimension not in ("channel", "category"):
        return err("dimension 取值必须是: channel / category")

    mon = month or datetime.now().strftime("%Y-%m")
    try:
        y, m = int(mon[:4]), int(mon[5:7])
        days_in_month = calendar.monthrange(y, m)[1]
    except Exception:
        return err("month 格式应为 YYYY-MM")

    today = datetime.now()
    if (y, m) == (today.year, today.month):
        elapsed = today.day
    elif (y, m) < (today.year, today.month):
        elapsed = days_in_month
    else:
        elapsed = 0
    month_progress = round(elapsed / days_in_month, 4) if days_in_month else 0

    # 实际销售额：channel 直接分组；category 需 join product
    if dimension == "channel":
        actual_sql = ("SELECT sd.channel AS dv, SUM(sd.amount) AS amt "
                      "FROM sales_daily sd "
                      "WHERE sd.deleted=0 AND DATE_FORMAT(sd.sales_date,'%%Y-%%m')=%s "
                      "GROUP BY sd.channel")
    else:
        actual_sql = ("SELECT p.category AS dv, SUM(sd.amount) AS amt "
                      "FROM sales_daily sd JOIN product p ON p.id=sd.product_id "
                      "WHERE sd.deleted=0 AND DATE_FORMAT(sd.sales_date,'%%Y-%%m')=%s "
                      "GROUP BY p.category")

    with Connection() as cur:
        cur.execute(
            "SELECT t.dimension_value, t.target_amount, t.owner, "
            "       COALESCE(a.amt, 0) AS actual_amount "
            "FROM sales_target t "
            "LEFT JOIN (" + actual_sql + ") a ON a.dv = t.dimension_value "
            "WHERE t.deleted=0 AND t.target_month=%s AND t.dimension=%s "
            "ORDER BY t.dimension_value",
            (mon, mon, dimension),
        )
        rows = cur.fetchall()

        cur.execute("SELECT tier_name, achieve_min, achieve_max, bonus_rate, remark "
                    "FROM bonus_rule WHERE deleted=0 ORDER BY achieve_min")
        rules = cur.fetchall()

    out = []
    for r in rows:
        target = float(r["target_amount"] or 0)
        actual = float(r["actual_amount"] or 0)
        rate = round(actual / target, 4) if target else None
        proj = round(rate / month_progress, 4) if (rate is not None and month_progress) else None
        tier = _bonus_tier(rate, rules)
        proj_tier = _bonus_tier(proj, rules)
        out.append({
            "dimensionValue": r["dimension_value"],
            "owner": r["owner"],
            "targetAmount": target,
            "actualAmount": round(actual, 2),
            "achieveRate": rate,
            "projectedRate": proj,
            # 当前完成率对应的档位（月中仅供参考）
            "bonusTier": (tier or {}).get("tierName"),
            "bonusRate": (tier or {}).get("bonusRate"),
            "bonusRemark": (tier or {}).get("remark"),
            # 按预计达成率外推的档位 —— 月中解释"最终大概能拿多少"应以此为准
            "projectedTier": (proj_tier or {}).get("tierName"),
            "projectedBonusRate": (proj_tier or {}).get("bonusRate"),
        })

    total_target = sum(x["targetAmount"] for x in out)
    total_actual = sum(x["actualAmount"] for x in out)
    total_rate = round(total_actual / total_target, 4) if total_target else None

    return ok({
        "month": mon,
        "dimension": dimension,
        "daysElapsed": elapsed,
        "daysInMonth": days_in_month,
        "monthProgress": month_progress,
        "summary": {
            "totalTarget": round(total_target, 2),
            "totalActual": round(total_actual, 2),
            "achieveRate": total_rate,
            "projectedRate": round(total_rate / month_progress, 4) if (total_rate is not None and month_progress) else None,
        },
        "records": out,
    })


# ----------------------------------------------------------------
# 元数据查询（结构 + 业务字典）
#   结构：实时读 information_schema —— 零维护，永远与库一致
#   语义：读人工维护的 meta_dict —— 数据库不存业务含义，只能人工写
# ----------------------------------------------------------------
@app.get("/api/meta/schema")
def meta_schema(table: Optional[str] = None):
    """元数据-结构：不传 table 返回表清单（含用途），传 table 返回该表字段清单。

    **结构信息一律从 information_schema 实时读取，不手工维护** ——
    这样改列名/加列后不会出现"字典表没同步"的不一致。
    """
    with Connection() as cur:
        if not table:
            # ⚠️ information_schema 的列名是全大写，直接过 camel() 会得到 TABLEName
            #    这种畸形 key —— 必须显式别名成小写 snake_case
            cur.execute(
                "SELECT t.TABLE_NAME    AS table_name, "
                "       t.TABLE_COMMENT AS table_comment, "
                "       d.meaning       AS purpose, "
                "       (SELECT COUNT(*) FROM information_schema.COLUMNS c "
                "         WHERE c.TABLE_SCHEMA = t.TABLE_SCHEMA "
                "           AND c.TABLE_NAME = t.TABLE_NAME) AS column_count "
                "FROM information_schema.TABLES t "
                "LEFT JOIN meta_dict d ON d.dict_type='table' "
                "     AND d.table_name = t.TABLE_NAME AND d.deleted = 0 "
                "WHERE t.TABLE_SCHEMA = DATABASE() AND t.TABLE_TYPE = 'BASE TABLE' "
                "  AND LEFT(t.TABLE_NAME, 1) <> '_' "
                "ORDER BY t.TABLE_NAME"
            )
            rows = [row_to_camel(r) for r in cur.fetchall()]
            return ok({"total": len(rows), "records": rows})

        cur.execute(
            "SELECT COUNT(*) AS n FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
        if not cur.fetchone()["n"]:
            return err("表不存在: " + table)

        cur.execute(
            "SELECT COLUMN_NAME    AS column_name, "
            "       COLUMN_TYPE    AS column_type, "
            "       IS_NULLABLE    AS is_nullable, "
            "       COLUMN_KEY     AS column_key, "
            "       COLUMN_COMMENT AS column_comment "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION", (table,))
        cols = [row_to_camel(r) for r in cur.fetchall()]

        cur.execute("SELECT meaning FROM meta_dict "
                    "WHERE dict_type='table' AND table_name=%s AND deleted=0 LIMIT 1", (table,))
        row = cur.fetchone()

    return ok({"table": table, "purpose": (row or {}).get("meaning"),
               "columnCount": len(cols), "columns": cols})


@app.get("/api/meta/dict")
def meta_dict_query(table: str, column: Optional[str] = None):
    """元数据-语义：字段取值是什么意思 / 该表有哪些枚举字段。

    两级来源（口径固定）：
      1. **优先**查人工维护的 `meta_dict` —— 编码型字段，如 customer_type=1 → KA商超
      2. 字典里没有的（值即含义的自描述字段，如 channel / category）→
         **实时**取该字段 DISTINCT 值与计数兜底

    两者合并返回，统一成 items[{code, meaning, rowCount}]；字典里有但数据里没有的
    取值也会返回（rowCount=0），便于提示"该取值当前无数据"。
    """
    if column:
        # ⚠️ 动态 SQL 的字段名必须双重校验：先语法（标识符），再存在性
        if not column.isidentifier():
            return err("字段名非法: " + column)
        cur_table = table
        with Connection() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                (cur_table, column))
            if not cur.fetchone()["n"]:
                return err(f"字段不存在: {cur_table}.{column}")

            cur.execute(
                "SELECT value_code, meaning, sort_no FROM meta_dict "
                "WHERE dict_type='enum' AND table_name=%s AND column_name=%s AND deleted=0 "
                "ORDER BY sort_no, value_code", (cur_table, column))
            dict_rows = cur.fetchall()

            try:
                cur.execute(
                    f"SELECT `{column}` AS v, COUNT(*) AS n FROM `{cur_table}` "
                    f"WHERE deleted=0 GROUP BY `{column}` ORDER BY n DESC LIMIT 50")
                data_rows = cur.fetchall()
            except Exception:
                data_rows = []

        counts = {str(r["v"]): r["n"] for r in data_rows}
        items, seen = [], set()
        for r in dict_rows:
            code = str(r["value_code"])
            seen.add(code)
            items.append({"code": code, "meaning": r["meaning"],
                          "rowCount": counts.get(code, 0)})
        for r in data_rows:
            code = str(r["v"])
            if code not in seen:
                items.append({"code": code, "meaning": None, "rowCount": r["n"]})

        return ok({"table": table, "column": column,
                   "source": "dict+distinct" if dict_rows else "distinct",
                   "total": len(items), "items": items})

    # 不传 column：返回该表已维护的全部枚举字段
    with Connection() as cur:
        cur.execute(
            "SELECT column_name, value_code, meaning, sort_no FROM meta_dict "
            "WHERE dict_type='enum' AND table_name=%s AND deleted=0 "
            "ORDER BY column_name, sort_no, value_code", (table,))
        rows = cur.fetchall()

    grouped: dict = {}
    for r in rows:
        grouped.setdefault(r["column_name"], []).append(
            {"code": r["value_code"], "meaning": r["meaning"]})

    return ok({"table": table, "dictColumnCount": len(grouped), "columns": grouped})


# ----------------------------------------------------------------
# 客户查询 customer（主数据管理域）
# ----------------------------------------------------------------
@app.get("/api/customers/search")
def customers_search(
    name: Optional[str] = None,
    customer_type: Optional[int] = None,
    discount_level: Optional[int] = None,
    limit: int = 50,
):
    """客户档案查询（主数据管理域）：名称/编码模糊匹配 + 类型/折扣等级筛选。"""
    where = ["deleted=0"]
    params: list = []
    if name:
        where.append("(name LIKE %s OR customer_code LIKE %s)")
        params += ["%" + name + "%", "%" + name + "%"]
    if customer_type is not None:
        where.append("customer_type=%s")
        params.append(customer_type)
    if discount_level is not None:
        where.append("discount_level=%s")
        params.append(discount_level)

    size = max(1, min(int(limit), 500))
    with Connection() as cur:
        cur.execute(
            "SELECT * FROM customer WHERE " + " AND ".join(where) +
            " ORDER BY id LIMIT %s",
            tuple(params + [size]),
        )
        rows = [row_to_camel(r) for r in cur.fetchall()]

    return ok({"total": len(rows), "records": rows})


# ----------------------------------------------------------------
# 采购订单 order
# ----------------------------------------------------------------
class OrderDetailItem(BaseModel):
    productId: int
    quantity: int = Field(ge=1)
    unitPrice: float
    subtotal: Optional[float] = None
    remark: Optional[str] = None


class OrderBody(BaseModel):
    orderNumber: Optional[str] = None
    totalAmount: Optional[float] = None
    status: Optional[int] = None
    orderTime: Optional[str] = None
    expectedDeliveryDate: Optional[str] = None
    actualDeliveryDate: Optional[str] = None
    createdBy: Optional[int] = None
    remark: Optional[str] = None
    orderDetail: List[OrderDetailItem] = []


def _gen_order_number(cur) -> str:
    """生成不重复单号：PO+yyyyMMdd+3 位随机数。"""
    for _ in range(20):
        num = "PO" + datetime.now().strftime("%Y%m%d") + str(random.randint(0, 999)).zfill(3)
        cur.execute("SELECT id FROM purchase_order WHERE order_number=%s", (num,))
        if not cur.fetchone():
            return num
    raise RuntimeError("生成订单编号失败（多次冲突）")


def _detail_subtotal(d: OrderDetailItem) -> float:
    """小计统一按 quantity*unitPrice 计算（order_detail.subtotal 为生成列，不可显式写入）。"""
    return round(d.quantity * d.unitPrice, 2)


def _backfill_supplier(cur, order_id: int) -> None:
    """回填订单主导供应商（与 db/migrations/001_p1_data_upgrade.sql 口径一致）：
    取该单有效明细中小计最大的那条明细对应 product 的供应商；明细为空/已删则置 NULL。"""
    cur.execute(
        "UPDATE purchase_order po "
        "LEFT JOIN ("
        "    SELECT od.order_id, p.supplier_id,"
        "           ROW_NUMBER() OVER (PARTITION BY od.order_id ORDER BY od.subtotal DESC, od.id) AS rn"
        "    FROM order_detail od JOIN product p ON p.id=od.product_id"
        "    WHERE od.order_id=%s AND od.deleted=0"
        ") t ON t.order_id=po.id AND t.rn=1 "
        "SET po.supplier_id=t.supplier_id, po.update_time=NOW() "
        "WHERE po.id=%s",
        (order_id, order_id),
    )


@app.post("/api/orders/create")
def order_create(body: OrderBody):
    """创建采购订单：自动计算小计/总额，事务写 purchase_order + order_detail。

    唯一性由数据库的唯一索引 `uk_order_number` 判定 —— 刻意**不做**「先 SELECT 查重再 INSERT」。
    那种写法是典型的 check-then-act 竞态：两个并发请求能同时通过检查，后一个撞唯一索引；
    而撞索引抛出的 IntegrityError 若未捕获，对外就是 HTTP 500 而不是可读提示。
    这里改为直接 INSERT、捕获 1062 翻译成友好消息 —— 让数据库当唯一性的裁判，
    应用层只负责把它的判决翻译成人话。
    """
    details = body.orderDetail or []
    if not details:
        return err("orderDetail 至少需要一项明细")
    order_number = body.orderNumber
    try:
        with Connection() as cur:
            invalid = [d.productId for d in details if not _fetch_product(cur, d.productId)]
            if invalid:
                return err("无效的 productId: " + str(invalid))
            subtotals = [_detail_subtotal(d) for d in details]
            total = round(body.totalAmount if body.totalAmount is not None else sum(subtotals), 2)
            order_number = order_number or _gen_order_number(cur)
            status = body.status if body.status is not None else 1
            order_time = _ts(body.orderTime) or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            created_by = body.createdBy if body.createdBy is not None else 1
            cur.execute(
                "INSERT INTO purchase_order (order_number, total_amount, status, order_time, "
                "expected_delivery_date, actual_delivery_date, created_by, remark, deleted, "
                "create_time, update_time) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,NOW(),NOW())",
                (order_number, total, status, order_time,
                 body.expectedDeliveryDate or None, body.actualDeliveryDate or None,
                 created_by, body.remark),
            )
            order_id = cur.lastrowid
            for d in details:
                cur.execute(
                    "INSERT INTO order_detail (order_id, product_id, quantity, unit_price, remark, "
                    "deleted, create_time, update_time) VALUES (%s,%s,%s,%s,%s,0,NOW(),NOW())",
                    (order_id, d.productId, d.quantity, d.unitPrice, d.remark),
                )
            _backfill_supplier(cur, order_id)
            order = _fetch_order(cur, order_id)
            detail_rows = _fetch_details(cur, order_id)
    except pymysql.err.IntegrityError as exc:
        if _is_duplicate(exc):
            return err("订单编号已存在: " + str(order_number))
        raise
    result = row_to_camel(order)
    result["orderDetail"] = [row_to_camel(r) for r in detail_rows]
    return ok(result)


@app.put("/api/orders/update/{order_id}")
def order_update(order_id: int, body: OrderBody):
    """更新采购订单：只更新传入字段；传 orderDetail 则整体替换明细并重算总额。

    改单号时的唯一性同样交给唯一索引判定（理由见 order_create 的 docstring）——
    原实现先 `SELECT ... AND id<>?` 查重，并发下会被绕过并抛出未捕获的 IntegrityError。
    """
    try:
        with Connection() as cur:
            order = _fetch_order(cur, order_id)
            if not order:
                return err("订单不存在: " + str(order_id), 404)
            sets, params = [], []
            if body.orderNumber is not None:
                # 不做 SELECT 预检：直接改，撞唯一索引时由外层捕获 1062
                sets.append("order_number=%s")
                params.append(body.orderNumber)
            if body.status is not None:
                if body.status not in (1, 2, 3, 4, 5):
                    return err("status 必须为 1-5")
                sets.append("status=%s")
                params.append(body.status)
            if body.totalAmount is not None:
                sets.append("total_amount=%s")
                params.append(body.totalAmount)
            if body.expectedDeliveryDate is not None:
                sets.append("expected_delivery_date=%s")
                params.append(body.expectedDeliveryDate)
            if body.actualDeliveryDate is not None:
                sets.append("actual_delivery_date=%s")
                params.append(body.actualDeliveryDate)
            if body.createdBy is not None:
                sets.append("created_by=%s")
                params.append(body.createdBy)
            if body.remark is not None:
                sets.append("remark=%s")
                params.append(body.remark)
            if body.orderTime is not None:
                sets.append("order_time=%s")
                params.append(_ts(body.orderTime))
            replace_details = bool(body.orderDetail)
            if replace_details:
                invalid = [d.productId for d in body.orderDetail if not _fetch_product(cur, d.productId)]
                if invalid:
                    return err("无效的 productId: " + str(invalid))
                new_total = sum(_detail_subtotal(d) for d in body.orderDetail)
                sets.append("total_amount=%s")
                params.append(round(new_total, 2))
            if not sets:
                return err("没有需要更新的字段")
            params.append(order_id)
            cur.execute(
                "UPDATE purchase_order SET " + ", ".join(sets) + ", update_time=NOW() WHERE id=%s",
                tuple(params),
            )
            if replace_details:
                cur.execute("UPDATE order_detail SET deleted=1, update_time=NOW() WHERE order_id=%s", (order_id,))
                for d in body.orderDetail:
                    cur.execute(
                        "INSERT INTO order_detail (order_id, product_id, quantity, unit_price, remark, "
                        "deleted, create_time, update_time) VALUES (%s,%s,%s,%s,%s,0,NOW(),NOW())",
                        (order_id, d.productId, d.quantity, d.unitPrice, d.remark),
                    )
                _backfill_supplier(cur, order_id)
            order = _fetch_order(cur, order_id)
            detail_rows = _fetch_details(cur, order_id)
    except pymysql.err.IntegrityError as exc:
        if _is_duplicate(exc):
            return err("订单编号已存在: " + str(body.orderNumber))
        raise
    result = row_to_camel(order)
    result["orderDetail"] = [row_to_camel(r) for r in detail_rows]
    return ok(result)


# ----------------------------------------------------------------
# 订单明细搜索（含商品详情与供应商）
# ----------------------------------------------------------------
@app.get("/api/orders/search-details")
def orders_search_details(product_name: Optional[str] = None,
                          start_date: Optional[str] = None, end_date: Optional[str] = None):
    """搜索采购订单明细：可选 productName / startDate / endDate（按 order_time）。"""
    where = ["od.deleted=0", "po.deleted=0"]
    params: list = []
    if product_name:
        where.append("p.name LIKE %s")
        params.append("%" + product_name + "%")
    if start_date:
        where.append("po.order_time >= %s")
        params.append(start_date + " 00:00:00")
    if end_date:
        where.append("po.order_time <= %s")
        params.append(end_date + " 23:59:59")
    cond = " AND ".join(where)
    with Connection() as cur:
        cur.execute(
            "SELECT od.id, od.order_id, od.product_id, od.quantity, od.unit_price, od.subtotal, od.remark, "
            "po.order_number, po.order_time, po.status, po.total_amount, "
            "po.expected_delivery_date, po.actual_delivery_date, "
            "p.id AS p_id, p.product_code, p.name AS product_name, p.model, p.specification, p.unit, "
            "p.purchase_price, p.suggested_retail_price, p.category, p.supplier_id, "
            "s.name AS supplier_name, s.credit_rating, s.status AS supplier_status "
            "FROM order_detail od "
            "JOIN purchase_order po ON po.id=od.order_id AND po.deleted=0 "
            "JOIN product p ON p.id=od.product_id AND p.deleted=0 "
            "LEFT JOIN supplier s ON s.id=p.supplier_id "
            "WHERE " + cond + " ORDER BY po.order_time DESC, od.id LIMIT 200",
            tuple(params),
        )
        raw = cur.fetchall()
    data = []
    for r in raw:
        item = {
            "id": r["id"], "orderId": r["order_id"], "productId": r["product_id"],
            "quantity": r["quantity"], "unitPrice": float(r["unit_price"]),
            "subtotal": float(r["subtotal"]), "remark": r["remark"],
        }
        item["partDetail"] = {
            "id": r["p_id"], "productCode": r["product_code"], "name": r["product_name"],
            "model": r["model"], "specification": r["specification"], "unit": r["unit"],
            "purchasePrice": float(r["purchase_price"]) if r["purchase_price"] else None,
            "suggestedRetailPrice": float(r["suggested_retail_price"]) if r["suggested_retail_price"] else None,
            "category": r["category"], "supplierId": r["supplier_id"],
        }
        item["supplier"] = {
            "id": r["supplier_id"], "name": r["supplier_name"],
            "creditRating": r["credit_rating"], "status": r["supplier_status"],
        } if r["supplier_id"] else None
        item["order"] = {
            "id": r["order_id"], "orderNumber": r["order_number"],
            "orderTime": r["order_time"], "status": r["status"],
            "totalAmount": float(r["total_amount"]),
            "expectedDeliveryDate": r["expected_delivery_date"],
            "actualDeliveryDate": r["actual_delivery_date"],
        }
        data.append(item)
    return ok(data)


# ================================================================
# 主数据写入（供应商 / 客户 / 商品）
# ================================================================
# 设计要点（与订单写入的差异，都是有原因的）：
#
# 1) 唯一性交给数据库，不做「先 SELECT 再 INSERT」。
#    三张表都有唯一索引（uk_supplier_code / uk_customer_code / uk_product_code），
#    所以写入时直接捕获 MySQL 1062（Duplicate entry）转成可读错误即可。
#    对比 order_create 的写法（先 SELECT order_number 查重再 INSERT）——那是**竞态**：
#    两个并发请求可能都查到「不存在」，然后都插入成功（或一个报 1062 而非友好提示）。
#    唯一索引是数据库层的最后一道闸，应用层预检查只是「提前给友好提示」，不能替代它。
#
# 2) 引用完整性必须在这一层补 —— 因为**数据库里一个外键约束都没有**。
#    product.supplier_id 可以指向不存在的供应商，MySQL 不会拦。
#    所以新增商品时必须先校验 supplierId 真实存在且未删除。
#
# 3) 编码能否自动生成，取决于它有没有业务含义：
#    - supplier：SUP001 / SUP002 ...  纯序号 → 可自动生成
#    - customer：CUS00001 / CUS00002  纯序号 → 可自动生成
#    - product ：HFS-400-01（澜溪 400ml）、MG-6-01（梦享）→ **有业务含义，必须显式传入**。
#      自动生成会产出无意义编码，破坏主数据的可读性。
# ================================================================


def _is_duplicate(exc: Exception) -> bool:
    """判断是否为 MySQL 1062（Duplicate entry，唯一索引冲突）。"""
    return bool(exc.args) and exc.args[0] == 1062


def _gen_master_code(cur, table: str, column: str, prefix: str, width: int) -> str:
    """生成下一个序号编码（如 SUP001 / CUS00001），沿用库里已有的编码风格。

    table/column 只来自本模块内的字面量常量、不接受外部输入，故此处可安全拼接；
    真正的并发撞号由唯一索引兜底（调用方捕获 1062 后重试）。
    """
    cur.execute(
        "SELECT MAX(CAST(SUBSTRING(`%s`, %d) AS UNSIGNED)) AS mx FROM `%s` WHERE `%s` LIKE %%s"
        % (column, len(prefix) + 1, table, column),
        (prefix + "%",),
    )
    row = cur.fetchone()
    return prefix + str((row["mx"] or 0) + 1).zfill(width)


class SupplierCreateBody(BaseModel):
    name: str
    supplierCode: Optional[str] = None
    contactPerson: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    creditRating: Optional[str] = None      # A/B/C/D
    status: Optional[int] = None            # 1=合作中, 0=已停用


class CustomerCreateBody(BaseModel):
    name: str
    customerCode: Optional[str] = None
    contactPerson: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    customerType: Optional[int] = None      # 1=KA商超, 2=经销商, 3=电商
    discountLevel: Optional[int] = None     # 1/2/3 级客户


class ProductCreateBody(BaseModel):
    productCode: str                        # 必填：含品牌与规格含义，不支持自动生成
    name: str
    purchasePrice: float
    model: Optional[str] = None
    specification: Optional[str] = None
    unit: Optional[str] = None
    suggestedRetailPrice: Optional[float] = None
    stockWarningValue: Optional[int] = None
    supplierId: Optional[int] = None
    category: Optional[str] = None
    description: Optional[str] = None


@app.post("/api/suppliers/create")
def supplier_create(body: SupplierCreateBody):
    """新增供应商主数据。编码不传则按 SUP+3 位序号自动生成。"""
    if not body.name or not body.name.strip():
        return err("name 必填")
    if body.creditRating is not None and body.creditRating not in ("A", "B", "C", "D"):
        return err("creditRating 只能是 A/B/C/D")
    if body.status is not None and body.status not in (0, 1):
        return err("status 只能是 0(已停用) / 1(合作中)")

    for _ in range(5):
        try:
            with Connection() as cur:
                code = body.supplierCode or _gen_master_code(cur, "supplier", "supplier_code", "SUP", 3)
                cur.execute(
                    "INSERT INTO supplier (supplier_code, name, contact_person, phone, email, "
                    "address, credit_rating, status, deleted, create_time, update_time) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,NOW(),NOW())",
                    (code, body.name.strip(), body.contactPerson, body.phone, body.email,
                     body.address, body.creditRating,
                     body.status if body.status is not None else 1),
                )
                new_id = cur.lastrowid
                cur.execute("SELECT * FROM supplier WHERE id=%s", (new_id,))
                row = cur.fetchone()
            return ok(row_to_camel(row))
        except pymysql.err.IntegrityError as exc:
            if not _is_duplicate(exc):
                raise
            if body.supplierCode:
                return err("供应商编码已存在: " + body.supplierCode)
            # 自动编码撞号（并发）→ 重算一次再试
            continue
    return err("自动生成供应商编码多次冲突，请显式传入 supplierCode")


@app.post("/api/customers/create")
def customer_create(body: CustomerCreateBody):
    """新增客户主数据。编码不传则按 CUS+5 位序号自动生成。"""
    if not body.name or not body.name.strip():
        return err("name 必填")
    if body.customerType is not None and body.customerType not in (1, 2, 3):
        return err("customerType 只能是 1(KA商超) / 2(经销商) / 3(电商)")
    if body.discountLevel is not None and body.discountLevel not in (1, 2, 3):
        return err("discountLevel 只能是 1/2/3 级客户")

    for _ in range(5):
        try:
            with Connection() as cur:
                code = body.customerCode or _gen_master_code(cur, "customer", "customer_code", "CUS", 5)
                cur.execute(
                    "INSERT INTO customer (customer_code, name, contact_person, phone, email, "
                    "address, customer_type, discount_level, registered_time, deleted, "
                    "create_time, update_time) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),0,NOW(),NOW())",
                    (code, body.name.strip(), body.contactPerson, body.phone, body.email,
                     body.address, body.customerType, body.discountLevel),
                )
                new_id = cur.lastrowid
                cur.execute("SELECT * FROM customer WHERE id=%s", (new_id,))
                row = cur.fetchone()
            return ok(row_to_camel(row))
        except pymysql.err.IntegrityError as exc:
            if not _is_duplicate(exc):
                raise
            if body.customerCode:
                return err("客户编码已存在: " + body.customerCode)
            continue
    return err("自动生成客户编码多次冲突，请显式传入 customerCode")


@app.post("/api/products/create")
def product_create(body: ProductCreateBody):
    """新增商品主数据。

    productCode 必须显式传入（有品牌/规格含义，不自动生成）；
    supplierId 若传入则必须真实存在 —— 数据库无外键，只能在这一层校验。
    """
    if not body.productCode or not body.productCode.strip():
        return err("productCode 必填（商品编码含品牌与规格含义，不支持自动生成）")
    if not body.name or not body.name.strip():
        return err("name 必填")
    if body.purchasePrice is None or body.purchasePrice < 0:
        return err("purchasePrice 必填且不能为负")
    if body.suggestedRetailPrice is not None and body.suggestedRetailPrice < 0:
        return err("suggestedRetailPrice 不能为负")
    if body.stockWarningValue is not None and body.stockWarningValue < 0:
        return err("stockWarningValue 不能为负")

    try:
        with Connection() as cur:
            if body.supplierId is not None:
                cur.execute("SELECT id FROM supplier WHERE id=%s AND deleted=0", (body.supplierId,))
                if not cur.fetchone():
                    return err("supplierId 不存在: " + str(body.supplierId))
            cur.execute(
                "INSERT INTO product (product_code, name, model, specification, unit, "
                "purchase_price, suggested_retail_price, stock_warning_value, supplier_id, "
                "category, description, deleted, create_time, update_time) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,NOW(),NOW())",
                (body.productCode.strip(), body.name.strip(), body.model, body.specification,
                 body.unit, body.purchasePrice, body.suggestedRetailPrice,
                 body.stockWarningValue, body.supplierId, body.category, body.description),
            )
            new_id = cur.lastrowid
            cur.execute("SELECT * FROM product WHERE id=%s", (new_id,))
            row = cur.fetchone()
        return ok(row_to_camel(row))
    except pymysql.err.IntegrityError as exc:
        if _is_duplicate(exc):
            return err("商品编码已存在: " + body.productCode)
        raise