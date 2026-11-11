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
    _apply_env()
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception:  # noqa: BLE001 —— 追踪失败绝不能影响主流程
        return None


def attach(config: dict, thread_id: str | None = None, user_id: str | None = None) -> dict:
    """把回调挂到 LangGraph 的 config 上，并把 session/user 写进 trace 元数据。"""
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
    return config


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
