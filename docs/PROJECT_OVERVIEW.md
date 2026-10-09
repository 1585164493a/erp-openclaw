# ERP_OPENCLAW 项目全景

> **快消零售供应链智能助手** —— 基于 DeepAgents（LangChain 1.x + LangGraph）的多 Agent 系统。
> 本文按**当前代码**逐处核对后重写，所有数字都标注了口径来源；无法从代码确认的地方写「以代码为准」。
> 与根 [`../README.md`](../README.md) 的口径差异集中在最后一节。

---

## 1. 一句话定位

主 Agent 只做**路由与协调**，把业务问题分派给 **6 个 YAML 声明的子 Agent**
（4 个只读领域 Agent + 2 个写操作 Agent）；子 Agent 经 **MCP** 取真实业务数据、
在 **per-user OpenSandbox 沙箱**里跑分析脚本、用**四态任务账本**追踪委派、
被 **HITL 中断**卡住写操作，最终以 **SSE 流式**推给 Vue3 前端。

业务背景是**多品牌线日化 / 食品的商品分销与采购**（快消零售域）：商品、供应商、客户三类主数据，
销量与销售目标/奖金规则、采购单与订单明细、多供应商报价、区域 DC 库存与物流运单。

---

## 2. 进程与端口拓扑

| 进程 | 入口 | 端口 | 说明 |
|---|---|---|---|
| Vue3 前端（Vite） | `frontend/`（`npm run dev`） | **3000** | 开发服务器把 `/api` 代理到 `127.0.0.1:8091`（`frontend/vite.config.js`） |
| Agent 后端 | `api_view.web_main:app` | **8091** | FastAPI + SSE；启动时初始化 Agent（`start_web.py`） |
| MCP Server | `mcp_server.server_main` | **8099**（`/mcp`） | FastMCP，`streamable-http`，把 ERP REST 包成 MCP 工具 |
| ERP REST API | `erp_api.main:app` | **8080** | FastAPI + `pymysql` **直连 MySQL**，返回 camelCase 形状 |
| MySQL 8 | — | 3306 | 业务库 `motorparts_db` + 长期记忆库 `agent_state` |
| MongoDB | VM `127.0.0.1:27017` | 27017 | 库 `langchain_db`：checkpointer、任务账本、展示消息、沙箱注册表 |
| OpenSandbox 控制面 | VM `127.0.0.1:8080` | 8080 | per-user 沙箱（`agent/config.py` 的 `SANDBOX_CONFIG`，可用 `OPENSANDBOX_DOMAIN` 覆盖） |
| 外部图表 MCP | `ANALYSIS_MCP_URL`（默认阿里云百炼 AntV 图表服务） | — | **可选**：加载失败只告警降级，不影响启动 |

调用方向：

```
Vue3 :3000 ──HTTP/SSE──> :8091 Agent 后端 ──MCP(streamable_http)──> :8099 MCP Server
                              │                                          └──HTTP──> :8080 ERP API ──> MySQL
                              └──MCP(sse)──> 外部图表 MCP（可选，降级）
                              └──OpenSandbox SDK──> VM 沙箱文件系统（不在 HTTP 链上）
```

- 沙箱**不在 HTTP 调用链上**：Agent 通过 OpenSandbox SDK 直接操作沙箱文件系统。
- `:8080` 在本机（ERP API），`127.0.0.1:8080` 在 VM（沙箱控制面），是两台机器上的同名端口，不冲突。
- 一键启动：`python start_web.py` —— 按依赖顺序拉起 ERP API → MCP → 后端（等 `/health` 返回 healthy）→ 前端，并在启动前/退出时清理自己的端口占用（按进程树 `taskkill /T`）。

> Agent 全链路还依赖 VM 上的 MongoDB 与 OpenSandbox。**未开 VM 时，ERP API / MCP / 数据层仍可独立验证**；
> 但启动期的配置校验不依赖 VM（见 §5.3）。

---

## 3. 分层架构

| 层 | 代码位置 | 职责 | 失败时影响谁 |
|---|---|---|---|
| 前端 | `frontend/src/`（`api/chat.js`、`api/history.js` + 7 个 Vue 组件） | SSE 消费与渲染、工具调用面板、中断横幅、会话列表 | 只影响交互 |
| 后端 | `src/api_view/`（`web_main.py`、`agent_loader.py`、`api/chat.py`、`api/history.py`） | FastAPI 路由、SSE 流、per-user graph 装配、展示消息读写 | 整站不可用 |
| 主 Agent | `src/agent/main_agent.py` + `memory/prompts.py` + `memory/AGENTS.md` | 路由、协调、`web_search` 自行答复通用问题 | 整站不可用 |
| 子 Agent | `src/agent/subagents/`（`loader.py` + `configs/*.yaml`） | 领域分析与写操作执行 | 单域能力缺失 |
| 中间件 | `src/agent/middlewares/`（9 个）+ `middleware_config.py`（档位工厂） | 沙箱健康、任务账本、工具错误、上下文注入、技能同步、摘要、记忆更新、熔断、调用上限 | 见 §7 |
| MCP | `src/mcp_server/` | 把 ERP REST 包装成 MCP 工具（**8 个实体分组模块 / 23 个工具**） | Agent 起不来（ERP 工具是硬依赖） |
| ERP API | `src/erp_api/`（`main.py` + `db.py`） | **24 个端点**（含 `/health`，业务端点 23 个），统一 `{code, message, data}` + camelCase | 所有业务查询失败 |
| 存储 | MySQL / MongoDB / VM 沙箱 | 业务数据、checkpoint、长期记忆、沙箱注册表 | 见 §9 |

