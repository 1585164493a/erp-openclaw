# 自研图表生成 MCP Server — 实施方案

> 目标：用官方 `mcp` SDK **手写协议层**，实现一个图表生成 MCP Server，
> 替换掉已经 **410 Gone** 的魔搭 `analysis` MCP（`src/agent/tools/mcp_client.py:26`），
> 且**不破坏**现有 Agent 侧的调用契约。

---

## 0. 为什么是"从头写"：L1 vs L2

| 层级 | 现有实现 | 本次目标 |
|---|---|---|
| L1 | `src/mcp_server/server_main.py:1` `from fastmcp import FastMCP`，用 `@mcp.tool` 装饰器 | —— |
| **L2** | —— | `from mcp.server import Server`，手写 `list_tools` / `call_tool` handler，自己挂 `StreamableHTTPSessionManager` |

L2 的意义：你会真正碰到 MCP 协议本体 —— JSON-RPC 2.0 方法（`initialize` / `tools/list` / `tools/call`）、
`Mcp-Session-Id` 会话头、`Tool.inputSchema`（就是给模型看的参数说明书）。
环境已具备：`mcp 1.27.0`、`fastapi 0.141.1`、`httpx 0.28.1`。

---

## 1. 必须兼容的既有契约（改错了会静默失效）

现有链路：`mcp_client.load_mcp_tools()` 取回远程工具 → `create_generate_chart_tool()` 把 N 个
`generate_*` 工具**合并成 1 个** `generate_visualization(chart_type, chart_config)`。

新 Server 必须满足这 4 条，否则合并函数会把工具丢进 `other_tools` 而**不报错**：

1. **工具名后缀**必须是 `_chart` / `_map` / `_diagram` / `_graph`
   （判定逻辑：`src/agent/tools/chart_generator.py:290-320`）
   - `generate_bar_chart` → key `bar`（`_chart` 被剥掉）
   - `generate_pin_map` → key `pin_map`（`_map` 保留）
   - `generate_flow_diagram` → key `flow_diagram`
   - `generate_network_graph` → key `network_graph`
2. **入参是顶层扁平字段**，不是 `{"chart_config": {...}}`
   （证据：`chart_generator.py:334` `await chart_tool.ainvoke(chart_config)`，dict 直接当入参）
   → inputSchema 顶层必须有 `data`，以及可选 `width` / `height` / `title` / `theme` / `style` / `axisXTitle` / `axisYTitle`
3. **返回值是可渲染的图片 URL，且必须以 `.png` 结尾**
   → 前端只对"扩展名是图片"或"白名单域名"的 URL 内联渲染
   （证据：`frontend/src/components/MarkdownRenderer.vue:47`，`imageHosts` 只有 4 个阿里域名）
   ⚠️ 返回 `.html` 会被前端降级成一个链接，不是图表
4. **工具名集合要凑够 26 个**，与 `CHART_TYPE_GUIDE`（`chart_generator.py:29-57`）的 key 完全一致

26 个工具名清单（按命名规则推导）：

```
标准图 19：generate_area_chart, generate_bar_chart, generate_boxplot_chart,
  generate_column_chart, generate_dual_axes_chart, generate_funnel_chart,
  generate_histogram_chart, generate_line_chart, generate_liquid_chart,
  generate_organization_chart, generate_pie_chart, generate_radar_chart,
  generate_sankey_chart, generate_scatter_chart, generate_treemap_chart,
  generate_venn_chart, generate_violin_chart, generate_waterfall_chart,
  generate_word_cloud_chart
地图 3：generate_district_map, generate_pin_map, generate_path_map
关系图 3：generate_fishbone_diagram, generate_flow_diagram, generate_network_graph
思维导图 1：generate_mind_map
```

（`generate_spreadsheet` 不属于图表，可忽略）

---

## 2. 技术选型

**渲染输出必须是 PNG**（见契约 3），所以选型围绕"谁能出 PNG"：

| 方案 | 出 PNG | 覆盖 26 种 | 依赖 | 评价 |
|---|---|---|---|---|
| **matplotlib** | 直接出 | ~12/26 | 单包 | ✅ **主方案**，零浏览器依赖 |
| pyecharts | 需 playwright 截图 | ~22/26 | 重 | P3 阶段再上，换地图/桑基/关系图 |
| plotly+kaleido | 直接出 | ~18/26 | 二进制 | 备选 |
| PIL 手绘 | 直接出 | 1/26 | 已装 | 只够兜底 |

