# 主数据写入能力 —— 设计与实现记录

> 日期：2026-09-18
> 起因：原先系统里**只有采购能下单**（`order_create` / `order_update` 是仅有的两个写工具）。
> 本次补上「主数据建档」这一类写操作：供应商准入、客户开户、新商品建档。

---

## 1. 为什么加这个（不是堆功能）

### 1.1 业务上它比下单更需要审批

真实企业里，主数据新增恰恰是**审批最重**的流程：

| 场景 | 真实审批环节 |
|---|---|
| 供应商准入 | 资质审核、财务/合规审查 |
| 客户开户 | 信用评估、账期与折扣等级核定 |
| 新 SKU 建档 | 定价审批、品类归属确认 |

采购下单是**交易型**操作（高频、金额明确）；主数据建档是**主数据型**操作（低频、影响面广、
错了污染全链路 —— 一条错误的供应商档案会影响后续所有比价、订单、结算）。
所以补上它之后，HITL 的故事更完整，而不是更牵强。

### 1.2 架构上它把「HITL 是特例」变成「HITL 是模式」

改动前，项目里其实**已经存在一读一写的模式**，只是没人点出来：

```
采购域：  procurement-analyst（读，比价/趋势） + procurement-order（写，下单/改单）
```

本次做的事，是把这个模式**复制到第二个域**：

```
主数据域：master-data（读，查档案）          + master-data-maintain（写，建档）
```

> **一处叫特例，两处才叫架构。** 关键在这里：HITL 不是给某个工具打的补丁，
> 而是一条可复用的原则 —— 凡是改变业务数据的操作，一律拆到专门的写 Agent 并挂审批。

---

## 2. 架构决策：写操作放在哪

### 2.1 三个候选方案

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A | 直接给 `master_data.yaml` 加写工具 | 改动最小 | ①「领域 Agent 全部只读」的边界失效；② 读工具与写工具同处一个 Agent 会**诱导非预期写入** |
| **B（采用）** | 新建 `master-data-maintain` 写 Agent | 与既有 `procurement-analyst`/`order` 完全对称；不改动任何既有 Agent；路由用**动词**区分读写 | Agent 数 5 → 6；两个主数据 Agent 的描述需写清边界 |
| C | 把 `procurement-order` 泛化成统一写 Agent | 写操作集中、HITL 只有一处 | 要改名（连锁改动）；一个 Agent 装两套业务流程，prompt 会很长 |

### 2.2 为什么否掉 A（这条值得记住）

A 看着最省事，但它有个**具体的失败模式**，不是理论担忧：

> 模型刚用 `supplier_query` 查出"这个供应商不存在"，
> **最自然的下一个动作就是把它建出来** —— 哪怕用户只是问了一句"有这个供应商吗"。

读工具和写工具放在同一个 Agent 里，等于给模型递了一个它不该够得着的按钮。
权限边界必须体现在**工具集**上，而不是靠 prompt 里写"不要写"。

### 2.3 为什么否掉 C

写操作集中确实有个真实好处（HITL 配置只有一处，不可能漏配）。
但这个好处**可以用校验网拿到**，而且更好 —— 见 §5。用架构去换一个校验能解决的问题，
代价是 prompt 变长、流程混淆、还要改名。

---

## 3. 改动分三层

| 层 | 文件 | 谁写 | 说明 |
|---|---|---|---|
| ERP API | `src/erp_api/main.py` | 基础设施 | 3 个写端点 + 统一异常形状 |
| MCP | `src/mcp_server/tools/{suppliers,customers,products}_tools.py` | 基础设施 | 3 个 thin wrapper（21→23 个工具） |
| Agent | `src/agent/subagents/configs/master_data_maintain.yaml`<br>`src/skills/master/master-data-maintain-sop/SKILL.md` | Agent 层 | 写 Agent 配置 + 建档规范 |

### 3.1 ERP API 层：3 个写端点

```
POST /api/suppliers/create    新增供应商（编码不传则自动 SUP+3位）
POST /api/customers/create    新增客户  （编码不传则自动 CUS+5位）
POST /api/products/create     新增商品  （商品编码必须显式传入）
```

三个设计要点，每条都有原因：

#### 要点 1：唯一性交给数据库，不做「先 SELECT 再 INSERT」

三张表都有唯一索引：`uk_supplier_code` / `uk_customer_code` / `uk_product_code`。
所以写入时**直接插、捕获 MySQL 1062（Duplicate entry）** 转成可读错误：

```python
def _is_duplicate(exc: Exception) -> bool:
    """判断是否为 MySQL 1062（Duplicate entry，唯一索引冲突）。"""
    return bool(exc.args) and exc.args[0] == 1062
```