**后端 SSE 一次对话的形状**（`api/chat.py`）：

```
POST /api/chat/stream {message, thread_id, user_id}
  └─ stream_chat_response(...)
       ├─ agent_loader.get_agent_for_user(user_id)   # per-user 沙箱 → 现场建 graph
       ├─ async for chunk in graph.astream(input, config, stream_mode="messages")
       │    ├─ chunk["ns"] 以 "tools:" 开头 = 来自子 Agent → source 标成子 Agent 名
       │    │   （名字从 task 委派的 subagent_type 参数反查，见 extract_subagent_type）
       │    └─ 逐类转成 SSE 事件
       └─ 结束发 {"type":"done"}，并落库本轮展示消息
```

SSE 事件类型（前端 `api/chat.js` 的 switch 与之逐一对应）：
`token` / `tool_start` / `tool_args` / `tool_result` / `tool_end` / `usage`（token 用量）/
`interrupt`（`hitl_approval` 或 `order_info_supplement`，另有 `unknown` 兜底）/ `done` / `error`。

**后端接口清单**（`api_view/` 下实际注册 10 条）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat/stream` | 流式对话（SSE） |
| POST | `/api/chat/{thread_id}/resume` | 中断恢复（HITL / 数据补充） |
| GET | `/api/chat/{thread_id}` | 当前会话状态 |
| GET | `/api/chat/{thread_id}/history` | 状态历史（直接查 checkpointer） |
| GET | `/api/history` | 会话列表（分页） |
| GET | `/api/history/{thread_id}/messages` | 消息历史（优先展示消息表，回退 checkpoint） |
| DELETE | `/api/history/{thread_id}` | 删会话（连带清 checkpoint、展示消息、任务账本） |
| PATCH | `/api/history/{thread_id}` | 未实现，stub |
| GET | `/health` `/` | 健康检查 / 首页信息 |

---

## 4. Graph Factory：启动期预计算 vs 请求期建图

核心取舍：**MCP 连接与 YAML 解析慢、且与用户无关 → 启动算一次；沙箱必须 per-user → 每请求重建。**

### 4.1 启动一次：`precompute_agent_context()`（`main_agent.py`）

| 阶段 | 做什么 | 关键点 |
|---|---|---|
| Phase 2 | `load_mcp_tools()` | 连 ERP MCP（硬依赖，失败 `raise RuntimeError`）+ 外部图表 MCP（软依赖，失败只告警） |
| Phase 3 | `create_generate_chart_tool()` | 把外部图表 MCP 的多枚 `generate_*` 工具**合并成 1 个** `generate_visualization` |
| Phase 5 | `load_subagent_configs()` | 读 `subagents/configs/*.yaml`（当前 6 份） |
| Phase 5.5 | 跑校验网 | `middleware_profile` / skill scope / `interrupt_on`（见 §5.2） |
| — | `build_skill_scope_map()` | 「Agent 名 → 技能 scope 目录」映射，由 skills 路径推导 |

产物是不可变的 `PrecomputedContext`：`toolset` / `extra_mcp_tools` / `generate_visualization` /
`raw_subagent_configs` / `skill_scope_map`。**只放不依赖沙箱的东西**。

### 4.2 每请求一次：`create_main_agent()`

| Phase | 做什么 | 关键点 |
|---|---|---|
| 1 | `backend_factory` → `CompositeBackend` | `default=sandbox`；`/memories/` 与 `/persisted-skills/` 路由到 `StoreBackend` |
| 1.4 | 上传 `AGENTS.md` 到沙箱 `/AGENTS.md` | 主 Agent 的 `memory=["/AGENTS.md"]` |
| 2/3/5 | 直接复用预计算结果 | —— |
| 3.6 | 建沙箱相关工具 | `assign_skill`、`download_sandbox_file`（依赖 backend 与 store） |
| 4 | 组装工具池 | 排除已被合并的原始图表工具、加回保留的独立工具、加本地工具，**重名显式告警去重** |
| 6 | 子 Agent 中间件 | 按 YAML 的 `middleware_profile` 查注册表构建 |
| 7 | `resolve_subagent_tools()` | YAML 里的工具名字符串 → 真实工具对象 |
| 8 | 主 Agent 中间件栈 | 顺序即语义，见 §7 |
| 9 | `create_deep_agent(...)` | model / system_prompt / `skills=["/skills/main/"]` / memory / tools（`web_search`、`assign_skill`、`download_sandbox_file`）/ subagents / middleware / backend / store / checkpointer / `context_schema=ProcurementContext` |

沙箱本身按 user 缓存（`ensure_sandbox_for_user` 命中 < 0.1s），**graph 每请求重建**：
这样沙箱重建不影响已建好的图，因为图里持有的是 `SandboxBackendProxy` 这个稳定句柄。

---

## 5. YAML 驱动的子 Agent 与启动期 fail-fast 校验

### 5.1 6 个子 Agent（`src/agent/subagents/configs/*.yaml`）

| 子 Agent | 中间件档位 | 技能目录 | 工具数 | 写工具 / HITL |
|---|---|---|---|---|
| `inventory-logistics` | analyst | `/skills/inventory/` | 9 | 无（只读） |
| `sales-analyst` | analyst | `/skills/sales/` | 8 | 无（只读） |
| `procurement-analyst` | analyst | `/skills/procurement/` | 9 | 无（只读） |
| `master-data` | analyst | `/skills/master/` | 8 | 无（只读） |
| `procurement-order` | write | `/skills/procurement/` | 5 | `order_create` / `order_update` |
| `master-data-maintain` | write | `/skills/master/` | 7 | `supplier_create` / `customer_create` / `product_create` |

- 工具数是 YAML `tools` 列表里的条目数（同一域内两个 Agent 可共用技能目录）。
- 主 Agent **只依据 `name` + `description` 路由**，所以每个 `description` 都按
  【我是谁】【什么时候派给我】【别派给我什么】三段式写，把最容易派错的方向显式推给别的 Agent。
- 6 个 YAML 的 `model` 字段当前全部是注释状态 → 都继承主 Agent 模型，切换模型只改 `agent/config.py` 一处。

### 5.2 启动期校验网（Phase 5.5，任一不过 → 服务起不来）

| 校验 | 拦住的错误 |
|---|---|
| 工具名精确命中（支持 `前缀*`，**刻意不支持子串匹配**） | YAML 里工具名写错 → 该 Agent 静默少工具 |
| `middleware_profile` 必须在注册表内 | 档位名写错 |
| `skill_scope` 由 skills 路径推导且**恰好一个**、必须在 `/skills/` 下 | 技能复制到 A、Agent 在 B 找 |
| 子 Agent `name` 唯一 | 重名导致后者覆盖前者的 scope / 中间件 |
| YAML 空文件 / 非映射守卫 | `safe_load` 返回 `None` 被当成合法配置 |
| **写工具必须配 `interrupt_on`**（双向：有写工具必须配；配了但不是本 Agent 写工具 = 静默无效也报错） | **漏配 = 静默获得免审批写权限** |
| **写工具登记表反向核对**（`WRITE_TOOLS` + 命名后缀 `_create/_update/_delete/...`） | **新增写工具忘了登记 → 安全网出现缺口** |

最后两条是互补的两张网：显式登记防**误判**，命名反查防**漏登记**，各自补对方的洞。
这张网上线当天就抓到真 bug：`procurement-analyst` 的 `product_*` 通配符把新增的 `product_create`
一并捞了进来，让一个只读分析 Agent 静默获得了建档写权限 —— 现已改为显式枚举，
且 YAML 里写明了为什么不用通配符。

覆盖范围是**业务数据写操作**；`assign_skill` 这类「Agent 自身元操作」不入网，
否则每次技能分配都要人工审批，会淹没真正的业务审批。

### 5.3 离线校验（不需要 VM / 沙箱 / LLM）

```bash
cd src && python test/validate_configs.py
```

它在**进程内**触发 MCP 工具注册拿到真实工具名集合，再跑 `load_subagent_configs` +
`resolve_subagent_tools` + 全部启动期校验（等价线上 Phase 5 / 5.5 / 7），失败即退出码非 0。
改完 YAML 先跑这个，比等完整启动失败快得多。

---

## 6. 读写分离与 HITL 审批

### 6.1 按「读 / 写」拆 Agent，而不是按业务域拆

同一个域可以有一读一写两个 Agent：采购域 = `procurement-analyst`（读）+ `procurement-order`（写）；
主数据域 = `master-data`（读）+ `master-data-maintain`（写）。

**为什么不把写工具塞进只读域**：读工具与写工具同处一个 Agent 会诱导非预期写入 ——
模型刚查出「这个供应商不存在」，最自然的下一个动作就是把它建出来。
**权限边界落在工具集上，不靠 prompt 里写「不要写」。**
写 Agent 的工具集也刻意收窄（例如建档 Agent 不给 `meta_schema` / `meta_dict`：
枚举取值写在 system_prompt 里比再挂两个工具更省 token，权限面也更小）。

### 6.2 两类中断

| 中断类型 | 触发者 | 前端拿到什么 | 恢复方式 |
|---|---|---|---|
| `order_info_supplement`（数据补充） | `request_order_info` 工具（`agent/tools/hitl_tools.py`） | 缺失字段 + 已收集数据 | 用户补自由文本 → `POST /api/chat/{thread_id}/resume` |
| `hitl_approval`（写操作审批） | 声明了 `interrupt_on` 的写工具，执行前 | 待审批动作摘要 | `approve` / `reject` → resume |

底层机制：LangGraph `interrupt()` + **MongoDB checkpointer** 持久化中断状态 →
服务重启后仍能恢复；`Command(resume=...)` 继续执行。

两条流程的分工是刻意的：**意图不清**（不知道用户想干什么）由主 Agent 先确认；
**意图清楚、只是缺字段**必须直接委派写 Agent，由它用 `request_order_info` 向用户索要 ——
主 Agent 自己先问一轮等于绕过了子 Agent 的数据补充环节。
写 Agent 的档位上限也更紧（见 §7），因为每次写都要人审批，循环次数多等于反复打扰人。

---

## 7. 中间件栈（顺序即语义）

主 Agent 中间件栈（`main_agent.py` Phase 8，9 个业务中间件 + 2 个调用上限，共 11 个实例）：

```
1  SandboxHealthMiddleware           每次 agent step 前 ping 沙箱 → 失败自动重建
2  TaskStateMiddleware               ★ 钩住 task 委派 → 四态账本
3  ToolErrorMiddleware               工具异常 → ToolMessage(status="error")，不崩整轮
4  ContextInjectionMiddleware        注入 user_id / username 到对话开头
5  SkillsSyncMiddleware              本地技能文件 → 沙箱（按哈希增量同步）
6  UserSkillsRestoreMiddleware       Store 里持久化的技能 → 沙箱
7  build_summarization_middleware    对话摘要（compact_conversation 工具）
8  MemoryUpdateMiddleware            回复后更新 /memories/{user_id}/preferences.md
9  SandboxCircuitBreakerMiddleware   连续沙箱错误 ≥ 阈值 → jump_to=end（熔断）
10 ModelCallLimitMiddleware(50) + ToolCallLimitMiddleware(200)
```

**为什么 TaskState 必须在 ToolError 之前**（`task_state.py` 文件头亲口说明）：
LangChain 把中间件链成洋葱，列表靠前 = 更外层（`first = outermost`）。
只有在外层，`handler()` 返回后才能拿到 ToolError 归一化过的 `ToolMessage(status="error")`，
否则只能收到原始异常，等于把错误捕获重写一遍。

**沙箱三层保护**：预防（Health）→ 止损（Breaker）→ 兜底（ToolError）。

**子 Agent 中间件按档位复用**（`middleware_config.py` 注册表，档位按**任务形态**而非业务域划分）：

| 档位 | 组成 | 用在哪 |
|---|---|---|
| `analyst` | 摘要中间件 + `ModelCallLimit(50)` + `ToolCallLimit(200)` | 4 个只读域（长链路、多步、需压缩上下文） |
| `write` | `ModelCallLimit(20)` + `ToolCallLimit(50)` | 2 个写 Agent（短链路、每次写都要审批，尽早刹车） |

档位名写错 → 启动期 fail-fast；`analyst` 档的摘要中间件可用
`AB_DISABLE_TOOLS_SUMMARIZATION=1` 关掉，用于量化摘要压缩省了多少 token（默认关闭，行为不变）。

---

## 8. 沙箱与技能同步

### 8.1 per-user 沙箱生命周期

- **粒度**：每个 user 一个 OpenSandbox，同一 user 的多条 thread 共享（`ensure_sandbox_for_user`）。
- **五态取用顺序**（顺序本身就是设计）：
  1. 内存缓存命中 → `ping` 健康检查 → 失败则重建
  2. 注册表（MongoDB `sandbox_registry`，`user_id` 唯一索引）里有 `sandbox_id` → 重连 → 失败则重建
  3. 启动预热池 `_warm_reserve` 有货 → 认领给**没有历史记录的新用户**
  4. 都没有 → 全新建
  5. 不可达 → `_recreate_sandbox()`（新建 + 换 backend + 更新注册表）

  **第 2 步必须排在第 3 步之前**：否则每次启动预热的新沙箱会被优先认领，老用户永远拿不到自己上次那个
  （复用 ≈18s；新建要重跑 venv 并预装 5 个包 ≈235s）。
- **预热**：启动后在后台 task 里预热，不阻塞 HTTP 端口绑定；注册表已有绑定时跳过预热（避免浪费）。
- **关闭时不删沙箱、不清注册表**：沙箱由平台 2 小时超时自动回收，保留记录下次启动才能走复用路径。
- **热替换**：`SandboxBackendProxy` 显式代理 `SandboxBackendProtocol` 的**全部 18 个方法 + id**，
  `replace_backend()` 换内部实现而外部引用不失效 → 沙箱重建时 Agent graph 不用重建。
- **初始化**（`sandbox_setup.py`）：`_ensure_dirs`（如 `/analysis/temp`）→ `_seed_files`（播种技能）
  → `_create_venv`（`/opt/skills-venv`，预装 numpy / pandas / matplotlib / requests / beautifulsoup4）。
  沙箱镜像固定、资源 2C4G、超时 2 小时。

### 8.2 技能（9 份 SKILL.md）

| scope 目录 | 技能数 | 谁用 |
|---|---|---|
| `/skills/main/` | 1（skill-management） | 主 Agent |
| `/skills/inventory/` | 1（inventory-metrics-sop） | `inventory-logistics` |
| `/skills/sales/` | 1（sales-analysis-sop） | `sales-analyst` |
| `/skills/procurement/` | 4（procurement-analysis、supplier-price-urls、web-scraper、web-content-fetcher） | `procurement-analyst` / `procurement-order` |
| `/skills/master/` | 2（master-data-sop、master-data-maintain-sop） | `master-data` / `master-data-maintain` |

合计 **9 份**（与 `src/skills/**/SKILL.md` 实际文件数一致）。

- **渐进式加载**：技能不进上下文，Agent 按需 `ls` + `read_file`；`config.py` 里还打了框架补丁
  （`DEFAULT_READ_LIMIT` 100 → 10^6），把行数闸门让位给 token 闸门，避免技能文件被静默截断。
- **同步**：`SkillsSyncMiddleware` 在每个 agent 周期前按 MD5 哈希增量比对本地 `src/skills/` 与沙箱，
  有变化才上传，并插一条系统通知告诉 Agent 有新技能。
- **分配与持久化**：`assign_skill` 把 `/skills/main/{skill}` 复制到目标 scope 目录（scope 由
  `build_skill_scope_map()` 从 YAML 推导，不写死），再 `aput` 进 Store 持久化；
  `UserSkillsRestoreMiddleware` 在建图时把它们恢复到沙箱。
- **虚拟命名空间路由**：`CompositeBackend` 让 `/memories/`（按 user_id 命名空间）和
  `/persisted-skills/` 走 Store，其余路径走沙箱 —— 所以「Agent 说写好了、下载却说不存在」
  的坑在 `download_sandbox_file` 里显式传了 store。

### 8.3 图表能力（外部依赖，注意现状）

`src/mcp_server/` 下**没有自研图表模块**（`docs/chart_mcp_plan.md` 的方案尚未落地）。
可视化走**外部图表 MCP**：地址由 `ANALYSIS_MCP_URL` 注入（默认阿里云百炼托管的 AntV 图表服务），
传输方式 `sse` + `Authorization: Bearer $DASHSCOPE_API_KEY`。
`chart_generator.py` 把这些 `generate_*` 工具合并成 1 个 `generate_visualization`，
按 **25 种 `chart_type`** 路由；完整参数 schema 挪到沙箱
`/skills/procurement/chart_params.md` 按需 `read_file`，工具描述里只留紧凑速查表。

- 该外部 MCP 是**软依赖**：加载失败只在日志里 `WARN` 并关闭可视化能力，不影响启动。
- 因此 3 个只读域（`inventory-logistics` / `procurement-analyst` / `sales-analyst`）
  的工具与 prompt 里声明了 `generate_visualization`，**能否真的出图取决于该端点与 key 是否可用**。
- 早先使用的免费试用端点会过期（曾实测返回 410），所以现在地址走环境变量，换源只改 `.env` 一行。

---

## 9. 数据层

### 9.1 MySQL —— 业务数据的唯一真相

- 业务库 `motorparts_db`（库名是历史遗留，**内容已是快消零售域**），由 `erp_api/db.py` 用 `pymysql` 直连。
- 表（以 `db/migrations/` 与根目录 `motorparts_db.sql` 为准）：
  `product` / `product_supplier` / `product_price_log` / `supplier` / `customer` / `inventory` /
  `logistics` / `purchase_order` / `order_detail` / `sales_daily` / `sales_target` / `bonus_rule` /
  `meta_dict`，另有运维表 `_migration_log` 与遗留表 `user`。
- 迁移脚本：`db/migrations/` 下 **8 份**（`001` ~ `008`，幂等 + `_migration_log`），
  基线数据由根目录 `motorparts_db.sql` 还原；迁移前的全量备份本地留存，不随仓库发布。
  - `004` 做纯语义改名：把历史遗留的「零件」语义表名、列名、索引统一改成商品语义
    （`product` / `product_supplier` / `product_price_log` 及对应列），不改结构、不改数据。
  - `005` 重建业务内容为快消零售域；`006` 给 `sales_daily` 加渠道维度并新建 `sales_target` / `bonus_rule`；
    `007` 建 `meta_dict`（表用途 + 字段取值的中文语义，结构仍从 `information_schema` 实时读）；
    `008` 建长期记忆库 `agent_state.langgraph_store`。
- 数据规模（README 记载 + 已与 `005` 种子脚本核对一致）：
  75 个 SKU、33 家供应商、40 家客户（KA 商超 / 经销商 / 电商）、`sales_daily` 2250 行（75 × 30 天）、
  178 张采购单、626 条订单明细、6 个区域 DC（华东-上海 / 华南-广州 / 华北-天津 / 华中-武汉 / 西南-成都 / 东北-沈阳）。
  销量里注入了确定性异常：断货归零 6 个 SKU × 4 天 = **24 条**，促销尖峰 10 个 SKU × 3 天 = **30 条**；
  库存量由「实际日均销量 × 目标可售天数」推导，与销量自洽。
- `agent_state` 库（`langgraph_store` 表）：**长期记忆落 MySQL** ——
  承载 `/memories/{user_id}/preferences.md` 与 `/persisted-skills/`。
  `MySQLStore` 是 LangGraph `BaseStore` 的实现，只实现 `batch` / `abatch` 即可
  （基类的 `aget/aput/asearch/adelete` 全部委托给 `batch`）；`PyMySQL` 是同步驱动，
  `abatch` 用 `asyncio.to_thread` 丢线程池，每线程一连接。

### 9.2 MongoDB —— 会话状态

库 `langchain_db`，当前承担 4 类数据（`agent/config.py`、`api_view/agent_loader.py`、
`middlewares/task_state.py`、`backends/sandbox_manager.py`）：

| 集合 | 用途 | 谁写 |
|---|---|---|
| `checkpoints`（+ checkpoint 系列集合） | LangGraph checkpointer：**HITL 中断状态持久化 + 跨重启恢复** | 框架每步自动写 |
| `session_tasks` | **四态任务账本**：一次子 Agent 委派 = 一条记录 | `TaskStateMiddleware` + `TaskLedger` |
| `session_display_messages` | 前端展示消息（**含子 Agent 消息**，checkpoint 里没有） | `agent_loader.save_display_messages` |
| `sandbox_registry` | per-user 沙箱绑定（`user_id` 唯一索引） | `sandbox_manager` |

访问方式上，`agent_loader.py` 有 5 处**绕过 checkpointer 直读 checkpoint 集合**
（列会话 `distinct("thread_id")`、取会话最后更新时间 —— 依赖 MongoDB ObjectId 内嵌时间戳、
删会话、展示消息读写）。

### 9.3 存储层迁移（进行中）

方案见 [`STORAGE_MIGRATION.md`](STORAGE_MIGRATION.md)，判据是**数据的可重建性**：
长期记忆 → MySQL（丢了用户立刻能感知、要能备份审计），会话状态 → Redis（丢了可重建），MongoDB 退场。

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | 长期记忆 `InMemoryStore` → `MySQLStore`（`agent_state` 库） | ✅ 已完成（含跨进程持久性验证） |
| Phase 2 | 任务账本 + 展示消息 + 会话索引 → Redis | 待做 |
| Phase 3 | 沙箱注册表 → MySQL | 待做 |
| Phase 4 | checkpointer → Redis（**牵连 `agent_loader.py` 5 处直读库代码，不可拆**） | 待做 |

> 当前代码里 **Redis 只出现在注释中，尚无任何运行时引用**；会话状态仍在 MongoDB。
> 四态账本的设计已为换库留好接口（`TaskLedger` 依赖注入 collection，用的全是通用 CRUD）。

**任务状态机**（`middlewares/task_state.py`）：

```
pending --> in_progress --> completed
                └--> failed --> in_progress（重试通道）
