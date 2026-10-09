# mock_site —— 供应商报价页（演示用）

## 这是什么

`procurement-analyst` 的比价流程里有一环：**去外部抓取供应商报价**。

```
商品名 + 供应商名  →  url_mapping.yaml 查到报价页 URL  →  web-scraper 抓页面  →  提取最新报价
```

这一环需要一个「可访问、内容稳定、含价格」的页面。本项目用**自建的模拟报价页**承担这个角色
（真实电商页面有反爬、内容易变，不适合做演示）。

> **历史背景**：原实现指向的是 VM 上的汽车零部件模拟页（`spark-plug*.html`），
> 但那些页面**在 VM 上根本不存在**（80 端口无服务），整条链路是死的。
> 本目录按库里真实的商品与候选报价重建了这些页面。

## 数据从哪来

`quotes/*.html` 全部由 `generate.py` **从 `motorparts_db` 的 `product_supplier` 表生成**，
所以页面上的供应商、报价、交期、起订量与 ERP 里的候选报价表**完全一致**。

同时它会产出技能用的映射表：

```
src/skills/procurement/supplier-price-urls/data/url_mapping.yaml
```

**单一数据源**，两者不会不同步。

## 重新生成

```bash
# 项目根目录
.venv/Scripts/python.exe mock_site/generate.py
```

改了 `product_supplier` 的数据（或加了新报价）后重跑即可。

如果报价页不放在 `127.0.0.1`，用环境变量指定：

```bash
MOCK_BASE_URL=http://10.0.0.5 python mock_site/generate.py
```

## 部署到 VM（让沙箱能访问）

沙箱跑在 VM 上，所以页面要由 **VM 上的静态服务**提供 —— 实测沙箱可以访问
`http://127.0.0.1/`（curl 和 python 都通）。

```bash
# 1) 把 quotes/ 传到 VM
scp -r mock_site/quotes root@127.0.0.1:/opt/mock-quotes/

# 2) 在 VM 上起静态服务（端口 80，与 url_mapping.yaml 里的 URL 对应）
cd /opt/mock-quotes && nohup setsid python3 -m http.server 80 > /var/log/mock-quotes.log 2>&1 &

# 3) 放行防火墙
firewall-cmd --permanent --add-port=80/tcp && firewall-cmd --reload

# 4) 验证
curl -sS http://127.0.0.1/quotes/HFS-400-01-珠海中福实业股份有限公司.html | head
```

## 注意

- 这是**演示数据**，页面内容和库里的候选报价一致，不代表真实市场价格
- 技能里明确写了：**映射表里不含价格** —— 价格必须去页面上抓，这正是这一环要验证的能力
- 映射表由 `generate.py` 生成，**不要手改**（会被下次生成覆盖）
