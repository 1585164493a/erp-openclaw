# 存储层改造方案：退掉 MongoDB，长期记忆 → MySQL，短期记忆 → Redis

> 决策日期：**2026-09-18**（用户明确拍板：「长期记忆放 MySQL，短期记忆放 Redis，MongoDB 不用了」）
> 本文件取代 `REFACTOR_PLAN.md` §4.7 的旧决策（旧版本写的是「用 RedisStore 一次换掉 InMemoryStore 和 MongoDBSaver」，**已作废**）
> 行号基于 2026-09-18 代码状态；按符号名定位更稳。

---

## 0. 为什么这么分（分层判据）

判据只有一条 —— **「这份数据丢了会怎样」**：

| 丢了会怎样 | 放哪 | 项目里的例子 |
|---|---|---|
| 用户无感，能重建 | **Redis**（内存，容量受内存限制） | 展示消息、任务台账、会话索引 |
| 用户长期感知，**重建不了** | **MySQL**（binlog / 独立备份 / 权限可收） | 用户偏好、持久化技能 |
| 业务真相，要对账审计 | **MySQL，绝对不动** | 订单、库存、价格、供应商 |

**关键论证（为什么长期记忆不放 Redis）**：

> 判据是**数据的可重建性**，不是「Redis 会不会丢数据」。
>
> 用户偏好这类长期记忆，特点是**量小、变化慢、丢了用户立刻能感知**（下次对话个性化就没了），
> 而且要**能单独备份、能审计、能追责**。Redis 是内存数据库：
> ① 数据常驻内存，容量受物理内存限制；② 原生没有 `binlog` 这类可审计的变更日志。
> 所以长期记忆放 MySQL，**Redis 只放会话状态 —— 那部分丢了可以重建**。

⚠️ **不要用「Redis 会 evict 数据」当论据（实测不成立）**：

VM 上这台 Redis 的实配是

| 配置 | 实测值 | 含义 |
|---|---|---|
| `maxmemory` | `0` | **不限内存**，根本不会触发淘汰 |
| `maxmemory-policy` | `noeviction` | 即使到上限也是**拒绝写入**，而不是删旧数据 |
| `appendonly` | `no` | RDB 快照（`save 3600 1 300 100 60 10000`） |

**`noeviction` 恰恰是最安全的一档**（宁可写失败也不丢已存数据）。
所以正确的论据是**可重建性 + 内存态定位 + 缺审计日志**，说「会被淘汰」会被懂行的人当场问倒。

> **待办**：既然会话状态要落 Redis，建议开 AOF（`appendonly yes` + `appendfsync everysec`）
> 把「最多丢 1 秒」写进设计说明 —— 比只靠 RDB（最多丢 1 小时）更符合"会话状态"的语义。
> 这是 Phase 2 的一部分。

---

## 1. 现状：MongoDB 实际承担的 5 件事

> ⚠️ 旧文档写「3 件事」，是**漏数了**。实际 grep 结果是 5 处，其中 2 处是「绕过 checkpointer 直读库」。

| # | 用途 | 代码位置 | 迁移目标 | 风险 |
|---|---|---|---|---|
| ① | 会话 checkpointer | `agent/config.py` `get_checkpointer()` | Redis | 🔴 高 |
| ② | **绕过 checkpointer 直读 checkpoint 集合** | `api_view/agent_loader.py` 5 处 | Redis（需重写） | 🔴 高 |
| ③ | 任务台账 `session_tasks` | `middlewares/task_state.py` | Redis | 🟢 低 |
| ④ | 展示消息 `session_display_messages` | `api_view/agent_loader.py` | Redis | 🟢 低 |
| ⑤ | 沙箱注册表 `sandbox_registry` | `backends/sandbox_manager.py` | MySQL | 🟡 中 |

### ② 的具体清单（换 checkpointer 时必须同批改完，否则历史接口直接挂）