```

`TRANSITIONS` 白名单校验，非法迁移抛 `IllegalTransition`；终态 `completed` 不可翻案。
为什么不直接用框架的 `write_todos`：Todo 没有 id、全量覆盖、框架鼓励删掉「不再相关」的任务、
失败时要求保持 `in_progress` —— 它回答不了「哪些委派失败了、失败几次」。

### 9.4 三层记忆与展示消息

| 层 | 存储 | 谁写 | 生命周期 |
|---|---|---|---|
| 短期对话状态 | MongoDB checkpointer | LangGraph 每步自动写 | 跟 `thread_id` |
| 长期用户记忆 | `/memories/{user_id}/preferences.md` → StoreBackend → **MySQL** | `MemoryUpdateMiddleware`（LLM 抽取，带触发词守卫） | 跟 `user_id`，跨进程可持久 |
| 任务账本 | MongoDB `session_tasks` | `TaskStateMiddleware` | 跟 `thread_id`，删会话时一起清 |
| 前端展示消息 | MongoDB `session_display_messages` | `agent_loader` | 跟 `thread_id` |

**为什么展示消息单独存一份**：checkpoint 里不含子 Agent 的消息，前端刷新要看到完整过程，
所以流式结束时另存一份；`GET /api/history/{thread_id}/messages` 优先读它，读不到才回退 checkpoint。

---

## 10. 目录结构

```
ERP_OPENCLAW/
├── start_web.py            一键启动（ERP API → MCP → 后端 → 前端，带健康检查与端口清理）
├── README.md               现状速览（本文与之的差异见 §13）
├── src/
│   ├── agent/                     Agent 主体
│   │   ├── main_agent.py          ★ 组装总入口：预计算 + Graph Factory
│   │   ├── middleware_config.py   子 Agent 中间件档位注册表（analyst / write）
│   │   ├── config.py              ★ 模型 / 沙箱 / 路径 / Mongo checkpointer / MySQL store（均懒加载）
│   │   ├── env_utils.py           .env 读取
│   │   ├── schema.py              上下文 dataclass + API/SSE 模型
│   │   ├── backends/              custom_opensandbox（协议实现）、sandbox_proxy（热替换）、
│   │   │                          sandbox_manager（per-user 生命周期 + 预热）、sandbox_setup（建目录/venv/播种）
│   │   ├── middlewares/           9 个中间件（见 §7）
│   │   ├── subagents/             loader.py（YAML 加载 + 工具解析 + 校验网）、configs/*.yaml（6 份）
│   │   ├── store/                 mysql_store.py（BaseStore 的 MySQL 实现）
│   │   ├── memory/                prompts.py（主 Agent system_prompt）、AGENTS.md（通用准则，随图上传到沙箱）
│   │   └── tools/                 mcp_client、chart_generator、assign_skill、
│   │                              download_sandbox_file、hitl_tools、web_search
│   ├── api_view/                  后端 Web 层
│   │   ├── web_main.py            FastAPI app + lifespan + CORS
│   │   ├── web_config.py          配置转发（Mongo 配置的唯一来源是 agent/config.py）
│   │   ├── agent_loader.py        ★ 单例：Mongo + 沙箱 + 预计算 + per-user 建图
│   │   └── api/chat.py            ★ SSE 流式 + 中断恢复；api/history.py 会话读写
│   ├── mcp_server/                FastMCP 服务（HTTP 转发壳）
│   │   ├── server_main.py         注册 8 个实体分组
│   │   ├── http_base.py           模块级懒加载 httpx.AsyncClient
│   │   ├── server_config.py       端口 8099、ERP_API_BASE_URL
│   │   └── tools/                 8 个 *_tools.py（23 个工具）
│   ├── erp_api/                   Python ERP REST 层（main.py + db.py，24 个端点）
│   ├── skills/                    9 份 SKILL.md，按 scope 分目录（main / inventory / sales / procurement / master）
│   └── test/                      工具与 Agent 验证脚本（validate_configs.py / check_tool_refs.py /
│                                  eval_routing.py / test_all_tools.py / test_mcp.py / agent_test.py / api_test.py）
├── frontend/                      Vue3 + Vite（api/chat.js、api/history.js + 7 个组件）
├── db/migrations/                 8 份幂等迁移脚本
├── motorparts_db.sql              基线数据导出（本地还原用）
└── docs/                          设计文档（索引见 docs/README.md）
```

---

## 11. 关键设计取舍

1. **为什么 Graph Factory**：MCP 连接 + YAML 解析慢且与用户无关 → 启动预计算；
   沙箱必须 per-user → 每请求重建。两者用 `PrecomputedContext` 解耦。
2. **为什么用 Proxy 包沙箱**：沙箱会挂、会重建，而 Agent graph 引用了 backend。
   Proxy 让「引用稳定、实现可换」，沙箱重建时不必重建图。
3. **为什么四态账本而不是框架的 todo**：`write_todos` 是给模型看的草稿纸（无 id、全量覆盖、
   不允许记失败），回答不了「哪些委派失败了、失败几次」。
4. **为什么中间件顺序敏感**：洋葱模型，靠前 = 外层；TaskState 必须在 ToolError 外层，
   才能读到归一化后的错误消息。
5. **为什么工具出错不抛异常**：`ToolErrorMiddleware` 把异常转成 `ToolMessage(status="error")`，
   让模型看到错误自我纠正，而不是让整轮对话崩掉。
6. **为什么把多个图表工具合并成 1 个**：工具数爆炸会吃满上下文。
   合并入口 + 长参数说明挪到沙箱文件按需 `read_file`，是典型的按需加载取舍。
7. **为什么写操作要 HITL**：写操作不可逆。数据不全先中断要人补（`request_order_info`），
   执行前再要人审批；且权限边界放在**工具集**上，不靠 prompt 约束。
8. **为什么用 YAML 定义子 Agent**：prompt / 工具 / 技能是配置不是代码，新增领域不动 Python；
   代价是必须有启动期校验网兜住配置错误。
9. **为什么沙箱三层保护**：健康探测（预防）→ 熔断（止损）→ 错误兜底（不影响主流程）。
10. **为什么展示消息单独存一份**：checkpoint 里没有子 Agent 的消息，前端刷新要看到完整过程。
11. **为什么依赖全部懒加载**：Mongo/MySQL 构造放在模块顶层会让「依赖不可达」表现成
    `import` 直接崩 + 报错不可读，服务根本走不到启动校验网；改成
    `get_checkpointer()` / `get_store()` 懒加载后，启动期只依赖配置本身，
    配置校验不依赖 VM。
12. **为什么写工具要两张网**：只用显式登记，新增写工具忘了登记就静默失守；
    只用命名反查，命名不符的就漏判 —— 两张互补。

---

## 12. 已知限制（以代码为准）

- **图表能力依赖外部服务**：`src/mcp_server/` 下无自研图表模块，可视化全靠 `ANALYSIS_MCP_URL`
  指向的外部 MCP（默认百炼 AntV，需 `DASHSCOPE_API_KEY`）。它挂了只降级不报错，
  所以「prompt 里教你用 `generate_visualization`」与实际可用性可能不一致。
- **Agent 全链路依赖 VM**：MongoDB 与 OpenSandbox 都在 `127.0.0.1`；未开 VM 时只有
  ERP API / MCP / 数据层可跑。但**配置校验与 `validate_configs.py` 不依赖 VM**。
- **存储层迁移未完成**：MongoDB 仍在承担 checkpointer 等 4 类数据（见 §9.3），
  Phase 2~4 待做；切换 checkpointer 必须同批改完 `agent_loader.py` 的 5 处直读库代码。
- **单进程假设**：Agent graph 引用、沙箱内存缓存都在进程内，`sandbox_registry` 亦为进程协作状态
  → 目前不能多 worker 部署（长期记忆已落 MySQL，不受此限）。
- **CORS 仍为 `allow_origins=["*"]` + `allow_credentials=True`**（`web_main.py`）。
- **默认凭据仍是代码里的兜底值**（MySQL / Mongo 连接串），虽已全部支持环境变量覆盖。

---

## 13. 本文与 README 的口径差异（本文以代码为准）

| 项 | README 写法 | 代码实际 | 依据 |
|---|---|---|---|
| Agent 后端端口 | 8090 | **8091**（8090 被一个无法回收的旧进程长期占用） | `start_web.py`、`frontend/vite.config.js`、`src/test/eval_routing.py` |
| MCP 分组模块数 | 9 个模块 / 23 个工具 | **8 个实体分组模块 / 23 个工具**（工具数一致） | `mcp_server/server_main.py` 注册的 8 个 `register_*` |
| 迁移脚本数 | 7 个 | **8 份**（多出 `008_agent_state_store.sql`，是存储迁移 Phase 1 的交付物） | `db/migrations/` |
| 图表 MCP 端点 | 「指向的魔搭 analysis MCP 实测 410 已死」 | 指向的已是**阿里云百炼 AntV**（`ANALYSIS_MCP_URL` 可覆盖 + Bearer 鉴权），是否可用取决于环境 | `agent/tools/mcp_client.py` |
| 数据模型注释 | —— | `005` 脚本内注释仍写「67 个 SKU / product 67 行」，实际已扩到 **75 行**（README 的 75 是正确的） | `db/migrations/005_p2_retail_seed.sql` |
| 个别脚本注释 | —— | `selfcheck_deps.py`、`mcp_server/server_config.py`、`erp_api/db.py` 的注释里仍写后端 8090 | 同上 |

> 结论：README 的结构性描述（6 个子 Agent、读写分离、Graph Factory、fail-fast 校验网）
> 与代码一致，可直接采信；需要更新的只是上表中的数字与端点。

---

## 14. 建议阅读顺序

1. `README.md` —— 先拿现状速览（把 §13 的差异记在心里）
2. `src/api_view/web_main.py` → `agent_loader.py` —— 入口、lifespan 与 per-user 建图
3. `src/agent/main_agent.py` —— 先看 `precompute_agent_context()`，再看 `create_main_agent()` 的 Phase 1→9
4. `src/agent/config.py` —— 模型 / 沙箱 / 路径 / checkpointer / store 都在这里（注意懒加载）
5. `src/agent/subagents/configs/*.yaml` + `subagents/loader.py` —— 子 Agent 是怎么被「配置」出来的、校验网挡什么
6. `src/agent/middlewares/task_state.py` —— 读文件头文档字符串，再看 `TaskStateMiddleware`
7. `src/api_view/api/chat.py` —— 只看 `stream_chat_response()`（其余是工具函数）
8. `src/agent/tools/mcp_client.py` + `src/mcp_server/server_main.py` —— 搞懂 MCP 两端

读的时候问自己三个问题：**它什么时候被创建？谁持有它的引用？它失败时会影响谁？**