**对比 `order_create` 的现有写法**（先 `SELECT order_number` 查重、再 `INSERT`）：

```python
# order_create 现在的写法 —— 有竞态
cur.execute("SELECT id FROM purchase_order WHERE order_number=%s", (order_number,))
if cur.fetchone():
    return err("订单编号已存在: " + order_number)
```

两个并发请求可能**都**查到"不存在"，然后都走到 INSERT。
唯一索引是数据库层的最后一道闸；应用层预检查只能"提前给友好提示"，**不能替代它**。
主数据这三个端点采用「直接插 + 捕获 1062」，让数据库做它该做的事。

> ⚠️ 遗留：`order_create` 的这处竞态没动（不属于本次范围），已记入待办。

#### 要点 2：引用完整性必须应用层补 —— 因为**没有外键**

查了 `information_schema.key_column_usage`，结果是 **数据库里一个外键约束都没有**：

```
=== 外键约束 ===
  （数据库里没有任何外键约束）
```

这意味着 `product.supplier_id` 可以指向一个不存在的供应商，MySQL 不会拦。
所以新增商品时必须显式校验：

```python
if body.supplierId is not None:
    cur.execute("SELECT id FROM supplier WHERE id=%s AND deleted=0", (body.supplierId,))
    if not cur.fetchone():
        return err("supplierId 不存在: " + str(body.supplierId))
```

这条如果没查出来，写出来的就是**脏数据**。

#### 要点 3：编码能否自动生成，取决于它**有没有业务含义**

看了真实数据才发现三类编码性质不同：

| 对象 | 实际编码 | 性质 | 能否自动生成 |
|---|---|---|---|
| 供应商 | `SUP001` … `SUP033` | 纯序号 | ✅ 自动 `SUP+3位` |
| 客户 | `CUS00001` … `CUS00040` | 纯序号 | ✅ 自动 `CUS+5位` |
| 商品 | `HFS-400-01`（澜溪 400ml）、`MG-90-01`（梦享） | **带品牌与规格含义** | ❌ **必须显式传入** |

商品编码自动生成会产出无意义编码，破坏主数据的可读性 ——
所以 `productCode` 是必填，且不支持自动生成。

生成逻辑（沿用库里已有风格，并发撞号由唯一索引兜底后重试）：

```python
def _gen_master_code(cur, table: str, column: str, prefix: str, width: int) -> str:
    # table/column 只来自本模块内的字面量常量、不接受外部输入，故此处可安全拼接
    cur.execute(
        "SELECT MAX(CAST(SUBSTRING(`%s`, %d) AS UNSIGNED)) AS mx FROM `%s` WHERE `%s` LIKE %%s"
        % (column, len(prefix) + 1, table, column),
        (prefix + "%",),
    )
    row = cur.fetchone()
    return prefix + str((row["mx"] or 0) + 1).zfill(width)
```

#### 附带修复：统一异常形状

测试时发现必填字段缺失走的是 FastAPI 默认的 `422 {"detail": [...]}`，
**不符合本项目 `{code, message, data}` 的返回约定**，导致 MCP 工具拿到异构形状，
**Agent 看不出"到底哪个字段缺了"**，无法自我纠正。

补了一个异常处理器把形状收敛回来：

```python
@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request, exc: RequestValidationError):
    parts = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e.get("loc", ()) if x != "body")
        parts.append("%s (%s)" % (loc or "body", e.get("msg", "")))
    return JSONResponse(status_code=200, content=err("参数校验失败: " + "; ".join(parts)))
```

效果（实测）：`参数校验失败: productCode (Field required); name (Field required); purchasePrice (Field required)`

### 3.2 MCP 层：3 个 thin wrapper

按既有约定放在**实体对应的文件**里（不新开文件），工具名与前缀一致：

| 文件 | 现有工具 | 新增 |
|---|---|---|
| `suppliers_tools.py` | `supplier_query` | `supplier_create` |
| `customers_tools.py` | `customer_search` | `customer_create` |
| `products_tools.py` | `product_query` / `_search` / `_quotes` / `_by_supplier` | `product_create` |

`server_main.py` **不用改** —— 新工具写在已注册的 `register_*_tools` 函数体内。

> **顺带修掉一处汽车域残留**：`products_tools.py` 里三处 docstring 还写着"零件名称"、"零配件列表"。
> 这类文案不是注释 —— **MCP 工具的 docstring 就是模型直接读的 API 文档**，
> 留着会把它往错误品类上带。已改为"商品名称 / 商品列表"。

### 3.3 Agent 层：`master_data_maintain.yaml`