```
agent_loader.py:203   collection.distinct("thread_id")                ← 列全部会话
agent_loader.py:212   collection.find_one(..., sort=[("_id", -1)])    ← 取会话最后更新时间  ★硬骨头
agent_loader.py:234   collection.delete_many({"thread_id": ...})      ← 删会话（级联删 3 个集合）
agent_loader.py:286   display_collection.insert_many(docs)            ← ④ 展示消息
agent_loader.py:301   display_collection.find(...).sort("index", 1)   ← ④ 展示消息
```

**★ `agent_loader.py:212` 是全项目最难换的一处**：

```python
latest_doc = collection.find_one({"thread_id": thread_id}, sort=[("_id", -1)])
if "_id" in latest_doc and hasattr(latest_doc["_id"], 'generation_time'):
    return latest_doc["_id"].generation_time      # ← 靠 MongoDB ObjectId 内嵌的 4 字节时间戳
```

它**依赖 MongoDB 独有的 ObjectId 内嵌时间戳**来排会话顺序。**Redis 没有任何等价物。**
→ 必须改成：写 checkpoint 时**自己另存一个 `updated_at` 字段**，排序读那个字段。

同理 `distinct("thread_id")` 是 Mongo 的集合操作。Redis 里没有等价物，
→ 必须改成：**额外维护一个 Set 存「这个用户有哪些 thread_id」**（否则只能 `SCAN` 全库）。

**结论：换 checkpointer 不是「改一个类」，而是要补一套索引结构 + 重写 5 处读库代码。**

---

## 2. 分阶段执行（每阶段结束都必须是可用状态）

> 核心原则：**绝不一次性重写**。每个 Phase 做完都能启动、能用、能回滚。

### Phase 1 — 长期记忆落 MySQL ✅ **已完成（2026-09-20，已验证）**

**为什么先做**：
- 这是**当前唯一的真 bug** —— `config.py` 里 `STORE = InMemoryStore()` 是**进程内存字典**，
  重启进程 → 所有用户偏好 + 所有持久化技能**全部归零**。长期记忆此前**连库都没进**。
- 它**兑现了设计里承诺的「持久化记忆」**：「通过 CompositeBackend 透明路由至沙箱或后端 Store」。
- 改动范围小、无 Mongo 耦合、调用点全是 `await` 形式。

#### 交付物

| 文件 | 说明 |
|---|---|
| `db/migrations/008_agent_state_store.sql` | 建 `agent_state` 库 + `langgraph_store` 表 + `_migration_log`。**幂等**（已实测重复执行无副作用） |
| `src/agent/store/mysql_store.py` | **`MySQLStore`** —— LangGraph `BaseStore` 的 MySQL 实现（新建，17.7KB） |
| `src/agent/store/__init__.py` | 包初始化 |
| `src/agent/config.py` | `STORE = InMemoryStore()` → `get_store()`（懒加载单例） |
| `src/agent/main_agent.py` | 3 处引用改为 `get_store()` / `from ... import get_store` |
| `src/agent/tools/assign_skill.py` | docstring 里的 `config.STORE` → `config.get_store()` |

#### 关键设计点：只实现 `batch` / `abatch` 就够

`BaseStore` 的抽象方法**只有 `batch` 和 `abatch`**；其余全部是基类默认实现，
且**都委托给 batch**（实测源码）：

```
aget(...)    -> await self.abatch([GetOp(...)])[0]
aput(...)    -> await self.abatch([PutOp(...)])[0]
asearch(...) -> await self.abatch([SearchOp(...)])[0]
```

所以实现 `batch + abatch` = 同时得到 `aget/aput/asearch/adelete`。
而项目对 STORE 的全部用法正好只有 `aget` / `aput` / `asearch` —— **完全覆盖**。

`PyMySQL` 是同步驱动，`abatch` 用 `asyncio.to_thread` 丢线程池，避免阻塞事件循环；
每个线程一个连接（`threading.local`），因为 `pymysql.Connection` **不是线程安全的**。

