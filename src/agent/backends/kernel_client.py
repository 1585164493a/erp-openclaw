"""常驻内核的客户端侧小工具（重置命名空间）。

工具本身在 `agent/tools/py_exec.py`；这里只放"对话切换时清空内核"这类生命周期逻辑。
"""
from __future__ import annotations

import json
import threading
from typing import Any

_KERNEL_URL = "http://127.0.0.1:9000"

# 记住每个用户上一次的对话 ID：thread 变了就重置内核，实现"一对话一内核"
_LAST_THREAD: dict[str, str] = {}
_LOCK = threading.Lock()


def reset_kernel(backend: Any, ns: str = "default") -> bool:
    """清空内核里该命名空间的所有变量。失败返回 False，不抛异常。"""
    try:
        backend.upload_files(
            [("/tmp/_py_reset_req.json", json.dumps({"ns": ns}).encode("utf-8"))]
        )
        resp = backend.execute(
            "curl -s -m 10 -X POST -H 'Content-Type: application/json' "
            f"--data-binary @/tmp/_py_reset_req.json {_KERNEL_URL}/reset"
        )
        return (resp.exit_code or 0) == 0 and "ok" in (resp.output or "")
    except Exception:  # noqa: BLE001 —— 重置失败不影响对话
        return False


def reset_kernel_if_new_thread(user_id: str, thread_id: str | None,
                               backend: Any) -> None:
    """对话（thread）发生变化时清空内核，避免上一轮的数据串到新对话里。

    同一对话内的多轮不清空 —— 那正是常驻内核的价值所在。
    """
    if not thread_id or backend is None:
        return
    with _LOCK:
        previous = _LAST_THREAD.get(user_id)
        _LAST_THREAD[user_id] = thread_id
    if previous and previous != thread_id:
        reset_kernel(backend)