#### 为什么叫 `master-data-maintain`（而不是 `master-data-order`）

`procurement-order` 的 `-order` 指的是"订单"。主数据如果叫 `master-data-order`，
读起来像"主数据订单"，是错的。`maintain`（维护）既准确，又与 `master-data` 形成
「读 / 写」对照。

#### description 的三段式与「动词判据」

主 Agent 只根据 `name` + `description` 路由，所以两个主数据 Agent 的边界必须写在描述里：

```yaml
description: >
  【我是谁】主数据维护专员。负责**新增**三类主数据档案……

  【什么时候派给我】当需求是**新增 / 建档 / 录入 / 开户 / 准入 / 注册 / 建到系统里**
  这类动作，且对象是供应商、客户或商品时，派给我。

  【别派给我什么】
  - **只看不动**的查询 → master-data。同样是供应商：
    **"查一下 XX 供应商" 给他；"新增 XX 供应商" 给我。**
    判断标准是**动词**：查 / 看 / 有哪些 / 是什么含义 → master-data；
    新增 / 建档 / 录入 / 开户 / 准入 → 我。
```

「用动词而不是名词区分」是这里的关键 —— 因为两个 Agent 的**名词域完全重合**（都是主数据），
唯一稳定的判据是动作。

#### 工具集刻意收窄：不给 `meta_dict` / `meta_schema`

`master-data`（读）有元数据工具，但写 Agent **故意不给**：

> 字段取值含义（A/B/C/D、客户类型 1/2/3）是稳定的小数据集，直接写在 system_prompt 里
> 比再挂两个工具更省 token、也让写 Agent 的权限面更小。

7 个工具 = 3 个写 + 3 个查重 + 1 个核验（`web_search`，用于核验企业工商信息）。

#### 没有给 `request_order_info`

`request_order_info` 是"缺字段时暂停等人工补充"的 HITL 工具。
主数据建档没挂它，原因：建档的必填字段很少（名称 + 编码 + 价格），
缺什么直接在对话里问用户就够了，不需要为它再开一条暂停通道。**权限最小化**。

#### `interrupt_on` 覆盖全部三个写工具

```yaml
interrupt_on:
  supplier_create:
    allowed_decisions: ["approve", "reject"]
  customer_create:
    allowed_decisions: ["approve", "reject"]
  product_create:
    allowed_decisions: ["approve", "reject"]
```

#### 配套技能：`master-data-maintain-sop`

`src/skills/master/master-data-maintain-sop/SKILL.md`，与 `master-data` 共用
`/skills/master/` scope（同域技能互相可见是预期行为，`procurement` 域本来就是这样）。

里面有份**真实的品牌缩写解码表**（从库里 75 个 SKU 实际统计出来，不是编的）：
`HFS`=澜溪、`CLR`=朗风、`LUX`=雅致、`DV`/`DVE`=悦芙、`VS`=润舒、`PD`=净妍、
`ZH`=中华、`JN`=洁诺、`LFB`=卫宝、`OM`=洁力、`JF`=柔洁、`WM`=净威先生、
`WLX`=雪悦、`MG`=梦享、`CAND`=甜心多、`KL`/`KN`=家悦、`LT`=茶悦。

这决定了 Agent 在用户只给商品名时，能**提出一个符合现有风格的编码建议**，
而不是编一个或直接卡住。

---

## 4. 中间件档位 `order` → `write`

原先注册表是：

```python
MIDDLEWARE_PROFILES = {"analyst": create_analyst_middleware, "order": create_order_middleware}
```

现在要出现**第二个写 Agent**，而它是主数据，挂 `middleware_profile: order` 就名不副实。
改为按**任务形态**命名：

```python
# 档位按「任务形态」划分，不按业务域：
#   analyst = 只读分析（长链路、多步、需压缩上下文）
#   write   = 写操作（短链路、需审批、上限更紧）
MIDDLEWARE_PROFILES = {
    "analyst": create_analyst_middleware,
    "write": create_write_middleware,
}
```

顺带在 `create_write_middleware` 的 docstring 里补上「为什么写操作上限更紧」：

> - 写工具挂了 `interrupt_on`，每次调用都要人来审批，循环次数多等于反复打扰人；
> - 写操作出错代价高，跑飞了不像查询那样可以重来 —— 尽早刹车。

引用点只有 2 处（`middleware_config.py` 定义 + `procurement_order.yaml` 引用），改动很小。

---

## 5. 写工具安全网（本次最有价值的部分）

### 5.1 要解决的问题

