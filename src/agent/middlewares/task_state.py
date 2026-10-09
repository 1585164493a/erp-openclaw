"""
任务状态机中间件（子 Agent 委派账本）。

职责
----
在中间件层为「子 Agent 委派」维护一本独立的任务账本：

1. 拦截 `task` 委派工具的进出（`awrap_tool_call` / `wrap_tool_call`）
2. 委派发起 -> 建任务（pending）并按状态机转为 in_progress
3. 委派返回 -> 按结果转为 completed / failed
4. 所有迁移统一走 TRANSITIONS 校验，非法迁移直接抛错

状态机
------
    pending --> in_progress --> completed
                    |
                    +--> failed --> in_progress（重试通道）

为什么不用内置的 write_todos？
------------------------------
`TodoListMiddleware` 提供的是给模型看的规划草稿纸：Todo 没有 id 字段、
`write_todos` 全量覆盖、框架还鼓励模型删掉「不再相关」的任务，
并且明确要求失败时「保持 in_progress」。
它无法回答「哪些委派失败了、失败了几次」，因此不能替代任务账本。

放置位置
--------
必须排在 `ToolErrorMiddleware` **之前**（即更外层）。
LangChain 把中间件链成洋葱：列表靠前 = 更外层
（见 `langchain.agents.factory._chain_tool_call_wrappers`：first = outermost）。
放在外层才能在 `handler()` 返回后拿到
`ToolErrorMiddleware` 转换好的 `ToolMessage(status="error")`，
否则只能收到原始异常，等于把错误捕获重写一遍。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command
from pymongo import MongoClient

# 开发环境根 logger 级别是 ERROR（见 main_agent._setup_logging），
# 这里显式放开本模块的 INFO，便于观察委派进出。
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------- 状态定义 ----------
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"

# 状态机的全部合法迁移边
TRANSITIONS: dict[str, set[str]] = {
    PENDING: {IN_PROGRESS},
    IN_PROGRESS: {COMPLETED, FAILED},
    FAILED: {IN_PROGRESS},  # 重试通道：失败任务可重新发起
    COMPLETED: set(),       # 终态，不可翻案
}

# 被追踪的工具名（deepagents SubAgentMiddleware 注入的委派工具）
TASK_TOOL_NAME = "task"

# MongoDB 集合名
TASK_COLLECTION_NAME = "session_tasks"

# task 工具在 subagent_type 非法时返回这段文本（而不是抛异常）
_FAILURE_MARKERS = ("We cannot invoke subagent",)


class IllegalTransition(ValueError):
    """非法状态迁移。"""


class TaskNotFound(LookupError):
    """任务不存在。"""


def _now() -> str:
    """人类可读时间戳，与 session_display_messages 的写法保持一致。"""
    return datetime.now().isoformat(timespec="seconds")


# ============================================================
# 存储层：任务账本
# ============================================================

_client: Optional[MongoClient] = None
_index_ready = False


def _default_collection(name: str):
    """惰性创建 MongoDB 集合：首次使用时才连库、建索引。

    刻意把 `agent.config` 的导入放在函数内，避免模块导入期就建立连接。
    """
    global _client, _index_ready
    if _client is None:
        from agent.config import MONGODB_DB_NAME, MONGODB_URI

        _client = MongoClient(MONGODB_URI)
    coll = _client[MONGODB_DB_NAME][name]
    if not _index_ready:
        coll.create_index([("thread_id", 1), ("tool_call_id", 1)])
        coll.create_index([("thread_id", 1), ("created_at", -1)])
        _index_ready = True
    return coll


class TaskLedger:
    """任务账本：一次子 Agent 委派 = 一条任务记录。

    与对话同生命周期：`thread_id` 是主键，
    会话删除时由 AgentLoader.delete_session 一并清理。
    """

    def __init__(self, collection=None, collection_name: str = TASK_COLLECTION_NAME) -> None:
        self._collection = collection
        self._collection_name = collection_name
        self._resolved = collection is not None

    @property
    def collection(self):
        if not self._resolved:
            self._collection = _default_collection(self._collection_name)
            self._resolved = True
        return self._collection

    # ---- 建 ----
    def create(
        self,
        *,
        thread_id: str,
        tool_call_id: str,
        agent: str,
        intent: str,
        user_id: Optional[str] = None,
    ) -> dict:
        """建任务，出生状态只能是 pending。"""
        now = _now()
        task = {
            "task_id": uuid.uuid4().hex[:12],
            "thread_id": thread_id,
            "tool_call_id": tool_call_id,  # 对号入座的唯一凭据
            "agent": agent,                # 哪个子 Agent 领活
            "intent": intent,              # 委派描述，即原子子任务
            "status": PENDING,
            "detail": "",
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
        }
        self.collection.insert_one(dict(task))
        return task

    # ---- 迁移 ----
    def transition(
        self,
        thread_id: str,
        tool_call_id: str,
        status: str,
        detail: Optional[str] = None,
    ) -> dict:
        """按 TRANSITIONS 校验后迁移状态，非法迁移抛 IllegalTransition。"""
        current = self.collection.find_one(
            {"thread_id": thread_id, "tool_call_id": tool_call_id},
            sort=[("created_at", -1)],
        )
        if current is None:
            raise TaskNotFound(f"任务不存在: thread={thread_id}, call={tool_call_id}")

        allowed = TRANSITIONS.get(current.get("status"), set())
        if status not in allowed:
            raise IllegalTransition(
                f"非法状态迁移: {current.get('status')} -> {status}"
            )

        update: dict[str, Any] = {"status": status, "updated_at": _now()}
        if detail is not None:
            update["detail"] = detail[:500]
        self.collection.update_one(
            {"thread_id": thread_id, "tool_call_id": tool_call_id},
            {"$set": update},
        )
        return {**current, **update}

    # ---- 查 ----
    def list_by_thread(self, thread_id: str, limit: int = 200) -> list[dict]:
        """按会话查任务，新的在前。"""
        cursor = (
            self.collection.find({"thread_id": thread_id})
            .sort("created_at", -1)
            .limit(limit)
        )
        return list(cursor)

    def get(self, thread_id: str, tool_call_id: str) -> Optional[dict]:
        return self.collection.find_one(
            {"thread_id": thread_id, "tool_call_id": tool_call_id}
        )


# ============================================================
# ToolCallRequest 取值助手
# 参考 middlewares/tool_error.py 的防御式写法
# ============================================================

def _tool_call(request) -> Any:
    return getattr(request, "tool_call", None)


def _tool_name(request) -> Optional[str]:
    tc = _tool_call(request)
    if isinstance(tc, dict):
        return tc.get("name")
    return getattr(tc, "name", None)


def _tool_args(request) -> dict:
    tc = _tool_call(request)
    args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
    return args if isinstance(args, dict) else {}


def _tool_call_id(request) -> Optional[str]:
    """优先取 runtime.tool_call_id，回退到 tool_call['id']。"""
    runtime = getattr(request, "runtime", None)
    tcid = getattr(runtime, "tool_call_id", None)
    if tcid:
        return tcid
    tc = _tool_call(request)
    if isinstance(tc, dict):
        return tc.get("id")
    return getattr(tc, "id", None)


def _thread_id(request) -> Optional[str]:
    """从 runtime.config 取当前会话 id，用于把任务绑定到会话。"""
    runtime = getattr(request, "runtime", None)
    config = getattr(runtime, "config", None) or {}
    configurable = config.get("configurable") or {}
    return configurable.get("thread_id")


def _user_id(request) -> Optional[str]:
    runtime = getattr(request, "runtime", None)
    ctx = getattr(runtime, "context", None)
    return getattr(ctx, "user_id", None)


# ============================================================
# 结果判定
# ============================================================

def _message_text(msg) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    return str(content)


def _extract_tool_message(result) -> Optional[ToolMessage]:
    """从 ToolMessage / Command 两种返回形态中取出 ToolMessage。"""
    if isinstance(result, ToolMessage):
        return result
    if isinstance(result, Command):
        update = getattr(result, "update", None) or {}
        messages = update.get("messages") if isinstance(update, dict) else None
        for msg in reversed(messages or []):
            if isinstance(msg, ToolMessage):
                return msg
    return None


def _judge(result) -> tuple[bool, str]:
    """判定委派结果 -> (是否失败, 详情摘要)。"""
    if isinstance(result, BaseException):
        return True, f"{type(result).__name__}: {result}"

    msg = _extract_tool_message(result)
    if msg is None:
        return False, "无法解析工具返回值，按成功记录"

    text = _message_text(msg)

    # 1) ToolErrorMiddleware 归一化的错误
    if getattr(msg, "status", None) == "error":
        return True, text

    # 2) subagent_type 非法时 task 返回的提示文本（仍是 success）
    for marker in _FAILURE_MARKERS:
        if marker in text:
            return True, text

    if not text.strip():
        return False, "子 Agent 返回空内容"

    return False, text


# ============================================================
# 中间件
# ============================================================

class TaskStateMiddleware(AgentMiddleware):
    """把 `task` 子 Agent 委派记录成一本四态任务账本。

    设计原则：账本绝不能拖垮主流程。
    建任务 / 结算失败都只记日志，不向上抛。
    """

    state_schema = AgentState

    def __init__(self, ledger: Optional[TaskLedger] = None, *, enabled: bool = True) -> None:
        super().__init__()
        self._ledger = ledger or TaskLedger()
        self._enabled = enabled

    # ---- 委派发起 ----
    def _start(self, request) -> None:
        if not self._enabled:
            return
        try:
            thread_id = _thread_id(request)
            tool_call_id = _tool_call_id(request)
            if not thread_id or not tool_call_id:
                logger.warning(
                    "TaskState: 缺少 thread_id/tool_call_id，跳过建任务 "
                    "(thread=%s, call=%s)", thread_id, tool_call_id,
                )
                return

            args = _tool_args(request)
            task = self._ledger.create(
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                agent=str(args.get("subagent_type", "?")),
                intent=str(args.get("description", ""))[:500],
                user_id=_user_id(request),
            )
            self._ledger.transition(thread_id, tool_call_id, IN_PROGRESS)
            logger.info(
                "TaskState: 委派开始 task_id=%s agent=%s thread=%s call=%s",
                task["task_id"], task["agent"], thread_id, tool_call_id,
            )
        except Exception as e:  # noqa: BLE001 - 账本失败不影响业务
            logger.warning("TaskState: 建任务失败（已忽略）: %s: %s", type(e).__name__, e)

    # ---- 委派结束 ----
    def _finish(self, request, result) -> None:
        if not self._enabled:
            return
        try:
            thread_id = _thread_id(request)
            tool_call_id = _tool_call_id(request)
            if not thread_id or not tool_call_id:
                return

            failed, detail = _judge(result)
            status = FAILED if failed else COMPLETED
            task = self._ledger.transition(thread_id, tool_call_id, status, detail)
            logger.info(
                "TaskState: 委派结束 task_id=%s status=%s agent=%s",
                task.get("task_id"), status, task.get("agent"),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("TaskState: 结算失败（已忽略）: %s: %s", type(e).__name__, e)

    def _abort(self, request, exc: BaseException) -> None:
        """handler 抛异常时把任务标记为 failed（随后原样抛回）。"""
        if not self._enabled:
            return
        try:
            thread_id = _thread_id(request)
            tool_call_id = _tool_call_id(request)
            if thread_id and tool_call_id:
                self._ledger.transition(
                    thread_id, tool_call_id, FAILED,
                    f"{type(exc).__name__}: {exc}",
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("TaskState: 异常结算失败（已忽略）: %s: %s", type(e).__name__, e)

    # ---- hooks ----
    def wrap_tool_call(self, request, handler):
        if _tool_name(request) != TASK_TOOL_NAME:
            return handler(request)
        self._start(request)
        try:
            result = handler(request)
        except GraphBubbleUp:
            # 中断 / 控制流信号不是失败：原样上抛，任务保持 in_progress
            raise
        except Exception as e:
            self._abort(request, e)
            raise
        self._finish(request, result)
        return result

    async def awrap_tool_call(self, request, handler):
        if _tool_name(request) != TASK_TOOL_NAME:
            return await handler(request)
        self._start(request)
        try:
            result = await handler(request)
        except GraphBubbleUp:
            # 中断 / 控制流信号不是失败：原样上抛，任务保持 in_progress
            raise
        except Exception as e:
            self._abort(request, e)
            raise
        self._finish(request, result)
        return result
