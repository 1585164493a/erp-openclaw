"""扩展示例数据：把只有 2026-09 的样本扩到今天，并注入"陷阱样本"。

为什么需要：
1. **时间断层**：sales_daily 只到 2026-09-21，问"最近销量"会落空；
   purchase_order / logistics 停在 2025-12，跨年断层。
2. **陷阱样本缺失**：现在 13 张表的 `deleted` **全是 0**、supplier 全是 status=1，
   "漏掉软删除过滤"、"停用供应商"这类错误在当前数据上根本体现不出来——
   评测就会漏判成"都对"。

用法：
    python scripts/expand_mock_data.py --dry-run     # 只看会写多少
    python scripts/expand_mock_data.py               # 真正写入
    python scripts/expand_mock_data.py --until 2026-11-12
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import date, datetime, timedelta

import pymysql
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

random.seed(20261112)          # 固定种子，重复跑结果一致，评测才可比


def conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST"), port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"), password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE", "motorparts_db"),
        charset="utf8mb4", autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


# ---------------------------------------------------------------- 销量
def expand_sales(c, until: date, dry: bool) -> int:
    """按最后一天的 (product, warehouse, channel) 组合，逐日复制到 until。"""
    cur = c.cursor()
    cur.execute("select max(sales_date) m, count(*) n from sales_daily")
    row = cur.fetchone()
    last, total = row["m"], row["n"]
    if last is None:
        print("  sales_daily 为空，跳过")
        return 0
    if last >= until:
        print(f"  sales_daily 已到 {last}，无需扩展")
        return 0

    print(f"  sales_daily: {last}（{total} 行）→ {until}，逐日复制")

    # 取最后一天的模板行
    cur.execute(
        "select product_id, warehouse_location, channel, quantity, amount "
        "from sales_daily where sales_date=%s", (last,)
    )
    template = cur.fetchall()

    inserted = 0
    day = last + timedelta(days=1)
    while day <= until:
        rows = []
        for r in template:
            # ±18% 波动；周一到周五略高，周末略低（让"趋势/异常"有真实形态）
            factor = 1.0 + random.uniform(-0.18, 0.18)
            if day.weekday() >= 5:
                factor *= 0.82
            qty = max(1, int(round(r["quantity"] * factor)))
            amt = round(float(r["amount"]) * factor, 2)
            rows.append((r["product_id"], r["warehouse_location"], r["channel"],
                         day, qty, amt, 0))
        if not dry:
            cur.executemany(
                "insert into sales_daily"
                "(product_id, warehouse_location, channel, sales_date, quantity, amount, deleted)"
                " values (%s,%s,%s,%s,%s,%s,%s)", rows
            )
        inserted += len(rows)
        day += timedelta(days=1)
    print(f"    将插入 {inserted} 行")
    return inserted


# ---------------------------------------------------------------- 销售目标
def expand_targets(c, until: date, dry: bool) -> int:
    cur = c.cursor()
    cur.execute("select max(target_month) m from sales_target")
    last = cur.fetchone()["m"]
    if not last:
        return 0
    cur.execute("select * from sales_target where target_month=%s", (last,))
    base = cur.fetchall()

    months = []
    y, m = int(last[:4]), int(last[5:7])
    while True:
        m += 1
        if m > 12:
            y, m = y + 1, 1
        key = f"{y:04d}-{m:02d}"
        if key > until.strftime("%Y-%m"):
            break
        months.append(key)

    if not months:
        print(f"  sales_target 已到 {last}，无需扩展")
        return 0

    step = 1
    inserted = 0
    for key in months:
        step += 1
        rows = []
        for r in base:
            # 目标逐月上调 4%，比销量略快，制造"达成率不足"的真实场景
            amt = round(float(r["target_amount"]) * (1.04 ** step), 2)
            rows.append((key, r["dimension"], r["dimension_value"], amt,
                         r.get("owner"), r.get("remark"), 0))
        if not dry:
            cur.executemany(
                "insert into sales_target"
                "(target_month, dimension, dimension_value, target_amount, owner, remark, deleted)"
                " values (%s,%s,%s,%s,%s,%s,%s)", rows
            )
        inserted += len(rows)
    print(f"  sales_target: {last} → {months[-1]}，将插入 {inserted} 行")
    return inserted


# ---------------------------------------------------------------- 采购订单
def expand_orders(c, until: date, dry: bool) -> int:
    cur = c.cursor()
    cur.execute("select max(order_time) m from purchase_order")
    last = cur.fetchone()["m"]
    if not last or last.date() >= until:
        print("  purchase_order 已覆盖到目标日期")
        return 0

    cur.execute("select id from supplier where deleted=0")
    suppliers = [r["id"] for r in cur.fetchall()]
    cur.execute("select id from product where deleted=0")
    products = cur.fetchall()
    if not suppliers or not products:
        return 0

    orders, details, logistics = [], [], []
    seq = 0
    day = last.date() + timedelta(days=1)
    while day <= until:
        # 每 10 天一批订单，避免数据爆炸
        if day.day % 10 == 1:
            for _ in range(3):
                seq += 1
                no = f"PO{day.strftime('%Y%m%d')}{seq:04d}"
                sup = random.choice(suppliers)
                status = random.choice([1, 2, 3, 4, 4, 4, 5])
                otime = datetime.combine(day, datetime.min.time()) + timedelta(
                    hours=random.randint(9, 17), minutes=random.randint(0, 59))
                picked = random.sample(products, k=random.randint(1, 3))
                amount = 0.0
                for p in picked:
                    qty = random.randint(50, 600)
                    price = round(random.uniform(8, 60), 2)
                    subtotal = round(qty * price, 2)
                    amount += subtotal
                    details.append((no, p["id"], qty, price, subtotal))
                orders.append((no, round(amount, 2), status, otime, sup))
                if status in (3, 4):
                    logistics.append((no, random.choice(["顺丰", "京东物流", "德邦", "中通"]),
                                      otime + timedelta(days=random.randint(1, 3)),
                                      random.choice([1, 2, 3])))
        day += timedelta(days=1)

    print(f"  purchase_order: 将插入 {len(orders)} 单 / {len(details)} 明细 / {len(logistics)} 物流")
    if dry or not orders:
        return len(orders)

    # 先插订单拿 id，再补明细与物流
    id_by_no = {}
    for no, amt, status, otime, sup in orders:
        cur.execute(
            "insert into purchase_order"
            "(order_number, total_amount, status, order_time, supplier_id, created_by, deleted)"
            " values (%s,%s,%s,%s,%s,1,0)", (no, amt, status, otime, sup))
        id_by_no[no] = cur.lastrowid
    for no, pid, qty, price, sub in details:
        # subtotal 是生成列（quantity*unit_price），不能显式插入
        cur.execute(
            "insert into order_detail(order_id, product_id, quantity, unit_price, deleted)"
            " values (%s,%s,%s,%s,0)", (id_by_no[no], pid, qty, price))
    for no, company, ship, status in logistics:
        cur.execute(
            "insert into logistics(order_id, logistics_company, tracking_number, ship_time, status, deleted)"
            " values (%s,%s,%s,%s,%s,0)",
            (id_by_no[no], company, f"SF{random.randint(10**9, 10**10)}", ship, status))
    return len(orders)


# ---------------------------------------------------------------- 陷阱样本
def inject_traps(c, dry: bool) -> None:
    """注入让"错误能被看见"的样本。已注入过就跳过。"""
    cur = c.cursor()

    cur.execute("select count(*) n from supplier where deleted=1")
    if cur.fetchone()["n"] == 0:
        # 2 家软删除（查"有哪些供应商"时必须被过滤掉）
        cur.execute("select id from supplier where deleted=0 order by id limit 2")
        ids = [r["id"] for r in cur.fetchall()]
        for i in ids:
            if not dry:
                cur.execute("update supplier set deleted=1 where id=%s", (i,))
        print(f"  注入软删除供应商: {ids}")

    cur.execute("select count(*) n from supplier where status=0")
    if cur.fetchone()["n"] == 0:
        # 2 家停用（状态过滤陷阱：停用是否算"现有供应商"要讲清口径）
        cur.execute("select id from supplier where deleted=0 order by id desc limit 2")
        ids = [r["id"] for r in cur.fetchall()]
        for i in ids:
            if not dry:
                cur.execute("update supplier set status=0 where id=%s", (i,))
        print(f"  注入停用供应商: {ids}")

    cur.execute("select count(*) n from inventory where current_quantity=0 and deleted=0")
    if cur.fetchone()["n"] == 0:
        # 2 条零库存（缺货 vs 低库存，口径不同）
        cur.execute("select id from inventory where deleted=0 order by id limit 2")
        for r in cur.fetchall():
            if not dry:
                cur.execute("update inventory set current_quantity=0 where id=%s", (r["id"],))
        print("  注入 2 条零库存")

    # 重名供应商（实体消歧陷阱）：加一条同名的分部
    cur.execute("select name from supplier where deleted=0 limit 1")
    row = cur.fetchone()
    if row and not dry:
        dup = row["name"] + "（分部）"
        cur.execute("select count(*) n from supplier where name=%s", (dup,))
        if cur.fetchone()["n"] == 0:
            cur.execute(
                "insert into supplier(supplier_code, name, contact_person, phone, credit_rating, status, deleted)"
                " values (%s,%s,%s,%s,%s,1,0)",
                ("SUP-DUP-01", dup, "测试联系人", "13800000000", "B"))
            print(f"  注入同名分部: {dup}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="2026-11-12", help="扩展到哪一天（YYYY-MM-DD）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    until = date.fromisoformat(args.until)
    print(f"目标日期 = {until}，dry_run={args.dry_run}")

    c = conn()
    try:
        print("\n[1/4] 扩展销量")
        expand_sales(c, until, args.dry_run)
        print("\n[2/4] 扩展销售目标")
        expand_targets(c, until, args.dry_run)
        print("\n[3/4] 扩展采购订单")
        expand_orders(c, until, args.dry_run)
        print("\n[4/4] 注入陷阱样本")
        inject_traps(c, args.dry_run)

        if args.dry_run:
            c.rollback()
            print("\n（dry-run：已回滚，没有写入任何数据）")
        else:
            c.commit()
            print("\n[OK] 已提交")
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