`interrupt_on` 原先只覆盖 `order_create` / `order_update`，靠的是**人记得配**。
一旦有人加了新的写工具却忘了配 `interrupt_on`，该 Agent 就会
**静默获得无需审批的写权限** —— 配置错了但服务照常启动，直到某次误写才被发现。

这类"漏配"必须由**启动期校验**拦住。为此加了 `loader.py` 里的两道互补的网：

### 5.2 第一张网：显式登记 + 双向校验

```python
WRITE_TOOLS = frozenset({
    "order_create", "order_update",              # 采购订单
    "supplier_create", "customer_create", "product_create",   # 主数据建档
})
```

`validate_interrupt_on(cfg)` **两个方向都查**：

| 方向 | 情况 | 后果 |
|---|---|---|
| 1 | 有写工具，但没配 `interrupt_on` | **越权** —— 工具能改数据但无人审批 |
| 2 | `interrupt_on` 配了，但该工具不是本 Agent 的写工具 | **静默无效** —— 配了个永远不会触发的审批，反而给人"已经配好了"的错觉 |

方向 2 特别值得注意：它和"工具名拼错"是同一类 bug ——
配置看起来存在，实际永不生效。

### 5.3 第二张网：反向核对防「漏登记」

只有第一张网还不够：**新增一个写工具但忘了登记进 `WRITE_TOOLS`** 时，
`validate_interrupt_on` 完全看不见它，安全网就出现缺口。

所以再加一道按命名反查的网：

```python
WRITE_NAME_HINTS = ("_create", "_update", "_delete", "_submit",
                    "_approve", "_cancel", "_import")

def cross_check_write_registry(available_tool_names) -> None:
    """反向核对：名字像写操作、却没登记进 WRITE_TOOLS 的工具，直接报错。"""
    suspicious = sorted(
        n for n in available_tool_names
        if str(n).endswith(WRITE_NAME_HINTS) and n not in WRITE_TOOLS
    )
    if suspicious:
        raise ValueError(...)
```

调用点在 `resolve_subagent_tools()` —— 那里是**唯一同时拿到全量工具名**的位置。

> **为什么要两张网**：只用第一张，漏登记就静默失守；只用第二张，命名不符的就漏判。
> 各自防对方的洞，合起来才闭合。

### 5.4 覆盖范围的诚实说明

登记表覆盖的是**业务数据写操作**，**不含** `assign_skill` 这类"Agent 自身记忆/元操作"。
理由：它不是业务事务，每次都要求人工审批会淹没真正的业务审批。
这一点写进了 `loader.py` 的注释，避免以后有人误以为漏登记。

---

## 6. 安全网上线即抓到一个真 bug

写完网跑第一次校验，立刻炸出一条**我没预料到的真实越权**：

```
FAIL  procurement-analyst
      子 Agent 'procurement-analyst' 拥有写工具 ['product_create']，
      但未配置 interrupt_on —— 这些工具将不经人工审批直接修改业务数据。
```

**根因**：`procurement_analyst.yaml` 的工具列表里写的是**通配符** `product_*`：

```yaml
  - product_*   # 商品分页查询 / 按名称搜索 / 按供应商查
```

我新增 `product_create` 之后，这个通配符**把它一并捞了进来** ——
一个只读分析 Agent 静默获得了「新建商品档案」的写权限。

**修复**：把通配符换成显式枚举（行为一个不少一个不多），并写明原因：

```yaml
# 注意：这里刻意**不用** product_* 通配符。
# 原先写的是 product_*，新增 product_create（写工具）后，它会被通配符一并捞进来，
# 使本 Agent（只读分析域）**静默获得建档写权限**。改用显式枚举后，
# 以后新增任何 product_ 前缀的工具都不会自动落到本 Agent 头上。
tools:
  - supplier_query
  - product_query
  - product_search
  - product_by_supplier
  - product_quotes
  - order_search_details
  - inventory_warning
  - generate_visualization
  - web_search
```

> 这个 bug 是**本次改动引入的**（新增工具让旧通配符的语义变了），
> 而且在没有校验网的情况下会**完全静默**：服务正常启动、测试正常通过、
> 直到某天分析 Agent 顺手建了个商品档案才被发现。
> 这是"权限必须显式声明"最有力的一课。

---

## 7. 验证证据

### 7.1 三个写端点实测（12 个用例）

测试脚本建了真实记录再清理（数据回到 33/40/75 行）：

