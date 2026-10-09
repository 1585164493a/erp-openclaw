# docs/ 文档索引

> 本目录只保留**随代码维护的现行文档**。历史推演稿、临时清单与已过时的业务域说明都已移出仓库，
> 避免出现"打开就照着旧口径理解"的情况。

---

## 1. 文档一览

| 文档 | 内容 | 什么时候读 |
|---|---|---|
| [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) | **项目全景**：进程拓扑、分层架构、Graph Factory（启动期预计算 vs 请求期建图）、YAML 驱动的子 Agent 与启动期 fail-fast 校验、读写分离与 HITL、中间件栈、沙箱与技能同步、数据层、关键设计取舍 | 想通读整个系统 |
| [`MASTER_DATA_WRITE.md`](MASTER_DATA_WRITE.md) | **主数据写入能力的设计与实现**：写工具安全网（显式登记 + 命名反查两道互补的网）、HITL 从特例变成模式的过程、实证抓到的越权 bug | 关心权限边界与写操作治理 |
| [`STORAGE_MIGRATION.md`](STORAGE_MIGRATION.md) | **存储层演进方案**：为什么长期记忆落 MySQL、短期状态落 Redis、checkpointer 如何退场；分阶段执行计划、硬阻塞清单、依赖变更与回滚 | 关心状态与记忆分层 |
| [`chart_mcp_plan.md`](chart_mcp_plan.md) | **自研图表生成 MCP Server 实施方案**（尚未落地）：多种图表的分阶段覆盖策略与验收标准 | 关心工具侧的可视化能力 |

---

## 2. 建议阅读路径

| 你想干什么 | 读这几份 |
|---|---|
| **5 分钟知道项目全貌** | 根 [`../README.md`](../README.md) → `PROJECT_OVERVIEW.md` §1~§4 |
| **理解 Agent 侧设计** | `PROJECT_OVERVIEW.md` §4~§8 |
| **理解权限与写操作** | `PROJECT_OVERVIEW.md` §6 → `MASTER_DATA_WRITE.md` |
| **理解状态与记忆** | `PROJECT_OVERVIEW.md` §9 → `STORAGE_MIGRATION.md` |
| **改子 Agent 配置** | 根 `README.md` §6 的离线校验（`src/test/validate_configs.py`）→ `PROJECT_OVERVIEW.md` §5 |
| **接手代码** | `PROJECT_OVERVIEW.md` §3（分层）→ §10（目录结构） |

---

## 3. 当前在案的问题

### 3.1 🟡 图表能力依赖外部 MCP

- `src/agent/tools/mcp_client.py` 走 `ANALYSIS_MCP_URL`（默认阿里云百炼 AntV 图表服务，
  Bearer `DASHSCOPE_API_KEY`，transport=sse）。外部端点不可用或配额耗尽时图表能力即失效。
- `src/mcp_server/` 下**没有自研图表模块**，即 `chart_mcp_plan.md` 的方案**尚未落地**。
- **影响面**：3 个子 Agent（`inventory-logistics` / `procurement-analyst` / `sales-analyst`）
  声明并指导使用合并后的 `generate_visualization`；代码是优雅降级（加载失败只告警），
  所以问题不会让服务起不来，但也不会自己暴露。
- **待决策**：① 落地 `chart_mcp_plan.md`；② 把 `generate_visualization` 从三个子 Agent 摘掉；
  ③ 换其它可用的图表方案。

### 3.2 ✅ 启动期依赖未懒加载 —— 已修复

**原问题**：`src/agent/config.py` 在模块顶层构造 `MongoClient` + `MongoDBSaver`，
远端不可达时 `import agent.main_agent` 要等 30s+ 才抛 `ServerSelectionTimeoutError`，
服务走不到启动校验网，且报错不可读。

**实测定位**：`MongoClient(...)` 构造本身不连库（0.2s 返回），真正触发连接的是
`MongoDBSaver(...)` 的构造函数。

**修法**：`serverSelectionTimeoutMS=3000` + checkpointer 改
`get_mongo_client()` / `get_checkpointer()` 懒加载；连接串走环境变量。

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `import agent.main_agent` | FAIL（32.6s 后崩） | **OK 3.2s** |
| `get_checkpointer()` | 卡 30s | **3.0s 快速失败** |
| `precompute_agent_context()` | 跑不到 | **SUCCESS 0.7s**（23 个 MCP 工具 + 6 个子 Agent） |

**连带查出的第二个 bug**：`SKILLS_STORE_NAMESPACE` 在 `config.py` 里是注释状态，
而 `main_agent.py` 有 3 处 import/使用它 —— `import agent.main_agent` 本来必然抛
`ImportError`，只是被前面的 Mongo 错误挡在后面从未暴露。

### 3.3 口径核对

- `src/skills/` 下共 **9 份 `SKILL.md`**（跨 5 个 scope：main 1 / inventory 1 / sales 1 / procurement 4 / master 2）。
- **后端端口 8091**（`start_web.py`、`frontend/vite.config.js` 的代理目标一致）；
  少数脚本注释里仍写 8090，属注释未同步。
- **MCP 工具 23 个 / 8 个实体分组模块**；**迁移脚本 8 份**（001~008）。
- 与 README 的数字差异已在 [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) §13 逐条列出，**以代码为准**。
