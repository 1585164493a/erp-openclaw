"""写操作补偿函数（回退用）。

与 `tool_rollback.ToolRollbackMiddleware` 配套：
- 中间件负责"什么时候撤、撤哪些"（逆序）；
- 本文件负责"每一种写操作具体怎么撤"。

后端能力（2026-11-12 核对 src/erp_api/main.py）：
- `POST /api/orders/create` —— **事务**写主单+明细，内部失败会整体回滚，不会留半单；
- `PUT  /api/orders/update/{id}` —— 支持 status 1~5，**5 = 已取消** → 这是撤销订单的唯一手段；
- `POST /api/suppliers|/customers|/products/create` —— **没有删除/停用接口**，
  所以建档类只能"回报人工处理"，不假装撤销（这也是用户定的规则：失败要明说）。

三条已确认规则：
1. 补偿**不走人工审批**（方向是收紧）；
2. 补偿失败**不静默重试**，记进"未能撤销，需人工处理"；
3. 自动撤销审批过的写操作可以接受，但必须在回答里**明确告知用户**。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

_API_BASE = os.getenv("ERP_API_BASE_URL", "http://localhost:8080/api").rstrip("/")


def _api() -> httpx.Client:
    return httpx.Client(base_url=_API_BASE, timeout=15)


def _payload(result: Any) -> dict:
    """从工具返回值里抠出 JSON（MCP 工具返回 ToolMessage 或 dict 或字符串）。"""
    content = getattr(result, "content", result)
    if isinstance(content, dict):
        return content
    text = content if isinstance(content, str) else str(content)
    text = text.strip()
    for candidate in (text, ):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data.get("data", data) if isinstance(data.get("data"), dict) else data
        except Exception:  # noqa: BLE001
            pass
    # 兜底：从文本里找 id / 编号（兼容 "id":42 和 id: 42 两种写法）
    out: dict = {}
    for key in ("id", "orderId", "supplierId", "customerId", "productId"):
        m = re.search(rf'"?{key}"?\s*[:：=]\s*(\d+)', text, re.IGNORECASE)
        if m:
            out[key] = int(m.group(1))
    for key in ("orderNumber", "supplierCode", "customerCode", "productCode"):
        m = re.search(rf'"?{key}"?\s*[:：=]\s*["\']?([A-Za-z0-9\-_]+)', text)
        if m:
            out[key] = m.group(1)
    return out


# ---------------------------------------------------------------- 订单
def compensate_order_create(args: dict, result: Any, snapshot: Any = None) -> str:
    """撤销新建订单：把 status 置 5（已取消）。

    为什么不删：后端没有删除接口；status=5 是业务上"已取消"的唯一表达，
    且保留审计痕迹（谁在什么时候取消的）。
    """
    data = _payload(result)
    order_id = data.get("id") or data.get("orderId") or data.get("purchaseOrderId")
    order_no = data.get("orderNumber") or data.get("order_no") or ""
    if not order_id:
        raise RuntimeError("返回里找不到订单 id，无法撤销——请人工核对是否已产生订单")

    with _api() as c:
        resp = c.put(f"/orders/update/{order_id}", json={"status": 5})
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code >= 400 or (isinstance(body, dict) and body.get("code") not in (None, 200)):
        raise RuntimeError(f"取消订单失败：HTTP {resp.status_code} {str(body)[:120]}")
    return f"订单 {order_no or order_id} 已置为「已取消」(status=5)，保留审计痕迹"


def snapshot_order(args: dict) -> dict:
    """改单前快照：用只读 SQL 读当前值（补偿时用来还原）。"""
    import pymysql

    order_id = args.get("order_id") or args.get("orderId")
    if not order_id:
        return {}
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST"), port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"), password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE", "motorparts_db"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_number, total_amount, status, expected_delivery_date, "
                "actual_delivery_date, remark FROM purchase_order WHERE id=%s AND deleted=0",
                (order_id,),
            )
            row = cur.fetchone() or {}
            cur.execute(
                "SELECT product_id, quantity, unit_price FROM order_detail "
                "WHERE order_id=%s AND deleted=0", (order_id,),
            )
            row["details"] = cur.fetchall() or []
            return row
    finally:
        conn.close()


def compensate_order_update(args: dict, result: Any, snapshot: Any = None) -> str:
    """撤销改单：把快照里的旧值写回去（没有快照就明说无法自动还原）。"""
    order_id = args.get("order_id") or args.get("orderId")
    if not order_id:
        raise RuntimeError("缺少 order_id")
    if not snapshot:
        raise RuntimeError("没有改单前快照，无法自动还原——请人工核对")

    body: dict = {}
    if snapshot.get("order_number"):
        body["orderNumber"] = snapshot["order_number"]
    if snapshot.get("total_amount") is not None:
        body["totalAmount"] = float(snapshot["total_amount"])
    if snapshot.get("status") is not None:
        body["status"] = int(snapshot["status"])
    if snapshot.get("expected_delivery_date") is not None:
        body["expectedDeliveryDate"] = str(snapshot["expected_delivery_date"])
    if snapshot.get("actual_delivery_date") is not None:
        body["actualDeliveryDate"] = str(snapshot["actual_delivery_date"])
    if snapshot.get("remark") is not None:
        body["remark"] = snapshot["remark"]
    if snapshot.get("details"):
        body["orderDetail"] = [
            {"productId": d["product_id"], "quantity": d["quantity"],
             "unitPrice": float(d["unit_price"])}
            for d in snapshot["details"]
        ]

    if not body:
        raise RuntimeError("快照为空，无法还原")

    with _api() as c:
        resp = c.put(f"/orders/update/{order_id}", json=body)
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("code") not in (None, 200)):
        raise RuntimeError(f"还原订单失败：HTTP {resp.status_code} {str(payload)[:120]}")
    return f"订单 {order_id} 已还原为改单前的值（{len(body)} 个字段）"


# ---------------------------------------------------------------- 主数据建档
def _master_data_compensator(kind: str, code_key: str, id_key: str):
    """建档类没有删除接口 → 只回报人工处理，不假装撤销。"""

    def _fn(args: dict, result: Any, snapshot: Any = None) -> str:
        data = _payload(result)
        ident = data.get(code_key) or ""
        rid = data.get(id_key) or data.get("id") or ""
        tag = f"{kind} {ident or rid or '(未取到编号)'}"
        raise RuntimeError(
            f"{tag} 已写入，但系统没有{kind}删除/停用接口，无法自动撤销——需人工停用"
        )

    return _fn


# ---------------------------------------------------------------- 注册
def register_default_compensators() -> None:
    """进程启动时调用一次：把默认补偿函数登记进注册表。"""
    from agent.middlewares.tool_rollback import register_compensator, register_snapshotter

    register_compensator("order_create", compensate_order_create)
    register_compensator("order_update", compensate_order_update)
    register_snapshotter("order_update", snapshot_order)

    register_compensator(
        "supplier_create", _master_data_compensator("供应商", "supplierCode", "supplierId")
    )
    register_compensator(
        "customer_create", _master_data_compensator("客户", "customerCode", "customerId")
    )
    register_compensator(
        "product_create", _master_data_compensator("商品", "productCode", "productId")
    )