| # | 用例 | 结果 |
|---|---|---|
| 1 | 自动编码新增供应商 | ✅ `SUP034` |
| 2 | 显式 `SUP001`（已存在） | ✅ `供应商编码已存在: SUP001` |
| 3 | `creditRating=X` | ✅ `creditRating 只能是 A/B/C/D` |
| 4 | 缺 `name` | ✅ `参数校验失败: name (Field required)`（统一形状） |
| 5 | 自动编码新增客户 | ✅ `CUS00041` |
| 6 | 显式 `CUS00001`（已存在） | ✅ `客户编码已存在: CUS00001` |
| 7 | `customerType=9` | ✅ `customerType 只能是 1(KA商超) / 2(经销商) / 3(电商)` |
| 8 | 缺 `productCode` | ✅ 参数校验失败 |
| 9 | `supplierId=999999` | ✅ `supplierId 不存在: 999999`（引用完整性） |
| 10 | 正常新增商品 | ✅ `TEST-SKU-001`，`supplierId=1` 落库正确 |
| 11 | 重复 `productCode` | ✅ `商品编码已存在: TEST-SKU-001` |
| 12 | `purchasePrice=-5` | ✅ `purchasePrice 必填且不能为负` |

数据库侧核对过编码真的落库、`deleted=0`、自增 ID 正确。

### 7.2 校验网单测（方向 1 / 方向 2 / 漏登记 / 通配符）

| 用例 | 结果 |
|---|---|
| 现有配置全部通过 | ✅ 6/6 |
| 有写工具没配 interrupt_on | ✅ 拦截 |
| **通配符**命中写工具却没配 | ✅ 拦截 |
| interrupt_on 配了只读工具 | ✅ 拦截 |
| interrupt_on 工具名拼错 | ✅ 拦截（`order_creat`） |
| 正确配置 | ✅ 通过（无误报） |
| 28 个真实工具名反向核对 | ✅ 无误报 |
| `supplier_delete` / `order_cancel` / `foo_import` / `bar_update` | ✅ 全部拦截 |

### 7.3 离线等价 Phase 5.5 + Phase 7

新增 `src/test/validate_configs.py`（重写，原文件还在引用汽车域的 `part_query`）。
它**不依赖 VM**，用进程内注册的真实工具名跑完整校验链：

```
1) 工具命名空间
  MCP 工具 23 个 + 本地工具 5 个 = 28 个
  写工具登记表核对通过（已登记 5 个写工具）
3) 启动期校验（等价 Phase 5.5）: 已注册中间件档位: ['analyst', 'write'] / 6 个配置全部通过
4) 工具解析（等价 Phase 7）:
  inventory-logistics    [只读域]   9 个工具
  master-data            [只读域]   8 个工具
  master-data-maintain   [写 Agent] 7 个工具  写工具 3 个 / 审批配置一致
  procurement-analyst    [只读域]   9 个工具
  procurement-order      [写 Agent] 5 个工具  写工具 2 个 / 审批配置一致
  sales-analyst          [只读域]   8 个工具
全部校验通过（exit 0）
```

用法（在 `src/` 下）：`python test/validate_configs.py`

---

## 8. 遗留与下一步

### 本次新增的待办

- [ ] **`order_create` 的查重竞态** —— 仍是「先 SELECT 再 INSERT」，
      可改用与主数据一致的「直接插 + 捕获 1062」
- [ ] **`request_order_info` 命名债** —— 名字带 order，但机制是通用的"缺字段暂停"。
      若将来给主数据也用它，应改名为 `request_info`
- [ ] **`procurement-analyst` 是否需要 `product_quotes`** —— 改显式枚举时按"保持原行为"
      保留了它，但需要确认比价流程是否真的用到
- [ ] **端到端 HITL 实测** —— 需要 VM 的 OpenSandbox，当前 VM 不可达（见下）

### 本次实证的阻塞项

**A2（Mongo 顶层连接）影响比原先记录的更大**：

```
ServerSelectionTimeoutError: 127.0.0.1:27017: timed out
```

VM 不可达时，`import agent.main_agent` 在**模块 import 期**就抛错，
**服务根本走不到 Phase 5.5** —— 也就是说那套启动期校验网在"依赖挂了"的场景下
没有机会执行，而且报错完全不可读。

修掉 A2（`MongoClient` / `CHECKPOINTER` 改懒加载 + `serverSelectionTimeoutMS=3000`）
之后，配置校验才能真正做到"不依赖 VM"。**建议优先级提前**。

### 三个关键点

1. **HITL 是模式不是特例** —— 同一原则被应用到第二个域，读写分离在采购域和主数据域重复出现
2. **两道互补的校验网** —— 显式登记防误判、命名反查防漏登记，各自防对方的洞
3. **通配符的真实代价** —— `product_*` 在新增工具后语义漂移，让只读 Agent 静默获得写权限；
   这是"权限必须显式声明"的实证，不是理论
