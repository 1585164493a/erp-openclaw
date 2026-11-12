"""上下文压缩：工具结果下沉（可读回）+ 日志正则压缩。

解决两个实测问题：
1. **下沉断链**：框架把大结果 offload 到 `/large_tool_results/`，但那个路径只存在于
   agent 虚拟文件系统，**沙箱里读不到** —— 实测有 agent 用 `execute find /` 满盘找文件，
   白烧 100+ 秒最后放弃。这里改成"写进沙箱 /analysis/scratch/ 并返回可读回的路径"。
2. **日志噪声**：报错/命令行输出里大量时间戳、ANSI 色、重复行、长堆栈，
   这些对模型决策几乎无用，却按全价占窗口。用正则可去掉 60%~90%。

设计原则：
- **只压缩"大"结果**，小结果原样透传（避免无谓的语义损失）；
- **压缩必须可逆**：完整内容一定落盘，且路径可读回；
- **保留信号**：错误行、异常类型、首尾各若干行绝不删。
"""
from __future__ import annotations

import re
import time
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

MARKER = "ContextCompact"

# 超过任一阈值就**下沉**（不是丢弃：写进沙箱，返回可读回路径）
MAX_LINES = 60
MAX_CHARS = 2500
# 下沉后保留的头尾
HEAD_LINES = 12
TAIL_LINES = 4
# 结果里用来标记"完整内容在哪"的机器可读行（历史折叠时会保留它）
SINK_MARK = "【完整结果】"

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TS_PREFIX = re.compile(
    r"^\s*[\[(]?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?[\])]?\s*[-|:]?\s*"
)
_LOG_PREFIX = re.compile(r"^\s*\[?(DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL)\]?\s*", re.I)
_TRACEBACK_FRAME = re.compile(r'^\s*File "[^"]+", line \d+')
_SIGNAL = re.compile(r"(error|exception|traceback|failed|denied|timeout|refused|错误|失败|异常)", re.I)

# 沙箱里可读的落盘目录（/analysis 已存在）
SCRATCH_DIR = "/analysis/scratch"


def _lines(text: str) -> list[str]:
    return text.splitlines()


def is_large(text: str) -> bool:
    return len(_lines(text)) > MAX_LINES or len(text) > MAX_CHARS


def compress_logs(text: str, keep_signal: int = 40) -> str:
    """日志/命令行输出正则压缩：去时间戳、去 ANSI、折叠重复、折叠堆栈、保留错误行。

    返回压缩后的文本；如果不含日志特征，原样返回（不硬压普通表格）。
    """
    if not text:
        return text
    raw = _lines(text)
    if not raw:
        return text

    looks_log = any(_TS_PREFIX.match(l) or _LOG_PREFIX.match(l) or _TRACEBACK_FRAME.match(l)
                    for l in raw[: min(len(raw), 40)])
    if not looks_log:
        return text

    out: list[str] = []
    prev_norm, prev_count = None, 0
    frames_in_group = 0
    signal_kept = 0

    def flush_repeat() -> None:
        nonlocal prev_norm, prev_count
        if prev_count > 1:
            out.append(f"… 上一行重复 {prev_count} 次 …")
        prev_norm, prev_count = None, 0

    for line in raw:
        line = _ANSI.sub("", line).rstrip()
        if not line.strip():
            flush_repeat()
            continue
        norm = _TS_PREFIX.sub("", line)
        norm = _LOG_PREFIX.sub("", norm).strip()

        # 重复行折叠
        if norm == prev_norm:
            prev_count += 1
            continue
        flush_repeat()
        prev_norm, prev_count = norm, 1

        # 堆栈帧折叠：连续 File "...", line N 只留最后 3 帧
        if _TRACEBACK_FRAME.match(norm):
            frames_in_group += 1
            if frames_in_group <= 3:
                out.append(norm)
            elif frames_in_group == 4:
                out.append("… 省略中间堆栈帧 …")
            continue
        frames_in_group = 0

        # 低价值信息行：保留有信号的，其余限量
        if _SIGNAL.search(norm) or len(out) < keep_signal:
            out.append(norm)
            if _SIGNAL.search(norm):
                signal_kept += 1

    flush_repeat()
    if not out:
        return text
    result = "\n".join(out)
    return result if len(result) < len(text) else text