#### 表结构

```sql
CREATE TABLE `langgraph_store` (
  `namespace` varchar(255) NOT NULL,   -- ("user_001",) → 'user_001'
  `key`       varchar(512) NOT NULL,   -- '/user_001/preferences.md'
  `value`     json         NOT NULL,   -- {"content":[...行...], "created_at":..., "modified_at":...}
  `created_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  `expires_at` datetime(6) NULL,       -- TTL（暂未使用）
  PRIMARY KEY (`namespace`, `key`)
)
```

**namespace 元组 → SQL 字符串**：用 `/` 连接，取回时 split。
⚠️ 必须特判空串 —— `"".split("/")` 会得到 `[""]`（即 `("",)` 而非 `()`），这是易错点。

**落点**：独立 `agent_state` 库，**不混进业务库 `motorparts_db`**。
理由：访问模式不同（业务表按条件查 / 本表按主键取整篇正文）、权限边界不同、备份粒度不同。

#### 验收结果（实测，全部通过）

| 验收项 | 结果 |
|---|---|
| `import agent.main_agent` | ✅ 通过（接线无误） |
| `MySQLStore` 接口测试 25 项 | ✅ **25/25**（aput/aget 往返、覆盖写、asearch 前缀搜索含层级、limit、adelete、value=None 删除、空 namespace 特判、缺 key 返回 None） |
| **跨进程持久性**（进程 A 写 → 进程 B 读） | ✅ 成功读回 —— **这正是 `InMemoryStore` 做不到的** |
| `MemoryUpdateMiddleware` 完整写入路径 | ✅ **14/14**（首次建文件、二次运行**合并而非覆盖**、旧供应商保留、区块不重复追加） |
| 直接查 MySQL 确认数据落库 | ✅ JSON 结构正确：`content` 是**行数组** |
| migration 脚本 | ✅ 5 条语句全 OK、**幂等**、未破坏既有数据 |
| 业务库未受影响 | ✅ `motorparts_db.product` 仍 75 行 |

> **一个易错点（测试中发现）**：`MemoryUpdateMiddleware` 有守卫
> `_is_meaningful_erp_exchange()` —— 用户消息必须命中 `_TRIGGER_KEYWORDS`
> （供应商/采购/库存/报价/分析…），否则**正确地跳过本次更新**。
> 写测试用例时消息措辞要带这些词，否则会误判为"写库失败"。

#### 遗留（不影响本 Phase 完成）

- `MySQLStore.close()` 已实现但未接入进程退出钩子。当前由进程退出回收，无实际影响。
- `asearch` 的 `query` 参数（向量检索）**未实现**，因为项目没有建索引也从不传 query。
  将来若要语义检索，需另建 embedding 表。
- 端到端（真实对话跑一轮）尚未做 —— 需要重启后端 + OpenSandbox 服务。

---

### Phase 2 — 会话状态落 Redis（③④）

**为什么这块是 Redis 的正当职责**：任务台账和展示消息都是**会话级**的
（`thread_id` 关联）、有状态机迁移、会话删了就该清掉 —— 天生短周期、高频读。

**改动风险低的关键证据**：`task_state.py` 的 `TaskLedger` **本来就是为换库设计的**：

```python
# task_state.py:123
def __init__(self, collection=None, collection_name: str = TASK_COLLECTION_NAME):
    self._collection = collection      # ← 依赖注入，外面传什么就用什么
