"""工具失败回退（补偿）中间件。

分工（这是关键，别混）：
- **工具层**：提供 `compensate(args, result) -> str`，撤掉这次调用产生的副作用。
  只有工具自己知道"主单 id 是多少、插了几条明细"。
- **中间件层（本文件）**：记录本轮成功的写操作 → 失败时**逆序**调用补偿；沙箱文件写由它
  直接备份/恢复（文件原内容它读得到，不需要工具配合）。

三条已确认的规则（2026-11-12 与用户确认）：
1. **补偿不再走人工审批** —— 它是"撤销"，方向是收紧；
2. **补偿失败不静默重试** —— 记录 + 明确告知"有 N 条未撤销，需人工处理"；
3. **自动撤销审批过的写操作可以接受**，但必须在回答里明确告诉用户"已撤销 X"。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from agent.subagents.loader import WRITE_TOOLS

MARKER = "ToolRollback"

# 工具名 → 补偿函数：fn(args, result[, snapshot]) -> str（返回人话描述）
COMPENSATORS: dict[str, Callable[..., str]] = {}
# 工具名 → 执行前快照函数：fn(args) -> Any（用于"改之前是什么"）
SNAPSHOTTERS: dict[str, Callable[[dict], Any]] = {}
# 沙箱里需要"写前备份"的工具
_FILE_WRITE_TOOLS = {"write_file", "edit_file"}


def register_compensator(tool_name: str, fn: Callable[..., str]) -> None:
    """工具工厂在创建工具时登记自己的补偿函数。"""
    COMPENSATORS[tool_name] = fn


def register_snapshotter(tool_name: str, fn: Callable[[dict], Any]) -> None:
    """登记"执行前快照"，供需要对比旧值的补偿（如 update）使用。"""
    SNAPSHOTTERS[tool_name] = fn


def _call_compensator(fn: Callable[..., str], args: dict, result: Any, snapshot: Any) -> str:
    """兼容 2 参数与 3 参数两种补偿函数签名。"""
    try:
        return fn(args, result, snapshot)
    except TypeError:
        return fn(args, result)


def _text_of(result: Any) -> str:
    content = getattr(result, "content", result)
    return content if isinstance(content, str) else str(content)


def _looks_failed(result: Any) -> bool:
    if getattr(result, "status", None) == "error":
        return True
    text = _text_of(result).lstrip()
    return (
        text.startswith("[SQL 执行失败]")
        or text.startswith("[SQL 被拒绝]")
        or text.startswith("[错误]")
        or "Traceback (most recent call last)" in text
    )


class ToolRollbackMiddleware(AgentMiddleware):
    """写操作失败时逆序补偿；沙箱文件写失败时恢复原内容。"""

    def __init__(self, backend: Any = None, compensators: dict | None = None) -> None:
        super().__init__()
        self.backend = backend
        if compensators:
            COMPENSATORS.update(compensators)
        # 本轮成功的写操作栈：(工具名, 入参, 结果, 执行前快照)
        self._done: list[tuple[str, dict, Any, Any]] = []
        # 文件写前的快照：路径 → 原内容（None 表示原本不存在）
        self._files: dict[str, bytes | None] = {}

    # ---------------- 文件快照 ----------------
    def _snapshot(self, path: str) -> None:
        if path in self._files or self.backend is None:
            return
        try:
            res = self.backend.download_files([path])
            content = res[0].content if res and res[0] and not res[0].error else None
            if isinstance(content, str):
                content = content.encode("utf-8")
            self._files[path] = content
        except Exception:  # noqa: BLE001
            self._files[path] = None

    def _restore(self, path: str) -> str:
        original = self._files.get(path, "missing")
        if original == "missing":
            return f"{path}: 未记录快照，跳过"
        try:
            if original is None:          # 原本不存在 → 删掉新建的文件
                if self.backend is not None:
                    self.backend.execute(f"rm -f {path}")
                return f"{path}: 已删除（原文件不存在）"
            if self.backend is not None:
                self.backend.upload_files([(path, original)])
            return f"{path}: 已恢复原内容"
        except Exception as e:  # noqa: BLE001
            return f"{path}: 恢复失败（{e}）"

    # ---------------- 补偿 ----------------
    def _rollback(self, failed_tool: str) -> str:
        """逆序补偿本轮所有成功的写操作，返回人话报告。"""
        if not self._done:
            return ""
        lines: list[str] = []
        failed_comp: list[str] = []
        for name, args, result, snapshot in reversed(self._done):
            fn = COMPENSATORS.get(name)
            if fn is None:
                failed_comp.append(f"{name}（无补偿函数）")
                continue
            try:
                detail = _call_compensator(fn, args, result, snapshot)
                lines.append(f"已撤销 {name}：{detail}")
            except Exception as e:  # noqa: BLE001 —— 补偿失败不重试，只记录
                failed_comp.append(f"{name}（{e.__class__.__name__}: {str(e)[:80]}）")
        self._done.clear()
        parts = []
        if lines:
            parts.append("【已自动撤销】\n" + "\n".join(f"- {x}" for x in lines))
        if failed_comp:
            parts.append(
                f"【未能撤销 {len(failed_comp)} 条，需人工处理】\n"
                + "\n".join(f"- {x}" for x in failed_comp)
            )
        return "\n\n".join(parts)

    # ---------------- 钩子 ----------------
    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        tool_call = getattr(request, "tool_call", {}) or {}
        name = tool_call.get("name") or ""
        args = tool_call.get("args") or {}

        # 文件写：写前快照
        if name in _FILE_WRITE_TOOLS and isinstance(args, dict):
            path = args.get("file_path") or args.get("path")
            if isinstance(path, str) and path:
                self._snapshot(path)

        # 业务写操作：执行前快照（如 update 需要"改之前是什么"）
        snapshot = None
        snap_fn = SNAPSHOTTERS.get(name)
        if snap_fn is not None and isinstance(args, dict):
            try:
                snapshot = snap_fn(args)
            except Exception:  # noqa: BLE001 —— 快照失败不阻塞执行
                snapshot = None

        try:
            result = handler(request)
        except Exception as e:  # noqa: BLE001
            report = self._rollback(name)
            msg = f"{MARKER}: 工具 {name} 抛异常：{e}"
            if report:
                msg += f"\n\n{report}"
            raise RuntimeError(msg) from e

        if _looks_failed(result):
            report = self._rollback(name)
            if name in _FILE_WRITE_TOOLS:
                path = args.get("file_path") or args.get("path") if isinstance(args, dict) else None
                if isinstance(path, str) and path:
                    report = (report + "\n" if report else "") + f"【文件回退】{self._restore(path)}"
            if report:
                return ToolMessage(
                    content=f"{_text_of(result)}\n\n{report}",
                    tool_call_id=tool_call.get("id") or "",
                    name=name,
                )
            return result

        # 成功：只有写操作进栈
        if name in WRITE_TOOLS:
            self._done.append((name, args if isinstance(args, dict) else {}, result, snapshot))
        return result

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        # 文件读写与补偿都是阻塞 IO，直接复用同步实现
        tool_call = getattr(request, "tool_call", {}) or {}
        name = tool_call.get("name") or ""
        args = tool_call.get("args") or {}

        if name in _FILE_WRITE_TOOLS and isinstance(args, dict):
            path = args.get("file_path") or args.get("path")
            if isinstance(path, str) and path:
                self._snapshot(path)

        snapshot = None
        snap_fn = SNAPSHOTTERS.get(name)
        if snap_fn is not None and isinstance(args, dict):
            try:
                snapshot = snap_fn(args)
            except Exception:  # noqa: BLE001
                snapshot = None

        try:
            result = await handler(request)
        except Exception as e:  # noqa: BLE001
            report = self._rollback(name)
            msg = f"{MARKER}: 工具 {name} 抛异常：{e}"
            if report:
                msg += f"\n\n{report}"
            raise RuntimeError(msg) from e

        if _looks_failed(result):
            report = self._rollback(name)
            if name in _FILE_WRITE_TOOLS:
                path = args.get("file_path") or args.get("path") if isinstance(args, dict) else None
                if isinstance(path, str) and path:
                    report = (report + "\n" if report else "") + f"【文件回退】{self._restore(path)}"
            if report:
                return ToolMessage(
                    content=f"{_text_of(result)}\n\n{report}",
                    tool_call_id=tool_call.get("id") or "",
                    name=name,
                )
            return result

        if name in WRITE_TOOLS:
            self._done.append((name, args if isinstance(args, dict) else {}, result, snapshot))
        return result