当前 venv 实测：`pyecharts / matplotlib / plotly / pandas / jinja2` **全部 MISS**，只有 `PIL` + `numpy` 是 OK。
→ **P0 先装 `matplotlib`**。

**分阶段覆盖策略**（26 种不要一次做完）：

- **P1（9 种，覆盖 80% 业务场景）**：`bar, column, line, area, pie, scatter, histogram, boxplot, radar` — matplotlib 全搞定
- **P2（+7 种）**：`funnel, waterfall, treemap, word_cloud, violin, dual_axes, organization` — matplotlib 变体
- **P3（剩下 7 种 + 3 地图）**：`sankey, venn, liquid, district_map, pin_map, path_map, fishbone_diagram, flow_diagram, network_graph, mind_map`
  → 引入 `pyecharts` + `playwright` 截图
- **P4**：切换 `mcp_client.py` 指向新服务；旧魔搭 URL 保留做降级分支

---

## 3. 目录结构

```
src/chart_mcp/
├── __init__.py
├── config.py            # 端口/输出目录/URL 前缀/字体路径/缓存上限
├── protocol.py          # 26 个 Tool 的定义（name + description + inputSchema）—— 唯一的"契约文件"
├── server_main.py       # L2 手写：Server 实例 + @list_tools + @call_tool + 启动
├── transport.py         # StreamableHTTPSessionManager 挂到 Starlette，+ 静态 /charts 路由
├── storage.py           # 图片落盘、URL 生成、按时间/数量清理旧图
├── renderers/
│   ├── __init__.py      # REGISTRY: chart_type -> render 函数（注册表模式）
│   ├── common.py        # 入参校验、主题(palette)、中文字体、figsize、保存
│   ├── basic.py         # P1 的 9 种
│   ├── advanced.py      # P2 的 7 种
│   └── graphs.py        # P3 的图/地图（先留空）
└── static/
    └── charts/          # 生成图片落盘目录（加入 .gitignore）
```

设计要点：**`protocol.py` 是唯一契约来源**。`skills/procurement/chart_params.md`
（给 Agent 看的参数速查）应当**从 protocol.py 自动生成**，避免两边漂移 ——
现有 `chart_generator.py:215` 已经有 `_extract_tool_params` 的雏形，可以复用这个思路。

---

## 4. 要实现的东西（Handler 清单）

### 4.1 协议层（server_main.py）

| # | 项 | 要做什么 | 你要理解的协议点 |
|---|---|---|---|
| 1 | `Server("chart-mcp", version=...)` | 构造 server，声明能力 | 服务端 identity + capabilities |
| 2 | `@server.list_tools()` | 返回 `list[types.Tool]`，从 `protocol.py` 拉 | `tools/list`，每个 Tool 的 `inputSchema` 是 JSON Schema |
| 3 | `@server.call_tool()` | 收 `(name, arguments)`，查 REGISTRY，分发 | `tools/call`，返回 `list[TextContent]` 或 `ImageContent` |
| 4 | 错误处理 | 未知 chart_type / 缺 data → 返回**结构化错误文本**而不是抛异常 | 让模型能自修复（呼应 ToolErrorMiddleware） |
| 5 | 生命周期 | 启动建目录/加载字体；关闭清理 | lifespan |

### 4.2 传输层（transport.py）

| # | 项 | 要做什么 | 你要理解的协议点 |
|---|---|---|---|
| 6 | `StreamableHTTPSessionManager(app=server)` | 挂到 Starlette，`/mcp` 一条路由 | streamable-http = POST 收 JSON-RPC + 可选 SSE 流 |
| 7 | 静态路由 `/charts/{file}.png` | `StaticFiles` 或自写 | 让返回的 URL 真能被前端取到 |
| 8 | 端口 | **用 8100**（8099 已被现有 erp MCP 占用，见 `mcp_server/server_config.py`） | —— |
| 9 | （可选）`notifications/tools/list_changed` | 演示对通知机制的理解 | 加分项，非必需 |

### 4.3 渲染层（renderers/）