def sink_to_file(backend: Any, text: str, tool_name: str, timeout_s: float = 6.0) -> str | None:
    """把完整结果写进沙箱 /analysis/scratch/，返回**沙箱内可读**的路径。

    ⚠️ 超时保护（2026-11-12 事故）：沙箱慢/半死时 `backend.execute` 是**同步阻塞**调用，
    没有超时会把事件循环卡死 —— 现象是 SSE 无输出、trace 停在上一个 span、整轮十几分钟不动。
    这里用线程 + 超时兜住：超时就放弃下沉，只做纯截断，绝不拖住主流程。
    """
    if backend is None:
        return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", tool_name or "tool")[:40]
    path = f"{SCRATCH_DIR}/{safe}_{ts}.txt"

    def _do() -> str:
        backend.execute(f"mkdir -p {SCRATCH_DIR}")
        backend.upload_files([(path, text.encode("utf-8", "replace"))])
        return path

    pool = None
    try:
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(_do)
        path_out = fut.result(timeout=timeout_s)
        pool.shutdown(wait=False)
        return path_out
    except Exception:  # noqa: BLE001 —— 超时/失败都降级为纯截断
        # 注意：不能用 with ThreadPoolExecutor(...)，它退出时会 wait=True 等线程跑完，
        # 超时形同虚设（实测仍阻塞 30s）。这里显式 shutdown(wait=False) 立即返回。
        if pool is not None:
            try:
                pool.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
        return None


def compact(text: str, tool_name: str, backend: Any) -> str:
    """统一入口：日志压缩 → 仍然过大则下沉（保留头尾 + 可读回路径）。"""
    if not text or not is_large(text):
        return text

    squeezed = compress_logs(text)
    if not is_large(squeezed):
        saved = len(text) - len(squeezed)
        return f"{squeezed}\n\n【{MARKER}】已压缩掉约 {saved} 字符（时间戳/重复行/堆栈噪声）"

    lines = _lines(squeezed)
    path = sink_to_file(backend, squeezed, tool_name)
    # 逐行裁剪：JSON/单行大文本按"行"截没用（一行就是全部），必须按字符裁
    clipped = [l[:210] + ("…" if len(l) > 210 else "") for l in lines]
    head = "\n".join(clipped[:HEAD_LINES])
    tail = "\n".join(clipped[-TAIL_LINES:]) if len(lines) > HEAD_LINES + TAIL_LINES else ""
    note = (
        f"\n\n{SINK_MARK} 共 {len(lines)} 行，只显示头尾。"
        + (f"完整内容：`{path}`（read_file 可读）" if path
           else "需要更多请缩小范围或用 sql_query 聚合。")
    )
    body = head + ("\n…\n" + tail if tail else "")
    return body + note


class ToolResultCompactionMiddleware(AgentMiddleware):
    """工具返回后统一压缩：日志正则 → 仍大则下沉到可读目录。"""

    def __init__(self, backend: Any = None) -> None:
        super().__init__()
        self.backend = backend
    def _post(self, result: Any, tool_name: str) -> Any:
        content = getattr(result, "content", None)
        if not isinstance(content, str):
            return result
        new = compact(content, tool_name, self.backend)
        if new == content:
            return result
        return ToolMessage(
            content=new,
            tool_call_id=getattr(result, "tool_call_id", "") or "",
            name=getattr(result, "name", None) or tool_name,
        )

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        tool_call = getattr(request, "tool_call", {}) or {}
        result = handler(request)
        return self._post(result, tool_call.get("name") or "")

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        tool_call = getattr(request, "tool_call", {}) or {}
        result = await handler(request)
        # 压缩里含沙箱 IO（同步阻塞），放到线程里跑，绝不占用事件循环
        import asyncio

        return await asyncio.to_thread(self._post, result, tool_call.get("name") or "")

# 注：逐条折叠方案已废弃（2026-11-12），改用 context_summary.ContextSummarizerMiddleware
# —— 折叠只砍字符、语义找不回；摘要能把多条结果压成带口径的一段结论。
