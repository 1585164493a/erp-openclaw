# ERP_OPENCLAW

**快消零售供应链智能助手** —— 基于 DeepAgents（LangChain 1.x + LangGraph）的多 Agent 系统。
主 Agent 把业务问题路由给 6 个子 Agent，子 Agent 经 MCP 协议取真实业务数据、
在 per-user 沙箱里跑分析脚本，全部结论以 SSE 流式推给 Vue 前端。

> 业务背景：多品牌线日化 / 食品的商品分销与采购（洗发护发、沐浴、口腔护理、
> 衣物与家居清洁、冰淇淋与调味食品等品类）。

---

## 1. 核心设计

### 1.1 YAML 完全驱动子 Agent，新增领域不改 Python

每个子 Agent 是一个 YAML：`name` / `description` / `system_prompt` / `tools` / `skills` /
`middleware_profile` / `interrupt_on`。主 Agent **只依据 `name` + `description` 路由**，
所以 `description` 写得好不好直接决定路由准不准 —— 每个都按【我是谁】【什么时候派给我】
【别派给我什么】三段式写。

### 1.2 读写分离：4 个只读域 + 2 个写 Agent，写操作全部挂人工审批

```
只读领域 Agent（4）                        写操作 Agent（2，均挂 HITL）
├─ inventory-logistics   库存/物流/补货     ├─ procurement-order        下单/改单
├─ sales-analyst         销量/趋势/奖金     └─ master-data-maintain     供应商准入/客户开户/新商品建档
├─ procurement-analyst   比价/采购成本
└─ master-data           档案/表结构/字段含义
```

**不是给每个域配一个 Agent，而是按「读 / 写」拆开。** 同一个域可以有一读一写两个 Agent
（采购域：`procurement-analyst` + `procurement-order`；主数据域：`master-data` + `master-data-maintain`）。

为什么不把写工具直接塞进只读域：读工具与写工具同处一个 Agent 会**诱导非预期写入** ——
模型刚查出"这个供应商不存在"，最自然的下一个动作就是把它建出来。
**权限边界落在工具集上，不靠 prompt 里写"不要写"。**

### 1.3 启动期 fail-fast 校验网

配置错了必须让服务起不来，不能让某个 Agent 静默消失或静默获得权限。启动时校验：

| 校验 | 拦住的错误 |
|---|---|
| 工具名精确命中（支持 `前缀*`） | YAML 里工具名写错 → 该 Agent 静默少工具 |
| `middleware_profile` 注册表 | 档位名写错 |
| `skill_scope` 由 skills 路径推导 | 技能复制到 A、Agent 在 B 找 |
| 子 Agent `name` 唯一 | 重名导致后者覆盖前者的 scope / 中间件 |
| 空 YAML 守卫 | `safe_load` 返回 `None` 被当成合法配置 |
| **写工具必须配 `interrupt_on`** | **漏配 = 静默获得免审批写权限** |
| **写工具登记表反向核对** | **新增写工具忘了登记 → 安全网出现缺口** |

最后两条是两道互补的网：显式登记防**误判**，命名反查防**漏登记**，各自补对方的洞。

> 实证：这两道网上线当天就抓到真 bug —— `procurement-analyst` 的 `product_*` 通配符
> 把新增的 `product_create` 一并捞了进去，让一个只读分析 Agent 静默获得了建档写权限。
> 详见 [`docs/MASTER_DATA_WRITE.md`](docs/MASTER_DATA_WRITE.md) §6。

### 1.4 Graph Factory：启动期预计算 + 请求期轻量装配

- **启动一次**（`precompute_agent_context`）：加载 MCP 工具 → 合并可视化工具 → 载入子 Agent YAML → 跑校验网
- **每请求**（`create_main_agent`）：基于 per-user OpenSandbox 沙箱轻量建图

---

## 2. 进程与端口拓扑

```
Vue3 前端                    :3000
  └─ FastAPI 后端（SSE 对话流）:8091   api_view.web_main
       └─ MCP Server           :8099   src/mcp_server（FastMCP，23 个工具）
            └─ ERP API          :8080   src/erp_api（FastAPI 直连 MySQL，24 个端点）
                 └─ MySQL 8      :3306   motorparts_db
```

一键启动：`python start_web.py`（按依赖顺序拉起 ERP API → MCP → 后端 → 前端，带健康检查）。

> Agent 全链路还需要 VM 上的 MongoDB（checkpointer）+ OpenSandbox（代码沙箱）。
> **未开 VM 时 ERP API / MCP / 数据层可独立验证**，Agent 层初始化会失败。

---

## 3. 数据层

| 域 | 内容 |
|---|---|
| 商品 / 供应商 / 客户 | 75 个快消 SKU、33 家供应商、40 家客户（KA 商超 / 经销商 / 电商） |
| 销售 | `sales_daily` 2250 行（含渠道维度）、销售目标、奖金规则；含注入的异常（24 条断货 / 30 条促销尖峰） |
| 采购 | 178 张采购单、626 条订单明细、候选报价、历史价格 |
| 库存 / 物流 | 6 个区域 DC（华东-上海 / 华南-广州 / 华北-天津 / 华中-武汉 / 西南-成都 / 东北-沈阳） |
| 元数据 | `meta_dict`：表用途 + 字段枚举取值的中文语义（结构从 `information_schema` 实时读） |