| # | 项 | 要做什么 |
|---|---|---|
| 10 | `common.py` 工具函数 | `normalize_data()` 校验/补齐字段；`apply_theme()` 套 palette；`setup_cjk_font()` 设中文字体（`C:\Windows\Fonts\msyh.ttc`）；`save_fig()` 落盘并返回 URL |
| 11 | `REGISTRY` 注册表 | `chart_type -> render(config) -> filepath`，`protocol.py` 与它必须键集合一致（有断言测试兜底） |
| 12 | P1 九种 | bar / column / line / area / pie / scatter / histogram / boxplot / radar |
| 13 | P2 七种 | funnel / waterfall / treemap / word_cloud / violin / dual_axes / organization |
| 14 | 数据模式适配 | 三种模式：`category-value` / `time-value` / 分布（纯数字数组，如 histogram），入参校验要按模式走 |
| 15 | 清理策略 | 每次调用后按"保留最近 N 张 / 超过 T 小时删除"清理 `static/charts/` |

---

## 5. 分阶段计划与验收标准

每个阶段**必须跑通验收才算完**，不要跳。

### P0 — 骨架打通（1 个工具）
- 做：协议层 + 传输层 + 只实现 `generate_pie_chart`
- **验收**：
  - `python -m chart_mcp.server_main` 能起，`http://127.0.0.1:8100/mcp` 有响应
  - 用**官方 mcp 客户端**连上 → `list_tools()` 返回 1 个工具，名字是 `generate_pie_chart`
  - 调 `call_tool` → 返回的字符串以 `.png` 结尾，浏览器打开是张饼图
  - 图能内联显示在你的聊天前端里（证明确实是 `.png` 而非 HTML）

### P1 — 9 种基础图
- **验收**：
  - `MultiServerMCPClient` 连上新服务，`create_generate_chart_tool()` 后 `tool_map` 里**有 9 个 key**（不是 0 个 —— 这是最关键的接口契约测试）
  - **断言**：`set(REGISTRY.keys()) == set(protocol.TOOLS.keys())`（防注册表/协议漂移）
  - 端到端：Agent 说"画个价格对比柱状图"→ 前端出图
  - 异常：空 `data` / 单点 `data` / 超长中文 category → 返回错误文本，不 500

### P2 — 16 种
- **验收**：`CHART_TYPE_GUIDE` 的 26 个 key 中覆盖 16 个；未实现的返回"暂不支持 X，可用：..."（**不能静默失败**）

### P3 — 补齐 26 种
- **验收**：`set(CHART_TYPE_GUIDE.keys()) - set(REGISTRY.keys()) == set()`，一行断言跑过

### P4 — 切换与降级
- 做：`mcp_client.py` 的 `analysis` 指向新服务；新服务不可达时回退旧 URL（旧 URL 已死，所以实际行为是"图表功能优雅降级为无"）
- **验收**：关掉新服务 → Agent 不崩，日志出现"图表工具缺失"警告（`mcp_client.py:60` 已有这个分支）

---

## 6. 已知风险 / 坑

| # | 风险 | 说明 |
|---|---|---|
| 1 | **命名后缀写错 → 静默失效** | 工具名不以 `_chart/_map/_diagram/_graph` 结尾，`create_generate_chart_tool` 会丢进 `other_tools`，**不报错**，Agent 直接没有图表工具 |
| 2 | **返回 HTML → 前端只显示链接** | `MarkdownRenderer.vue:47` 只按扩展名/白名单判定，必须 `.png` |
| 3 | **中文乱码/方框** | matplotlib 默认字体不含中文，必须显式设 `font.sans-serif` + `axes.unicode_minus=False` |
| 4 | **端口冲突** | 8099 被 erp MCP 占用 → 新服务 8100 |
| 5 | **参数漂移** | `skills/procurement/chart_params.md` 是 Agent 的参数说明书，必须与 `protocol.py` 一致；建议自动生成 |
| 6 | **依赖缺失** | 当前 venv 无任何绘图库，P0 必须先装 |
| 7 | **磁盘堆积** | 每次调用生成一张图，必须有清理策略，否则 `static/charts/` 无上限增长 |
| 8 | **并发写文件名冲突** | 用 `uuid4()` 命名，别用时间戳秒级 |

---

## 7. 设计收益与后续

- 「MCP 不是黑盒：它就是一个 JSON-RPC 2.0 服务，`tools/list` 返回的 `inputSchema` 就是喂给模型的函数签名」
- 「我不用 FastMCP 也能写 —— FastMCP 帮我省掉的只是 handler 样板和传输层胶水」
- 「上游 MCP 服务挂了（410 Gone）我怎么办：自己实现替换 + 保留降级分支」
- 「一份契约两处使用会漂移，所以我把 schema 做成唯一来源，参数文档自动生成」
- 「工具数量爆炸（26 个）时，合并成一个入口 + 把长参数说明挪到沙箱文件，是按需加载的取舍」（这条已有实现，`chart_generator.py`）