```

它用的 `insert_one` / `find_one` / `update_one` / `find().sort().limit()`
**全是通用 CRUD，一个 Mongo 特有 API 都没有**。
换 Redis = 改这几个方法的实现，`create/transition/list_by_thread/get`
四个业务方法的**签名与语义完全不动**。

**改什么**：

| 文件 | 改什么 |
|---|---|
| `src/agent/config.py` | 新增 `REDIS_URL`（`os.getenv()`）、懒加载 `get_redis()` |
| `src/agent/middlewares/task_state.py` | `TaskLedger` 底层 collection → Redis 实现 |
| `src/api_view/agent_loader.py` | `save_display_messages` / `get_display_messages` / `delete_session` |
| `requirements.txt` | 确认 `redis` / `redisvl` 版本（已装） |

**Redis 数据结构（草案）**：

```
任务台账：  HSET  task:{thread_id}  {tool_call_id}  <json>       # 每任务一个 field
           SADD  thread:{thread_id}:task_ids  <tool_call_id>      # 索引
展示消息：  RPUSH thread:{thread_id}:display  <json>              # 天然有序，省掉 index 字段
会话索引：  SADD  user:{user_id}:threads  <thread_id>            # 替代 Mongo distinct()
```

**验收**：
- [ ] 任务台账建/迁移/查询结果与现在一致
- [ ] 展示消息顺序正确、会话删除时一并清理
- [ ] 会话列表接口能列出全部 thread_id

---

### Phase 3 — 沙箱注册表落 MySQL（⑤）

`backends/sandbox_manager.py` 的 `sandbox_registry` 是**唯一的索引 `user_id` unique**。
它是基础设施状态、量小、要跨重启存活 → MySQL 合适。

**注意**：`sandbox_manager.initialize(mongo_client)` 的签名要改，`shutdown()` 里
`_mongo_client.close()` 也要跟着改。

---

### Phase 4 — checkpointer 落 Redis（①②，最后做，且不可拆）

**为什么放最后**：
- 它牵连 ② 的 5 处读库代码，必须**同一个 PR 做完**，否则历史的接口直接挂
- 需要写一次性迁移脚本，否则**历史会话读不出来**（旧 checkpoint 留在 Mongo）
- 涉及 sync→async API 变更
- 风险最高，收益却只是「会话状态跑 Redis」—— 而 Phase 2 已经把这件事立住了

**技术要点**：

1. **必须用 Redis Stack（带 RediSearch），不是普通 Redis** ——
   `langgraph-checkpoint-redis` → `redisvl` → 需要 `FT.CREATE` / `FT.SEARCH`。
   （VM 上已装 Redis Stack 7.2.5，**待开机后复核模块是否齐全**）
2. **异步化**：`AsyncRedisSaver` 需要显式初始化，`get_checkpointer()` 要变 `async def`：

   ```python
   from langgraph.checkpoint.redis.aio import AsyncRedisSaver
   _saver = AsyncRedisSaver(redis_url=...)
   await _saver.asetup()      # ← 建 RediSearch 索引，必须先跑，否则一用就报错
   ```

   调用点（`agent_loader.py:191` 等）本来就是 `await` 形式，天然兼容；
   但「首次初始化」要防并发重复 `asetup()`。
3. **`agent_loader.py:212` 的 ObjectId 时间戳逻辑必须重写**（见 §1 的 ★）
4. **HITL 恢复链路必须重测** —— 「HITL 中断后服务重启还能恢复」的依据就在这条链路

**验收**：
- [ ] 跨会话能读回历史消息
- [ ] HITL 中断 → 重启服务 → 恢复，链路完整
- [ ] 会话列表 / 会话时间 / 删除会话三个接口正常
- [ ] `agent_loader.py` 里**不再有任何 Mongo 引用**

---

## 3. 执行前的环境状态（2026-09-20 实测复核）

| # | 项 | 实测结果 | 结论 |
|---|---|---|---|
| 1 | **VM** | 已开机；`6379` / `27017` / `22` 可连 | ✅ 可用 |
| 2 | **Redis Stack 能力** | 7.2.5，`modules=['ReJSON','RedisCompat','bf','redisgears_2','search','timeseries']`，`FT._LIST` 与 `JSON.SET` 均 OK；当前 `DBSIZE=0` | ✅ **Phase 4 可行**（RediSearch 在） |
| 3 | **Redis 持久化** | `maxmemory=0`、`policy=noeviction`、`appendonly=no`、`save 3600 1 300 100 60 10000` | ⚠️ 见 §0 —— 建议 Phase 2 开 AOF |
| 4 | **MongoDB 数据基线** | 见下表（迁移前后要对账） | ✅ 已记录 |
| 5 | **本地 `.venv` 环境** | **完好**。20/20 关键导入 OK；`import agent.main_agent` 通过 | ✅ 可用 |
| 6 | **MySQL（Phase 1 落点）** | 🔴 **未运行**。VM 上无 mysqld；本机 `MySQL80` 服务 Stopped，启动需管理员权限 | **阻塞 Phase 1** |
| 7 | **OpenSandbox 服务** | 🔴 VM 上未运行（需手动拉起） | 阻塞端到端联调 |
| 8 | **mock-quotes 站点** | 🔴 VM 上未运行（`/opt/mock-quotes` 目录还在） | 影响 web-scraper 技能验证 |

**MongoDB 迁移前基线（`langchain_db`）**：

| 集合 | 文档数 | 去向 |
|---|---|---|
| `checkpoints` | **2452** | → Redis（Phase 4） |
| `checkpoint_writes` | **4188** | → Redis（Phase 4） |
| `checkpoint_blobs` | 0 | — |
| `session_display_messages` | **525** | → Redis（Phase 2） |
| `session_tasks` | **4** | → Redis（Phase 2） |
| `sandbox_registry` | **1** | → MySQL（Phase 3） |

> ⚠️ **`checkpoints` 有 2452 条、`checkpoint_writes` 有 4188 条** —— 这不是少量测试数据。
> Phase 4 换 checkpointer 前**必须**先备份 + 评估是否要写迁移脚本，否则这 6600 条历史会话状态就断了。

### 已排除的假警报（记录在案，避免重复踩）

> 曾一度判定「`.venv` 半损坏：43 个包只有 `dist-info` 没有包目录」—— **该结论是错的**，
> 原因是**审计脚本用错了模块名**。
>
> `langgraph-checkpoint` / `-mongodb` / `-redis` 这三个发行版**并不提供
> `langgraph_checkpoint_redis` 这样的顶层模块**，而是把代码装进 `langgraph.*` 命名空间
> （见 `pip show -f` 的 RECORD：`langgraph\checkpoint\redis\__init__.py`）。
>
> **正确的导入路径**：
>
> | 发行版 | 真实导入路径 |
> |---|---|
> | `langgraph-checkpoint` | `langgraph.checkpoint` |
> | `langgraph-checkpoint-mongodb` | `langgraph.checkpoint.mongodb` → `MongoDBSaver` |
> | `langgraph-checkpoint-redis` | `langgraph.checkpoint.redis.aio` → `AsyncRedisSaver` |
> | — | `langgraph.store.redis` → `RedisStore` |
>
> **教训**：不要按包名猜模块名，用 `pip show -f <pkg>` 看 RECORD，或 `importlib.util.find_spec` 实测。

### 本机 pip 的特殊情况（重要，影响以后装依赖）

在本机直接跑 `pip install <包名>` 会**卡在在线索引解析**（实测：`GET /simple/six/` 返回 304 后
再无任何输出，进程空转；直连 PyPI 与清华镜像都卡）。但——

- `httpx` 能正常抓取索引页与下载 wheel（HTTP 200）
- `pip install --no-index --find-links <本地目录> <包>` **完全正常**

→ **可用的 Workaround**：用 httpx 下 wheel 到本地目录，再让 pip 离线装。
另外 `pip install --force-reinstall` 在本机会因**卸载旧包时删临时目录被拒**（`Errno 13`）
而**拖垮整批安装**，请避免使用。
（详见 `REFACTOR_PLAN.md` 变更记录）

---

## 4. 依赖变更清单

| 操作 | 包 | 说明 |
|---|---|---|
| ✅ 已就绪 | `langgraph-checkpoint-redis` | **包实体已在 `.venv`**（导入路径 `langgraph.checkpoint.redis`），Phase 4 无需新装 |
| ✅ 已就绪 | `pymysql` | Phase 1 必需（已装 1.2.0，ERP API 已在用） |
| ✅ 已就绪 | `redis` / `redisvl` | 会话状态层所需（已装 7.4.0 / 0.16.0） |
| ➖ 待移除 | `langgraph-checkpoint-mongodb==0.2.2` | **Phase 4 完成后**才移除 |
| ➖ 待移除 | `langchain-mongodb==0.7.0` | 同上 |
| ➖ 待移除 | `pymongo==4.15.5` | 同上 |

> ⚠️ `requirements.txt:7-9` 有一条**锁版本注释**：
> 「MongoDB checkpointer 三件套必须锁定（`langgraph-checkpoint-mongodb==0.2.2` +
> `langchain-mongodb==0.7.0` + `pymongo==4.15.5`）；新版本会强制升 `langchain-core` 到 1.6.2，
> 破坏 `deepagents 0.4.12` / `langgraph 1.1.3` 的可用组合，请勿单独升级。」
> **移除时要把这段注释一起改掉**，并重新验证 `langchain-core` 版本没被带上去。

---

## 5. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 历史 checkpoint 丢失 | Phase 4 前先跑一次 MongoDB 备份 + 写迁移脚本；或明确接受断代并记录。**注意基线是 2452 + 4188 条** |
| `.venv` 环境异常导致改动无法验证 | **已实测完好**（20/20 导入 OK、`import agent.main_agent` 通过）。但本机 pip 在线安装会卡，装依赖须走下节 workaround |
| Redis 被 evict 导致会话丢失 | 实测 `noeviction`，不会 evict；但 `appendonly=no`，崩溃最多丢 1 小时 → **建议开 AOF** |
| 一次性重写导致全盘不可用 | **严格按 Phase 顺序**，每阶段结束都可用、可回滚 |
| MySQL 表混进 ERP 业务库 | 单开 `agent_state` 库 |

**回滚方式**：每 Phase 独立提交，Mongo 数据在 Phase 4 前**不删**。

---

## 6. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-18 | 建档。用户拍板「长期记忆 MySQL / 短期记忆 Redis / MongoDB 退场」。作废 `REFACTOR_PLAN.md` §4.7 旧决策（原写「RedisStore 一次换掉两个」）。修正旧文档「MongoDB 承担 3 件事」为**实际 5 处**（补上 `agent_loader.py` 直读 checkpoint 的 5 处调用）。记录 VM 关机 + `.venv` 包实体缺失两个硬阻塞。 |
| 2026-09-20 | **VM 开机后复核，修正两处先前结论**：① 实测 Redis Stack 模块齐全（`search` + `ReJSON`），Phase 4 可行；② **撤回「`.venv` 半损坏 / 43 个包缺失」的错误结论** —— 那是审计脚本用错模块名所致（这些发行版装在 `langgraph.*` 命名空间下），环境实测完好。补充：Redis 实配为 `noeviction`+`appendonly=no`，**§0 的「会被 evict」论据已改正**；新增 MongoDB 迁移前数据基线（checkpoints 2452 / writes 4188 / display 525 / tasks 4 / registry 1）；记录本机 pip 在线安装卡的 workaround。**当前唯一阻塞 Phase 1 的是 MySQL 未运行**。 |
| 2026-09-20 | **Phase 1 完成并验证**。MySQL 以进程方式启动（服务需管理员权限，改用 `mysqld --console` 直接跑）。交付 `MySQLStore`（BaseStore 的 MySQL 实现）、`get_store()` 懒加载、migration 008。验收全绿：接口测试 25/25、middleware 写入路径 14/14、**跨进程持久性通过**（这是 `InMemoryStore` 做不到的）、migration 幂等。长期记忆首次真正落库。 |
