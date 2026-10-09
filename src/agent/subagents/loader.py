"""
子 Agent YAML 配置加载器。

从 YAML 配置文件目录读取子 Agent 定义，转换为 DeepAgents
create_deep_agent() 所需的 SubAgent dict 格式。

使用方式:
    from agent.subagents.loader import load_subagent_configs, resolve_subagent_tools

    # 1. 加载原始配置（tools 为字符串名称列表）
    raw_configs = load_subagent_configs()

    # 2. 与 MCP 工具匹配，解析为可调用的工具对象
    subagents = resolve_subagent_tools(raw_configs, all_mcp_tools)
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import yaml

# YAML 配置文件目录
CONFIGS_DIR = Path(__file__).parent / "configs"


def load_subagent_configs(
    configs_dir: Optional[Path] = None,
) -> List[Dict]:
    """
    加载指定目录下所有 .yaml 子 Agent 配置文件。

    每个 YAML 文件的顶级结构:
        name: str               # 必须 — 子 Agent 唯一名称
        description: str        # 必须 — 供主 Agent 决策的描述
        system_prompt: str      # 必须 — 子 Agent 系统提示词
        tools: list[str]        # 必须 — 工具名称列表（运行时解析）
        model: str              # 可选 — 模型标识符
        skills: list[str]       # 可选 — 技能路径列表

    Args:
        configs_dir: 配置文件目录，默认为 CONFIGS_DIR。

    Returns:
        SubAgent 配置字典列表，tools 字段暂为字符串名称。

    Raises:
        ValueError: 目录不存在 / 任一文件读取或解析失败 / 缺少必填字段 /
                    name 重复（fail-fast：配置错误必须让服务起不来，
                    不能让某个 Agent 静默消失）
    """
    if configs_dir is None:
        configs_dir = CONFIGS_DIR

    if not configs_dir.exists():
        raise ValueError(f"子 Agent 配置目录不存在: {configs_dir}")

    configs: List[Dict] = []
    errors: List[str] = []
    seen_names: Dict[str, str] = {}   # name → 来源文件名

    for yaml_file in sorted(configs_dir.glob("*.yaml")):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            errors.append(f"  - {yaml_file.name}: YAML 解析失败 — {e}")
            continue
        except Exception as e:
            errors.append(f"  - {yaml_file.name}: 读取失败 — {e}")
            continue
        # 空文件 / 只含注释的文件会被 safe_load 解析成 None
        if not isinstance(data, dict):
            errors.append(
                f"  - {yaml_file.name}: 内容不是有效的 YAML 映射"
                f"（实际为 {type(data).__name__}）"
            )
            continue
        # 校验必填字段
        missing = _validate_subagent_config(data, yaml_file.name)
        if missing:
            errors.append(
                f"  - {yaml_file.name}: 缺少必填字段 {', '.join(missing)}"
            )
            continue

        # 校验 name 唯一：重名会让后者覆盖前者的 scope / 中间件配置
        name = data["name"]
        if name in seen_names:
            errors.append(
                f"  - {yaml_file.name}: 子 Agent 名 '{name}' "
                f"与 {seen_names[name]} 重复"
            )
            continue

        seen_names[name] = yaml_file.name
        configs.append(data)
        print(f"[INFO] 已加载子 Agent 配置: {name} ← {yaml_file.name}")

    if errors:
        raise ValueError("子 Agent 配置加载失败：\n" + "\n".join(errors))

    return configs


def resolve_subagent_tools(
    configs: List[Dict],
    available_tools: List,
    extra_middleware: Dict[str, List] | None = None,
) -> List[Dict]:
    """将 YAML 配置中的工具名称字符串解析为实际的工具可调用对象。

    匹配规则见 _match_tools：精确名，或 "前缀*" 通配。
    任何未命中的模式都会在全部解析完成后一次性 fail-fast，不做静默降级。

    Args:
        configs: load_subagent_configs() 返回的原始配置列表。
        available_tools: 可用的工具对象列表（MCP + 本地工具）。
        extra_middleware: 子 Agent 额外中间件，key 为子 Agent 名称。

    Returns:
        可以直接传入 create_deep_agent(subagents=...) 的 SubAgent 字典列表。

    Raises:
        ValueError: 存在未能匹配任何工具的模式（启动期 fail-fast）
    """
    if extra_middleware is None:
        extra_middleware = {}

    # 构建工具名 → 工具对象的索引
    tool_index: Dict[str, object] = {}
    for t in available_tools:
        name = getattr(t, "name", None)
        if name:
            tool_index[name] = t

    # 反向核对写工具登记表：此处已拿到全量工具名，是唯一能查"漏登记"的位置
    cross_check_write_registry(tool_index.keys())

    subagents: List[Dict] = []
    unresolved: List[str] = []

    for config in configs:
        resolved_tools: List = []

        for pattern in config.get("tools", []):
            matched = _match_tools(pattern, tool_index)
            if not matched:
                unresolved.append(f"  - {config['name']} → {pattern!r}")
                continue
            resolved_tools.extend(matched)

        # 去重（保留顺序）：通配与显式名可能命中同一个工具
        seen = set()
        unique_tools = []
        for t in resolved_tools:
            name = getattr(t, "name", id(t))
            if name not in seen:
                seen.add(name)
                unique_tools.append(t)

        subagent: Dict = {
            "name": config["name"],
            "description": config["description"].replace("\n", " ").strip(),
            "system_prompt": config["system_prompt"],
            "tools": unique_tools,
        }

        # 可选字段
        if config.get("model"):
            subagent["model"] = config["model"]

        if config.get("skills"):
            subagent["skills"] = config["skills"]

        if config.get("interrupt_on"):
            subagent["interrupt_on"] = config["interrupt_on"]

        # 合并额外中间件
        agent_middleware = list(config.get("middleware", []))
        if config["name"] in extra_middleware:
            agent_middleware.extend(extra_middleware[config["name"]])
        if agent_middleware:
            subagent["middleware"] = agent_middleware

        subagents.append(subagent)

    # 全部解析完再一次性报错：一次看到所有问题，而不是改一个报一个
    if unresolved:
        raise ValueError(
            "子 Agent 工具名解析失败，以下模式未匹配到任何工具：\n"
            + "\n".join(unresolved)
            + f"\n\n可用工具（{len(tool_index)} 个）：\n  "
            + ", ".join(sorted(tool_index))
        )

    for sa in subagents:
        print(
            f"[INFO] 子 Agent '{sa['name']}' 已解析: "
            f"{len(sa['tools'])} 个工具, "
            f"{len(sa.get('skills', []))} 个技能"
        )

    return subagents


def _validate_subagent_config(data: Dict, filename: str) -> List[str]:
    """
    校验 SubAgent 配置的必填字段。

    Returns:
        缺失的字段名列表（空列表表示通过校验）。
    """
    required = ["name", "description", "system_prompt", "tools"]
    missing = [f for f in required if f not in data or data[f] is None]

    # tools 必须是非空列表
    if "tools" in data:
        tools = data["tools"]
        if not isinstance(tools, list) or len(tools) == 0:
            missing.append("tools (必须为非空列表)")

    # system_prompt 不能为空字符串
    if "system_prompt" in data:
        sp = data["system_prompt"]
        if not isinstance(sp, str) or not sp.strip():
            missing.append("system_prompt (必须为非空字符串)")

    return missing


# ============================================================
# 技能 scope 映射
# ============================================================

MAIN_SKILL_SCOPE = "main"

# 技能目录的固定前缀（沙箱内）
SKILLS_ROOT_PREFIX = "/skills/"

def _normalized_skills(cfg: Dict) -> List[str]:
    """取子 Agent 的 skills 路径并归一化（去尾斜杠）。

    YAML 里 /skills/procurement/ 与 /skills/procurement 视为同一路径。
    """
    return [str(p).rstrip("/") for p in (cfg.get("skills") or [])]

def validate_skill_scope(cfg: Dict) -> None:
    """校验子 Agent 的 skills 声明足以推导出唯一的技能 scope 目录。

    scope 由 skills 路径推导（/skills/procurement/ → "procurement"），
    故路径必须：非空、唯一、位于 /skills/ 下 —— 否则推导出的 scope
    会与 Agent 实际读取的目录不一致（技能复制到 A、Agent 在 B 找）。

    Raises:
        ValueError: 未声明 skills / 声明了多个 / 路径不在 /skills/ 下
    """
    name = cfg.get("name", "<未命名>")
    skills = _normalized_skills(cfg)

    if not skills:
        raise ValueError(
            f"子 Agent '{name}' 未声明 skills，无法确定技能 scope 目录"
        )

    if len(skills) > 1:
        raise ValueError(
            f"子 Agent '{name}' 声明了多个 skills 目录 {skills}，"
            f"无法确定唯一技能 scope（assign_skill 只能写入一个目录）"
        )

    if not skills[0].startswith(SKILLS_ROOT_PREFIX):
        raise ValueError(
            f"子 Agent '{name}' 的 skills 路径 '{skills[0]}' 不在 "
            f"'{SKILLS_ROOT_PREFIX}' 下，无法推导技能 scope 目录"
        )

def build_skill_scope_map(configs: List[Dict]) -> Dict[str, str]:
    """构建「Agent 名 → 技能 scope 目录」映射。

    scope 不是独立字段 —— 它就是 skills 路径的末段目录名，
    因此二者不可能不一致，也无需维护冗余配置。

    main 是内建项：主 Agent 自身，没有 YAML 配置。

    Args:
        configs: load_subagent_configs() 的返回值

    Returns:
        {"main": "main", "procurement-analyst": "procurement", ...}

    Note:
        调用方应先用 validate_skill_scope() 逐个校验 —— 启动期由
        precompute_agent_context() 统一执行。
    """
    scope_map: Dict[str, str] = {MAIN_SKILL_SCOPE: MAIN_SKILL_SCOPE}

    for cfg in configs:
        skills = _normalized_skills(cfg)
        if not skills:
            # 兜底：未经校验直接调用时不崩；合法性判定归 validate_skill_scope
            continue
        # "/skills/procurement" → "procurement"
        scope_map[cfg["name"]] = skills[0].rsplit("/", 1)[-1]

    return scope_map

def _match_tools(pattern: str, tool_index: Dict[str, object]) -> List:
    """按模式匹配工具。

    支持两种写法：
    - "supplier_query"   精确匹配（推荐：权限边界明确）
    - "inventory_*"      前缀通配（末尾 * 是唯一通配符）

    刻意不支持子串匹配：子串会让"写短了"变成静默扩权，
    例如写 supplier 会把 product_by_supplier 一起捞进来。

    Args:
        pattern: YAML tools 里的一个条目
        tool_index: {工具名: 工具对象}

    Returns:
        匹配到的工具对象列表（可能为空列表）
    """
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return [obj for name, obj in tool_index.items() if name.startswith(prefix)]

    obj = tool_index.get(pattern)
    return [obj] if obj else []


# ============================================================
# 写操作安全网：写工具必须挂 HITL（interrupt_on）
# ============================================================
# 背景：interrupt_on 原先只覆盖 order_create / order_update。一旦有人给某个
# 子 Agent 加了新的写工具却忘了配 interrupt_on，该 Agent 就会**静默获得无需
# 审批的写权限** —— 配置错了但服务照常启动，直到某次误写才被发现。
# 这种"漏配"必须由启动期校验拦住，不能靠人记得。
#
# 这里用两张互补的网，各自补对方的洞：
#   1) WRITE_TOOLS 显式登记 —— 防【误判】：不靠命名去猜哪些是写工具，
#      避免把只读工具误当写工具（命名约定会猜错）。
#   2) WRITE_NAME_HINTS 反向核对 —— 防【漏登记】：名字看起来像写工具、
#      却没登记进 WRITE_TOOLS 的，直接报错。
# 只用第 1 张，新增写工具忘了登记就静默失守；只用第 2 张，命名不符的就漏判。
#
# 覆盖范围：**业务数据写操作**。不含 assign_skill 这类"Agent 自身记忆/元操作"
# —— 它不是业务事务，每次都要求人工审批会淹没真正的业务审批。
# ============================================================

WRITE_TOOLS = frozenset({
    # 采购订单
    "order_create",
    "order_update",
    # 主数据建档
    "supplier_create",
    "customer_create",
    "product_create",
})

WRITE_NAME_HINTS = (
    "_create", "_update", "_delete", "_submit",
    "_approve", "_cancel", "_import",
)


def cross_check_write_registry(available_tool_names) -> None:
    """反向核对：名字像写操作、却没登记进 WRITE_TOOLS 的工具，直接报错。

    这张网防的是**漏登记** —— 例如将来加了 supplier_delete 但忘了登记，
    那么 validate_interrupt_on() 完全看不见它，安全网就出现了缺口。

    Raises:
        ValueError: 存在疑似未登记的写工具（启动期 fail-fast）
    """
    suspicious = sorted(
        n for n in available_tool_names
        if str(n).endswith(WRITE_NAME_HINTS) and n not in WRITE_TOOLS
    )
    if suspicious:
        raise ValueError(
            "以下工具名看起来是写操作，但未登记到 loader.WRITE_TOOLS，"
            "因此不会被「写工具必须配 interrupt_on」校验覆盖：\n  - "
            + "\n  - ".join(suspicious)
            + "\n\n若确为写工具：请登记进 WRITE_TOOLS，并在对应子 Agent 配 interrupt_on；"
            "\n若确为只读工具：请改名以消除歧义。"
        )


def _expand_write_patterns(patterns) -> List[str]:
    """把 YAML 的 tools 模式（含 "前缀*" 通配）展开成实际命中的**写**工具名。"""
    hit: List[str] = []
    for p in patterns or []:
        p = str(p)
        if p.endswith("*"):
            hit.extend(n for n in WRITE_TOOLS if n.startswith(p[:-1]))
        elif p in WRITE_TOOLS:
            hit.append(p)
    return sorted(set(hit))


def validate_interrupt_on(cfg: Dict) -> None:
    """校验子 Agent 的写工具与 interrupt_on 配置互相匹配（两个方向都查）。

    方向 1 —— 有写工具却没配 interrupt_on：**越权**（工具能改数据但无人审批），
              这是最危险的一类，必须让服务起不来。
    方向 2 —— 配了 interrupt_on 但该工具不是本 Agent 的写工具：**静默无效**，
              配了个永远不会触发的审批，反而给人"已经配好了"的错觉。

    Raises:
        ValueError: 上述任一方向出问题
    """
    name = cfg.get("name", "<未命名>")
    write_hits = _expand_write_patterns(cfg.get("tools"))
    configured = {str(k) for k in (cfg.get("interrupt_on") or {}).keys()}

    missing = [t for t in write_hits if t not in configured]
    if missing:
        raise ValueError(
            f"子 Agent '{name}' 拥有写工具 {missing}，但未配置 interrupt_on —— "
            f"这些工具将不经人工审批直接修改业务数据。"
            f"请在 YAML 的 interrupt_on 下为它们声明 allowed_decisions。"
        )

    ineffective = sorted(configured - set(write_hits))
    if ineffective:
        raise ValueError(
            f"子 Agent '{name}' 的 interrupt_on 配置了 {ineffective}，"
            f"但它们既不是该 Agent 的写工具、也不在其 tools 列表中 —— "
            f"该审批永远不会触发（形同虚设）。请删除或修正。"
            f"（当前已知写工具：{sorted(WRITE_TOOLS)}）"
        )