迁移脚本在 `db/migrations/`（8 份，幂等 + `_migration_log`）；基线数据导出为仓库根目录的 `motorparts_db.sql`。

---

## 4. 技术栈

- **Agent**：DeepAgents + LangGraph Graph Factory；MCP（FastMCP，streamable_http）
- **模型**：DeepSeek（主链）；多 provider key 经 `.env` 注入
- **后端**：FastAPI + SSE 流式
- **前端**：Vue3 + Vite
- **存储**：MySQL 8（业务数据）、MongoDB（LangGraph checkpointer，支撑 HITL 中断状态持久化）、InMemoryStore（开发期）
- **沙箱**：OpenSandbox，per-user 隔离，代码在沙箱内执行

---

## 5. 目录

```
src/erp_api/        Python ERP REST 层（直连 MySQL，camelCase 形状，替代原 Java 后端）
src/mcp_server/     FastMCP 工具（按实体分组，8 个模块 / 23 个工具）
src/agent/          主 Agent、子 Agent 加载器、9 层中间件栈、MCP 客户端装配
  ├─ subagents/configs/   6 个子 Agent 的 YAML
  ├─ memory/              主 Agent system prompt + AGENTS.md（通用准则）
  └─ middlewares/         沙箱健康、任务账本、工具错误、上下文注入、技能同步、摘要 …
src/skills/         9 项领域 Skill（SOP 手册，渐进式加载）
src/api_view/       后端 FastAPI（SSE 对话流）+ agent_loader
src/test/           工具与 Agent 的验证脚本
db/migrations/      数据迁移（8 份，幂等）
motorparts_db.sql   基线数据导出（本地还原用）
docs/               设计文档（索引见 docs/README.md）
```

---

## 6. 环境与运行

```bash
# 1) 依赖
.venv/Scripts/activate
pip install -r requirements.txt      # 首行 -e . 为本仓库可编辑安装

# 2) 配置
# .env（已被 .gitignore 忽略）：DeepSeek / Zhipu 等 key，参考 .env.example

# 3) 本机 MySQL 8（root/123456，库 motorparts_db）
#    基线可用仓库根目录的 motorparts_db.sql 还原

# 4) 一键起本地四件套
python start_web.py
```

**离线校验子 Agent 配置**（不需要 VM / 沙箱 / LLM，改完 YAML 先跑这个）：

```bash
cd src && python test/validate_configs.py
```
它用进程内注册的真实工具名跑完整校验链（等价线上 Phase 5.5 + Phase 7），失败即退出码非 0。

控制台中文乱码时先 `chcp 65001`。

---

## 7. 已知限制

- **🟡 图表能力依赖外部 MCP**：`src/agent/tools/mcp_client.py` 走可配置的
  `ANALYSIS_MCP_URL`（默认阿里云百炼 AntV 图表服务，Bearer `DASHSCOPE_API_KEY`，
  transport=sse）；`src/mcp_server/` 下**没有自研图表模块**（`docs/chart_mcp_plan.md` 的方案尚未落地）。
  3 个子 Agent（`inventory-logistics` / `procurement-analyst` / `sales-analyst`）
  的工具列表与 prompt 里声明并指导使用合并后的 `generate_visualization`，
  该端点不可用时**优雅降级**（加载失败只告警，不阻断启动）。
- **Agent 全链路依赖外部服务**：MongoDB checkpointer 与 OpenSandbox 沙箱都部署在同一台远端 VM
  （地址由 `.env` 注入），未开 VM 时只有 ERP API / MCP / 数据层可跑。
  **但启动期的配置校验不依赖 VM** —— `precompute_agent_context()` 与
  `src/test/validate_configs.py` 在 VM 不可达时也能跑通（依赖已改懒加载，见下）。
- **`docs/` 与 README 存在少量数字口径差异**（后端端口 8091 / MCP 模块数 / 迁移脚本份数），
  逐条列在 [`docs/PROJECT_OVERVIEW.md`](docs/PROJECT_OVERVIEW.md) §13，以代码为准。
- **Skill 数量口径**：当前 9 项（`src/skills/` 下 9 份 `SKILL.md`）。

---

## 8. 文档

完整索引见 [`docs/README.md`](docs/README.md)。最值得先读的三份：

| 文档 | 内容 |
|---|---|
| [`docs/PROJECT_OVERVIEW.md`](docs/PROJECT_OVERVIEW.md) | 项目全景（架构 + 逐层机制） |
| [`docs/MASTER_DATA_WRITE.md`](docs/MASTER_DATA_WRITE.md) | 主数据写入能力的设计与实现（含写工具安全网） |
| [`docs/STORAGE_MIGRATION.md`](docs/STORAGE_MIGRATION.md) | 存储层演进方案（长期记忆→MySQL、短期记忆→Redis） |
