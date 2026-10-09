"""生成 mock 供应商报价页 —— 数据直接取自 motorparts_db 的 product_supplier 表。

为什么需要这个：
  `supplier-price-urls` 技能的做法是「商品名 + 供应商名 → 报价页 URL → 爬虫抓取」，
  原实现指向的是 127.0.0.1 上的汽车零部件模拟页（火花塞），
  而那些页面**在 VM 上根本不存在**（80 端口无服务），整条链路是死的。

  本脚本按库里的真实商品与真实候选报价，生成对应的 FMCG 报价页，
  再把这些页面放到 VM 的静态服务上（见 README.md），链路即可真正跑通。

用法（在项目根目录）:
    python mock_site/generate.py

输出:
    mock_site/quotes/<商品编码>-<供应商简称>.html
"""
from __future__ import annotations

import html
import os
import sys
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
OUT_DIR = ROOT / "quotes"
YAML_OUT = PROJECT / "src" / "skills" / "procurement" / "supplier-price-urls" / "data" / "url_mapping.yaml"

# 报价页由 VM 上的静态服务提供（见 README.md）
MOCK_BASE_URL = os.getenv("MOCK_BASE_URL", "http://127.0.0.1")

# 与 src/erp_api/db.py 保持一致的连接参数（支持环境变量覆盖）
MYSQL = dict(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.getenv("MYSQL_PASSWORD", "123456"),
    database=os.getenv("MYSQL_DB", "motorparts_db"),
    charset="utf8mb4",
)

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{supplier} - {product} 报价</title>
</head>
<body>
<h1>{supplier} 报价单</h1>

<table border="1" cellpadding="6">
  <tr><th>商品编码</th><td>{code}</td></tr>
  <tr><th>商品名称</th><td>{product}</td></tr>
  <tr><th>品牌</th><td>{brand}</td></tr>
  <tr><th>型号</th><td>{model}</td></tr>
  <tr><th>包装规格</th><td>{spec}</td></tr>
  <tr><th>计量单位</th><td>{unit}</td></tr>
  <tr><th>品类</th><td>{category}</td></tr>
  <tr><th>供应商</th><td>{supplier}</td></tr>
  <tr><th>供应商信用评级</th><td>{rating}</td></tr>
  <tr><th>是否主供应商</th><td>{primary}</td></tr>
  <tr><th>当前报价</th><td>{price} 元 / {unit}（含税）</td></tr>
  <tr><th>最小起订量</th><td>{moq} {unit}</td></tr>
  <tr><th>交货周期</th><td>{lead} 天</td></tr>
  <tr><th>建议零售价</th><td>{retail} 元 / {unit}</td></tr>
  <tr><th>报价有效期</th><td>{valid_from} 起</td></tr>
</table>

<p>备注：本页为供应链演示用模拟报价单，价格数据与 ERP 候选报价表一致。</p>
</body>
</html>
"""


def slug(name: str) -> str:
    """供应商名 → URL 友好的短名。"""
    for sep in ("（", "(", " "):
        name = name.split(sep)[0]
    return name


def brand_of(product_code: str) -> str:
    """从商品编码前缀推品牌（与 master-data-maintain-sop 的缩写表一致）。"""
    prefix = product_code.split("-")[0]
    return {
        "HFS": "澜溪", "CLR": "朗风", "LUX": "雅致", "DV": "悦芙", "DVE": "悦芙",
        "VS": "润舒", "PD": "净妍", "ZH": "中华", "JN": "洁诺", "LFB": "卫宝",
        "OM": "洁力", "JF": "柔洁", "WM": "净威先生", "WLX": "雪悦", "MG": "梦享",
        "CAND": "甜心多", "KL": "家悦", "KN": "家悦", "LT": "茶悦",
    }.get(prefix, prefix)


def main() -> int:
    conn = pymysql.connect(**MYSQL)
    cur = conn.cursor()
    cur.execute(
        """SELECT p.product_code, p.name, p.model, p.specification, p.unit,
                  p.suggested_retail_price, p.category,
                  s.name, s.credit_rating,
                  ps.quote_price, ps.is_primary, ps.lead_days, ps.moq, ps.valid_from
             FROM product_supplier ps
             JOIN product p  ON p.id = ps.product_id
             JOIN supplier s ON s.id = ps.supplier_id
            WHERE ps.deleted = 0 AND p.deleted = 0 AND s.deleted = 0
            ORDER BY p.id, ps.quote_price"""
    )
    rows = cur.fetchall()
    conn.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    entries = []
    for (code, name, model, spec, unit, retail, category,
         supplier, rating, price, primary, lead, moq, valid_from) in rows:
        fname = "%s-%s.html" % (code, slug(supplier))
        (OUT_DIR / fname).write_text(
            PAGE.format(
                code=code, product=name, brand=brand_of(code), model=model or "-",
                spec=spec or "-", unit=unit or "-", category=category or "-",
                supplier=supplier, rating=rating or "-",
                primary="是" if primary else "否",
                price=price, moq=moq, lead=lead,
                retail=retail if retail is not None else "-",
                valid_from=valid_from or "-",
            ),
            encoding="utf-8",
        )
        written.append(fname)
        entries.append((name, supplier, code, fname))

    # 同步产出技能用的映射表 —— 与页面同一数据源，杜绝两者不一致。
    # 刻意**不写价格**：这一环的目的就是让 Agent 去页面上抓最新报价，
    # 把价格抄进映射表等于把演示要验证的东西提前给了答案。
    lines = [
        "# 采购报价网址映射表",
        "#",
        "# ⚠️ 本文件由 mock_site/generate.py 从 motorparts_db 的 product_supplier 表生成，请勿手改。",
        "#    改数据请改库（或迁移脚本），然后重跑：python mock_site/generate.py",
        "#",
        "# 页面由 VM 上的静态服务提供（部署方式见 mock_site/README.md）。",
        "# 用途：Agent 按「商品名 + 供应商名」查到报价页 URL → 交给 web-scraper 抓取最新报价。",
        "# 价格【不在这里】—— 必须去页面上抓，这正是本技能存在的意义。",
        "",
        "mappings:",
    ]
    for name, supplier, code, fname in entries:
        lines.append('  - product_name: "%s"' % name)
        lines.append('    supplier: "%s"' % supplier)
        lines.append('    product_code: "%s"' % code)
        lines.append('    url: "%s/quotes/%s"' % (MOCK_BASE_URL.rstrip("/"), fname))
    YAML_OUT.parent.mkdir(parents=True, exist_ok=True)
    YAML_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("已生成 %d 个报价页 → %s" % (len(written), OUT_DIR))
    print("已同步映射表     → %s" % YAML_OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
