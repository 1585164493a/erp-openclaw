---
name: web-content-fetcher
description: "网页内容获取（第三方 Reader 服务）| 当 sandbox 直连抓取被反爬/Cloudflare 挡住时，借 r.jina.ai / markdown.new / defuddle.md 把公网页面转成 Markdown。触发词：抓不到、被拦截、Cloudflare、反爬、fetch blocked、获取网页内容"
author: Boss
metadata:
  openclaw:
    emoji: 🌐
    tags: [web, fetch, markdown, cloudflare, bypass]
---

# 网页内容获取（第三方 Reader 服务）

## 与 web-scraper 的分工（先看这个，别用错）

| | `web-scraper` | **本技能** |
|---|---|---|
| 抓取方式 | 沙箱内**直连**目标 URL | 经**第三方 Reader 服务**转取 |
| 能访问内网吗 | ✅ 能（如 `http://127.0.0.1/...`） | ❌ **不能** —— 第三方服务够不到你的内网 |
| 能绕过反爬吗 | ❌ 容易被 Cloudflare/反爬挡 | ✅ 这是它的用途 |
| 数据经过谁 | 只经过你自己 | **经过第三方**（内容会发给 r.jina.ai 等） |

**判断顺序**：
1. 内网地址 / 普通公网页 → 先用 `web-scraper`
2. 公网页面被反爬挡住、`web-scraper` 抓回空内容或拦截页 → 才用本技能
3. 涉及敏感数据的内部页面 → **不要**用本技能（内容会外发给第三方）

## 支持的服务

| 优先级 | 服务 | 用法 | 适用场景 |
|--------|------|------|----------|
| 1 | **r.jina.ai** | `https://r.jina.ai/{url}` | 最稳定，通用性强 |
| 2 | **markdown.new** | `https://markdown.new/{url}` | Cloudflare 保护的站点 |
| 3 | **defuddle.md** | `https://defuddle.md/{url}` | 备用方案 |

## 使用方法

本目录有两个脚本，等价，任选：

```bash
# Python（推荐）
python /skills/procurement/web-content-fetcher/fetch_content.py <url> [method]

# Shell
bash /skills/procurement/web-content-fetcher/fetch.sh <url> [method]
```

`method` 取值：`jina`（默认）/ `markdown` / `defuddle` / `all`（依次都试）

示例：

```bash
python /skills/procurement/web-content-fetcher/fetch_content.py https://example.com
python /skills/procurement/web-content-fetcher/fetch_content.py https://example.com markdown
```

## 示例

```
用户: 帮我获取 https://news.example.com/article/123 的内容
助手: 先试 web-scraper 直连 → 返回拦截页 →
     改用 fetch_content.py https://news.example.com/article/123 jina →
     拿到 Markdown → read_file 读取并提炼
```

## 注意

- 脚本内部用 `curl` 发请求，**需要沙箱能访问公网**
- 内容是**第三方服务转出来的**，可能有缺漏或改写；引用时注明来源的不确定性
- 三个服务都失败 → 如实告知用户"该页面无法获取"，**不要编造页面内容**
