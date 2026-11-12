"""给 T10 补数据：让"低库存商品 + C 级供应商"这个组合真实存在。

背景：原数据里低库存的 8 个商品，供应商评级只有 A/B，
所以 T10（低库存且供应商 C/D）的标准 SQL 查不到任何结果——题目无解。
这个脚本幂等地补一个 C 级供应商并挂到某个低库存商品上。

用法：python scripts/seed_trap_extras.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys

import pymysql
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

C_SUPPLIER_CODE = "SUP-C-TRAP"
C_SUPPLIER_NAME = "信达工业材料（测试用 C 级）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST"), port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"), password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE", "motorparts_db"),
        charset="utf8mb4", autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )
    cur = conn.cursor()
    try:
        # 1) 找一个低库存商品
        cur.execute("""
            select i.product_id, p.name
            from inventory i join product p on p.id = i.product_id
            where i.deleted = 0 and p.deleted = 0 and i.current_quantity < i.safety_stock
            order by i.product_id limit 1
        """)
        row = cur.fetchone()
        if not row:
            print("没有低库存商品，先跑 expand_mock_data.py")
            return 1
        pid, pname = row["product_id"], row["name"]
        print(f"目标商品：{pid} {pname}")

        # 2) 确保存在一个 C 级供应商
        cur.execute("select id from supplier where supplier_code = %s", (C_SUPPLIER_CODE,))
        s = cur.fetchone()
        if s:
            sid = s["id"]
            print(f"C 级供应商已存在：id={sid}")
        else:
            if args.dry_run:
                print(f"[dry-run] 将新建 C 级供应商：{C_SUPPLIER_NAME}")
                sid = None
            else:
                cur.execute(
                    "insert into supplier(supplier_code, name, contact_person, phone,"
                    " credit_rating, status, deleted) values (%s,%s,%s,%s,'C',1,0)",
                    (C_SUPPLIER_CODE, C_SUPPLIER_NAME, "赵六", "13900000000"),
                )
                sid = cur.lastrowid
                print(f"新建 C 级供应商：id={sid}")

        # 3) 挂到该商品（幂等）
        if sid is not None:
            cur.execute(
                "select 1 from product_supplier where product_id=%s and supplier_id=%s",
                (pid, sid),
            )
            if cur.fetchone():
                print("关联已存在，跳过")
            elif args.dry_run:
                print("[dry-run] 将建立 product_supplier 关联")
            else:
                # quote_price / valid_from 是 NOT NULL 且无默认值，必须显式给
                cur.execute(
                    "insert into product_supplier"
                    "(product_id, supplier_id, quote_price, is_primary, valid_from, deleted)"
                    " values (%s, %s, %s, 0, curdate(), 0)",
                    (pid, sid, 25.50),
                )
                print("已建立 product_supplier 关联")

        if args.dry_run:
            conn.rollback()
            print("（dry-run：已回滚）")
        else:
            conn.commit()
            print("已提交")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
