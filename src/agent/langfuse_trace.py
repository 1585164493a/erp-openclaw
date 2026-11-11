"""Langfuse 追踪接入（可选、无侵入）。

踩过的坑（2026-11-10）：
    主 `.env` 里有一份**指向 Langfuse Cloud 的历史配置**（`LANGFUSE_PUBLIC_KEY=pk-lf-1cf7...`，
    且没有 `LANGFUSE_HOST`），而 `src/agent/env_utils.py` 会 `load_dotenv(override=True)`。
    只要它在我的模块之后再跑一次，环境变量就被改回 Cloud 的 key → trace 发去云端，
    本地实例上什么都看不到（还不报错）。

所以这里的规矩是：**配置只认 `.env.langfuse`，并且在每次取 handler 前强制写回 os.environ**，
不管谁之前改过环境变量。没配这个文件时完全静默，不改变原有行为。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

# src/agent/langfuse_trace.py -> parents[0]=agent, [1]=src, [2]=项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = {k: v for k, v in dotenv_values(_PROJECT_ROOT / ".env.langfuse").items() if v}

_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "LANGFUSE_BASE_URL")

# 默认**保留**中间件 span（可以据此排查「同步/中间件」耗时）；嫌吵就在 .env.langfuse 里
# 设 LANGFUSE_HIDE_MIDDLEWARE=1，只上报业务节点（LLM / 工具 / 根节点）。
_DROP_NAME_HINTS = ("Middleware",) if _CONFIG.get("LANGFUSE_HIDE_MIDDLEWARE") == "1" else ()

_client_ready = False
# （改这一行是为了触发 uvicorn 重新加载，让上面读到的 .env.langfuse 生效）


def _should_export(span) -> bool:
    try:
        name = getattr(span, "name", "") or ""
    except Exception:  # noqa: BLE001
        return True
    return not any(h in name for h in _DROP_NAME_HINTS)


def _ensure_client() -> None:
    """显式建 Langfuse 客户端，挂上 span 过滤。

    CallbackHandler 用的是全局单例客户端，所以要先把它建好，
    否则构造 handler 时才会用默认配置（不过滤中间件）。
    """
    global _client_ready
    if _client_ready or not is_enabled():
        return
    _client_ready = True
    _apply_env()
    try:
        from langfuse import Langfuse

        Langfuse(
            public_key=_CONFIG.get("LANGFUSE_PUBLIC_KEY"),
            secret_key=_CONFIG.get("LANGFUSE_SECRET_KEY"),
            host=_CONFIG.get("LANGFUSE_HOST"),
            should_export_span=_should_export,
        )
    except Exception:  # noqa: BLE001 —— 已有客户端就用现成的，别把主流程搞挂
        pass


def is_enabled() -> bool:
    """是否配置了本地 Langfuse（没配就什么都不做）。"""
    return bool(_CONFIG.get("LANGFUSE_PUBLIC_KEY") and _CONFIG.get("LANGFUSE_SECRET_KEY"))


def _apply_env() -> None:
    """把 .env.langfuse 的值强制写回环境变量（覆盖主 .env 里那份 Cloud 配置）。"""
    for key in _KEYS:
        os.environ.pop(key, None)
    os.environ.update(_CONFIG)


# 模块被 import 时就先写一次：这样调用方直接用 langfuse 的 @observe / get_client 也能拿到正确配置
_apply_env()


def get_callback() -> Any | None:
    """返回一个 LangChain 回调 handler；未配置或导入失败时返回 None。"""
    if not is_enabled():
        return None
    _ensure_client()
    _apply_env()
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception:  # noqa: BLE001 —— 追踪失败绝不能影响主流程
        return None


def attach(
    config: dict,
    thread_id: str | None = None,
    user_id: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """把回调挂到 LangGraph 的 config 上，并把 session/user/名字/标签写进 trace。"""
    handler = get_callback()
    if handler is None:
        return config

    callbacks = config.setdefault("callbacks", [])
    callbacks.append(handler)

    metadata = config.setdefault("metadata", {})
    if thread_id:
        # 同一 thread 的多轮会归到一个 Session
        metadata.setdefault("langfuse_session_id", thread_id)
    if user_id:
        metadata.setdefault("langfuse_user_id", user_id)
    if trace_name:
        # 比依赖 LangChain 的 run_name 更可靠：Langfuse 直接认这个 key
        metadata.setdefault("langfuse_trace_name", trace_name)
    if tags:
        metadata.setdefault("langfuse_tags", tags)
    return config


def finalize_trace(question: str, answer: str) -> None:
    """把 trace 的 input/output 改成纯文本。

    默认 LangChain 集成会把整坨 {"messages":[...]} 当 input/output，
    界面上读不动、judge 也难判。这里覆盖成「用户问题 / 最终回答」。
    """
    if not is_enabled():
        return
    _apply_env()
    try:
        from langfuse import get_client

        get_client().set_current_trace_io(input=question or "", output=answer or "")
    except Exception:  # noqa: BLE001
        pass


def patch_trace_io(thread_id: str, question: str, answer: str,
                   attempts: int = 6, wait_seconds: float = 3.0) -> bool:
    """后台任务：把这条 trace 的 input/output 覆盖成纯文本。

    为什么不用 set_current_trace_io：流结束时会话上下文已经退出，
    `set_current_trace_io` 静默失效（实测 2026-11-11：trace 里还是整坨 messages JSON）。
    改成按 sessionId(=thread_id) 反查 trace，再用 ingestion 的 trace-create 事件 upsert。
    """
    if not is_enabled():
        return False
    pk, sk = _CONFIG.get("LANGFUSE_PUBLIC_KEY"), _CONFIG.get("LANGFUSE_SECRET_KEY")
    host = (_CONFIG.get("LANGFUSE_HOST") or "").rstrip("/")
    if not (pk and sk and host and thread_id):
        return False

    import time
    import uuid

    import httpx

    auth = (pk, sk)
    for _ in range(attempts):
        time.sleep(wait_seconds)                       # trace 是异步入库的，等它出现
        try:
            with httpx.Client(timeout=15.0) as c:
                r = c.get(f"{host}/api/public/traces",
                          params={"sessionId": thread_id, "limit": 1}, auth=auth)
                data = (r.json() or {}).get("data") or []
                if not data:
                    continue
                t = data[0]
                event = {
                    "id": str(uuid.uuid4()),
                    "type": "trace-create",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    "body": {
                        "id": t.get("id"),
                        "name": t.get("name"),
                        "sessionId": t.get("sessionId"),
                        "userId": t.get("userId"),
                        "tags": t.get("tags") or [],
                        "metadata": t.get("metadata") or {},
                        "input": question or "",
                        "output": answer or "",
                    },
                }
                resp = c.post(f"{host}/api/public/ingestion", json={"batch": [event]}, auth=auth)
                if resp.status_code in (200, 207):
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def flush() -> None:
    """SDK 是异步批量上报的，请求结束前必须 flush，否则 trace 可能没发出去。"""
    if not is_enabled():
        return
    _apply_env()
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:  # noqa: BLE001
        pass
