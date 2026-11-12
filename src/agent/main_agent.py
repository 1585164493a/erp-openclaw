"""
主 Agent 入口模块。

使用 DeepAgents `create_deep_agent` 将所有组件串联为一个可运行的
ERP 采购智能助手。采用 Graph Factory 模式：启动时预计算可复用组件，
每次请求基于 per-user 沙箱轻量创建 agent graph，实现用户级沙箱隔离。

使用方式:
    from agent.main_agent import precompute_agent_context, create_main_agent

    # 启动时
    precomputed = await precompute_agent_context()

    # 每次请求
    agent_graph = await create_main_agent(
        config,
        sandbox_backend=user_sandbox,
        precomputed=precomputed,
    )
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend
from deepagents.backends.protocol import SandboxBackendProtocol
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.runnables import RunnableConfig

from agent.config import (
    AGENTS_MD_FILENAME,
    DOWNLOAD_DIR,
    LOCAL_AGENTS_MD,
    MAIN_MODEL,
    SKILLS_STORE_NAMESPACE,
    SUMMARY_MODEL,
    get_checkpointer,
    get_store,
)
from agent.memory.prompts import system_prompt
from agent.middleware_config import build_subagent_middleware, MIDDLEWARE_PROFILES,validate_middleware_profile
from agent.middlewares.context_injection import ContextInjectionMiddleware
from agent.middlewares.memory_update import MemoryUpdateMiddleware
from agent.middlewares.sandbox_breaker import SandboxCircuitBreakerMiddleware
from agent.middlewares.tool_loop_guard import ToolLoopGuardMiddleware
from agent.middlewares.tool_rollback import ToolRollbackMiddleware
from agent.middlewares.context_compaction import ToolResultCompactionMiddleware
from agent.middlewares.context_summary import ContextSummarizerMiddleware
from agent.middlewares.compensators import register_default_compensators
from agent.middlewares.sandbox_health import SandboxHealthMiddleware
from agent.middlewares.skills_sync import SkillsSyncMiddleware
from agent.middlewares.task_state import TaskStateMiddleware
from agent.middlewares.tool_error import ToolErrorMiddleware
from agent.middlewares.tools_summarization import build_summarization_middleware
from agent.middlewares.user_skills_restore import UserSkillsRestoreMiddleware
from agent.schema import ProcurementContext
from agent.subagents.loader import (
    load_subagent_configs,
    resolve_subagent_tools,
    build_skill_scope_map,
    validate_interrupt_on,
    validate_skill_scope,
)
from agent.tools.chart_generator import create_generate_chart_tool
from agent.tools.hitl_tools import request_order_info
from agent.tools.clarification import request_clarification
from agent.tools.assign_skill import create_assign_skill_tool
from agent.tools.download_sandbox_file import create_download_tool
from agent.tools.py_exec import create_py_exec_tool
from agent.tools.sql_tools import create_sql_tools
from agent.tools.mcp_client import load_mcp_tools, McpToolset
from agent.tools.web_search import web_search


def _setup_logging() -> None:
    env = os.environ.get("APP_ENV", "development")
    if env == "production":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            filename="erp_agent.log",
            filemode="a",
        )
    else:
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stdout,
        )

_setup_logging()
logger = logging.getLogger(__name__)


# ============================================================
# PrecomputedContext — 启动时预计算的可复用组件
# ============================================================

@dataclass
class PrecomputedContext:
    """Phase 2/3/5 预计算结果的不可变容器。

    仅包含不依赖 sandbox_backend 的组件（MCP 工具、图表工具、YAML 配置）。
    sandbox 依赖的工具（assign_skill、download_sandbox_file）和 backend 依赖的
    中间件在 create_main_agent() 中按请求动态创建。
    """
    toolset: Optional[McpToolset] = None
    extra_mcp_tools: list = field(default_factory=list)
    generate_visualization: object = None
    raw_subagent_configs: list = field(default_factory=list)
    skill_scope_map: dict = field(default_factory=dict)

async def precompute_agent_context() -> PrecomputedContext:
    """Phase 2/3/5 预计算，启动时执行一次，所有请求复用。"""
    logger.info("=== 预计算 Agent 上下文（Phase 2/3/5）===")

    # Phase 2: MCP 工具加载
    #
    # ⚠️ 2026-11-10 加了三重重试。原因：
    #   启动器虽然会先等 8099 的 TCP 通了才拉起后端，但「TCP 通了」≠「MCP 端点可用」——
    #   服务此时可能还在预热工具列表 / 首次握手。原来一次失败就 raise，
    #   后端进程直接退出，启动器再等 240 秒、然后判失败并（旧版）连坐杀掉 8080/8099。
    #   加重试后，这类几秒级的时序抖动能自愈，不必让用户重新点一次启动。
    #
    #   注意：重试只针对「连接/握手」这类瞬时错误；如果 erp-api 真的挂了，
    #   三次都会失败，仍然 raise —— 那个语义没有放松。
    logger.info("Phase 2: 加载 MCP 工具...")
    toolset: Optional[McpToolset] = None
    last_exc: Optional[BaseException] = None
    for attempt in range(1, 4):
        try:
            toolset = await load_mcp_tools()
            if attempt > 1:
                logger.info("  第 %d 次尝试成功", attempt)
            break
        except Exception as exc:  # noqa: BLE001 - 故意兜住一切，下面统一重试
            last_exc = exc
            logger.warning("MCP 工具加载失败（第 %d/3 次）：%s: %s",
                           attempt, type(exc).__name__, exc)
            if attempt < 3:
                backoff = 2.0 * attempt          # 2s, 4s
                logger.info("  %.0f 秒后重试...", backoff)
                await asyncio.sleep(backoff)
    if toolset is None:
        logger.error("MCP 工具加载失败（已重试 3 次），最后一次异常：%r", last_exc)
        raise RuntimeError("MCP 工具加载失败，无法预计算") from last_exc

    # Phase 3: 可视化工具合并
    logger.info("Phase 3: 合并可视化工具 (26→1)...")
    generate_visualization, extra_mcp_tools = create_generate_chart_tool(toolset.chart_tools)
    if extra_mcp_tools:
        logger.info(f"  保留独立工具: {[t.name for t in extra_mcp_tools]}")

    # Phase 5: 子 Agent YAML 配置加载
    logger.info("Phase 5: 加载子 Agent YAML 配置...")
    raw_configs = load_subagent_configs()
    if not raw_configs:
        logger.warning("  未找到任何子 Agent 配置")
    else:
        logger.info(f"  已加载 {len(raw_configs)} 个子 Agent 配置")

    # Phase 5.5: 启动期校验子 Agent 配置（不等到第一个请求才暴露）
    for cfg in raw_configs:
        validate_middleware_profile(
            cfg.get("middleware_profile"), cfg.get("name", "<未命名>")
        )
        validate_skill_scope(cfg)
        # 写工具必须挂 HITL：漏配 = 该子 Agent 静默获得无需审批的写权限
        validate_interrupt_on(cfg)
    skill_scope_map = build_skill_scope_map(raw_configs)
    logger.info("=== 预计算完成 ===")
    return PrecomputedContext(
        toolset=toolset,
        extra_mcp_tools=extra_mcp_tools,
        generate_visualization=generate_visualization,
        raw_subagent_configs=raw_configs,
        skill_scope_map=skill_scope_map,
    )


# ============================================================
# create_main_agent — 每次请求基于 per-user 沙箱创建 agent graph
# ============================================================

async def create_main_agent(
    config: RunnableConfig,
    *,
    sandbox_backend: SandboxBackendProtocol,
    precomputed: PrecomputedContext,
):
    """创建 ERP 采购智能助手的 per-request graph factory。

    每次请求调用，使用预计算的 MCP 工具/YAML 配置 + 外部传入的 per-user 沙箱，
    轻量创建 agent graph。SandboxBackendProxy 保证沙箱热替换不丢引用。

    Args:
        config: LangGraph RunnableConfig，含 thread_id + user_id。
        sandbox_backend: per-user 沙箱后端（SandboxBackendProxy）。
        precomputed: 启动时预计算的 MCP 工具/YAML 配置。
    """
    user_id = config["configurable"]["user_id"]
    logger.info(f"=== 为用户 {user_id} 创建 Agent Graph ===")

    # ---- Phase 1: CompositeBackend factory ----
    # 每次请求重建 CompositeBackend（StoreBackend 依赖 runtime），
    # 内部 sandbox_backend 是 SandboxBackendProxy（热替换不丢引用）。
    def backend_factory(runtime):
        return CompositeBackend(
            default=sandbox_backend,
            routes={
                "/memories/": StoreBackend(
                    runtime=runtime,
                    namespace=lambda rt: (getattr(rt.runtime.context, 'user_id', 'aff'),),
                ),
                "/persisted-skills/": StoreBackend(
                    runtime=runtime,
                    namespace=lambda rt: SKILLS_STORE_NAMESPACE,
                ),
            },
        )

    # ---- Phase 1.4: 上传 AGENTS.md 到沙箱 ----
    logger.info("Phase 1.4: 上传 AGENTS.md 到沙箱...")
    ag_md_content = LOCAL_AGENTS_MD.read_text(encoding="utf-8")
    sandbox_backend.upload_files([("/AGENTS.md", ag_md_content.encode("utf-8"))])

    # ---- Phase 2/3/5: 使用预计算结果 ----
    logger.info("Phase 2-5: 使用预计算的 MCP 工具 + 图表工具 + YAML 配置...")
    generate_visualization = precomputed.generate_visualization
    extra_mcp_tools = precomputed.extra_mcp_tools

    # ---- Phase 3.6: 创建 sandbox 依赖工具（per-request）----
    logger.info("Phase 3.6: 创建 sandbox 依赖工具...")
    assign_skill = create_assign_skill_tool(
        sandbox_backend,
        store=get_store(),
        skills_namespace=SKILLS_STORE_NAMESPACE,
        scope_map=precomputed.skill_scope_map,
    )
    # store 传进去，是为了让 /memories/ 这类【虚拟命名空间】下的文件也能下载：
    # CompositeBackend 把 /memories/ 路由到 StoreBackend（正文在 MySQL
    # agent_state.langgraph_store），容器里并没有那个文件；不传 store 的话
    # 就会出现"Agent 说写好了、下载却说不存在"。
    download_sandbox_file = create_download_tool(
        sandbox_backend, DOWNLOAD_DIR, store=get_store()
    )
    # 常驻内核工具：分析类任务用它，变量留在内存里，不必反复 execute 起进程
    py_exec, py_reset = create_py_exec_tool(sandbox_backend)
    # Text2SQL 工具族：给只读子 Agent 用（主 Agent 不直接查库，仍走委派）
    sql_tools = create_sql_tools()

    # ---- Phase 4: 构建工具池 ----
    logger.info("Phase 4: 构建工具池...")
    # 原始图表工具已被合并后的 generate_visualization 取代 → 先从池中排除
    # extra_mcp_tools 是未能合并、需保留原样的独立工具 → 再加回来
    chart_names = {getattr(t, "name", None) for t in precomputed.toolset.chart_tools}
    candidates = (
        [t for t in precomputed.toolset.all_tools
         if getattr(t, "name", None) not in chart_names]
        + list(precomputed.extra_mcp_tools)
        + [generate_visualization, web_search, request_order_info,
           request_clarification, assign_skill, download_sandbox_file,
           py_exec, py_reset] + sql_tools
    )

    # 工具池由多个来源拼成（多个 MCP Server + 本地工具），
    # 重名会让模型看到两个同名 tool → 显式去重，并告警而不是静默丢弃
    available_tools: list = []
    seen: set = set()
    for t in candidates:
        name = getattr(t, "name", None)
        if name in seen:
            logger.warning(f"  工具重名，已跳过后者: {name}")
            continue
        seen.add(name)
        available_tools.append(t)

    logger.info(f"  工具池: {len(available_tools)} 个工具")

    # ---- Phase 6: 子 Agent 中间件（按 YAML 声明的档位构建）----
    logger.info("Phase 6: 创建子 Agent 中间件...")
    extra_middleware = {
        cfg["name"]: build_subagent_middleware(
            cfg.get("middleware_profile"), SUMMARY_MODEL, backend_factory, cfg["name"]
        )
        for cfg in precomputed.raw_subagent_configs
    }
    logger.info(f"  已为 {len(extra_middleware)} 个子 Agent 构建中间件")
    # ---- Phase 7: 子 Agent 工具解析 ----
    logger.info("Phase 7: 解析子 Agent 工具名称...")
    subagents = resolve_subagent_tools(
        precomputed.raw_subagent_configs,
        available_tools,
        extra_middleware=extra_middleware,
    )
    logger.info(f"  已解析 {len(subagents)} 个子 Agent")

    # ---- Phase 8: 主 Agent 中间件栈 ----
    logger.info("Phase 8: 构建主 Agent 中间件栈...")
    main_middleware = [
        # 1. 沙箱健康守护：每次 agent step 前 ping → 失败自动恢复
        SandboxHealthMiddleware(
            sandbox_backend=sandbox_backend,
            user_id=user_id,
            agents_md_content=ag_md_content.encode("utf-8"),
        ),
        # 2. 任务状态机：钩住 task 委派 → 四态账本（pending/in_progress/completed/failed）
        #    必须排在 ToolErrorMiddleware 之前（更外层），
        #    才能拿到它归一化后的 ToolMessage(status="error")
        TaskStateMiddleware(),
        # 3. 工具错误捕获：wrap_tool_call → ToolMessage(status="error")，防止单工具崩溃
        ToolErrorMiddleware(),
        # 4. 用户上下文注入
        ContextInjectionMiddleware(),
        # 5. 技能同步（本地 → 沙箱）
        SkillsSyncMiddleware(sandbox_backend),
        # 6. 持久化技能恢复（StoreBackend → 沙箱）
        UserSkillsRestoreMiddleware(sandbox_backend, SKILLS_STORE_NAMESPACE),
        # 7. 对话摘要
        build_summarization_middleware(backend_factory, SUMMARY_MODEL),
        # 8. 用户记忆更新
        MemoryUpdateMiddleware(model=SUMMARY_MODEL),
        # 9. 沙箱熔断：连续沙箱错误 ≥ 阈值 → jump_to=end
        SandboxCircuitBreakerMiddleware(),
        # 10. 工具循环熔断：同名同参连续 2 次 → 降级；同名 3 次 → 降级；5 次 → 中止
        ToolLoopGuardMiddleware(),
        # 11. 写操作回退：失败时逆序补偿（补偿不走审批，失败只记录不重试）
        ToolRollbackMiddleware(backend=sandbox_backend),
        # 12. 上下文压缩：日志正则压缩 + 超大结果下沉到沙箱可读目录
        ToolResultCompactionMiddleware(backend=sandbox_backend),
        # 13. 上下文摘要：工具结果累计超 token 预算（或 4 轮兜底）→ 压成一段结论
        ContextSummarizerMiddleware(model=SUMMARY_MODEL),
        # 10. 调用限制（主 Agent）
        #
        # ⚠️ 2026-11-10 收紧：50/200 → 20/40，并补上 thread_limit。
        #
        # 为什么收紧（依据是主 Agent 的真实开销，不是拍脑袋）：
        #   主 Agent 的职责只有三件 — 意图识别、任务拆解、委派汇总。
        #   正常单域任务 3~5 次模型调用；多域任务（销量+库存+采购）10~15 次；
        #   HITL 中断恢复再 +2~4 次。所以 20 已经覆盖极端情况还有余量。
        #   原来的 50 是 5~10 倍余量 —— 作为"防死循环"太松，作为"预算"又没意义。
        #
        #   更重要的用途变化：这个上限现在当**自检线**用 ——
        #   如果主 Agent 单轮经常超过 15 次，说明它没做到"只做规划和委派"，
        #   而是直接调了业务工具。那时要查的是设计，不是继续调大上限。
        #
        # 为什么补 thread_limit（这是原来的真漏洞）：
        #   run_limit 是**每次 invoke** 累计、每轮重置 → 拦不住"连续问 20 轮 × 50 次"。
        #   thread_limit 跨 invoke 累计，150 ≈ 10 轮 × 15 次，够正常对话用，
        #   又能拦住"某个会话一直失控"。
        #   子 Agent 的 profile 刻意不加 thread_limit —— 因为子 Agent 本来就会
        #   被反复委派，加上去会误伤正常的多轮协作。
        ModelCallLimitMiddleware(run_limit=20, thread_limit=150),
        ToolCallLimitMiddleware(run_limit=40, thread_limit=300),
    ]

    # ---- Phase 9: create_deep_agent ----
    logger.info("Phase 9: 创建 Deep Agent...")
    # 登记写操作补偿函数（订单撤销 / 改单还原 / 建档回报人工）
    try:
        register_default_compensators()
    except Exception as exc:  # noqa: BLE001 —— 补偿登记失败不影响主流程
        logger.warning("登记默认补偿函数失败: %s", exc)
    agent_graph = create_deep_agent(
        model=MAIN_MODEL,
        system_prompt=system_prompt,
        skills=["/skills/main/"],
        memory=[AGENTS_MD_FILENAME],
        tools=[web_search, assign_skill, download_sandbox_file,
               py_exec, py_reset, request_clarification],
        subagents=subagents,
        middleware=main_middleware,
        backend=backend_factory,
        store=get_store(),
        checkpointer=get_checkpointer(),
        context_schema=ProcurementContext,
    )

    logger.info(f"=== 用户 {user_id} Agent Graph 创建完成 ===")
    return agent_graph
