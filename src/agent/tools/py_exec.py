"""py_exec：在沙箱的常驻内核里执行 Python 代码（状态可跨调用保留）。

和 `execute("python x.py")` 的区别：
- execute：每次起新进程，重新 import，变量全丢 —— 一条分析 trace 实测 25 次调用、几十秒白花
- py_exec：代码投给常驻进程，`df` 这类变量留在内存里，第二次之后几乎零启动成本

实现上不引入任何依赖：请求体写到沙箱 /tmp 下的 json，再用 curl 打本地 9000 端口。
"""
from __future__ import annotations

import json
from typing import Any

_KERNEL_URL = "http://127.0.0.1:9000"


def create_py_exec_tool(sandbox_backend: Any, default_ns: str = "default"):
    """创建 py_exec 工具。

    Args:
        sandbox_backend: 沙箱后端（提供 execute / upload_files）。
        default_ns: 默认命名空间；一轮对话共享同一个即可。
    """

    def py_exec(code: str, timeout: int = 30, ns: str = "") -> str:
        """在沙箱的常驻 Python 内核里执行代码，变量会保留给后续调用。

        Args:
            code: 要执行的 Python 代码。用 print() 输出想看的内容。
            timeout: 超时秒数（默认 30）。超时后该命名空间会被标记不可信，需 py_reset。
            ns: 命名空间，一般不用填。

        Returns:
            标准输出；出错时返回 traceback 摘要。
        """
        namespace = ns or default_ns
        payload = json.dumps(
            {"code": code, "ns": namespace, "timeout": int(timeout)}, ensure_ascii=False
        ).encode("utf-8")

        # 走文件通道传请求体：避免把代码拼进 shell 命令时的引号/换行地狱
        sandbox_backend.upload_files([("/tmp/_py_exec_req.json", payload)])
        cmd = (
            f"curl -s -m {int(timeout) + 15} -X POST "
            "-H 'Content-Type: application/json' "
            "--data-binary @/tmp/_py_exec_req.json "
            f"{_KERNEL_URL}/exec"
        )
        resp = sandbox_backend.execute(cmd, timeout=int(timeout) + 25)
        raw = (resp.output or "").strip()
        if not raw:
            return "py_exec 无响应：内核可能未启动（可改用 execute 兜底）"
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return f"py_exec 返回不可解析：{raw[:500]}"

        out = (data.get("stdout") or "").rstrip()
        if data.get("ok"):
            return out or "(执行成功，但没有任何输出——记得用 print)"
        err = (data.get("error") or "未知错误").strip()
        return f"{out}\n[错误]\n{err[:1500]}" if out else f"[错误]\n{err[:1500]}"

    def py_reset(ns: str = "") -> str:
        """清空常驻内核里该命名空间的所有变量（执行超时或状态混乱时用）。

        Args:
            ns: 命名空间，一般不用填。
        """
        namespace = ns or default_ns
        sandbox_backend.upload_files(
            [("/tmp/_py_reset_req.json", json.dumps({"ns": namespace}).encode("utf-8"))]
        )
        resp = sandbox_backend.execute(
            "curl -s -m 10 -X POST -H 'Content-Type: application/json' "
            f"--data-binary @/tmp/_py_reset_req.json {_KERNEL_URL}/reset"
        )
        return (resp.output or "").strip() or "reset 已提交"

    py_exec.name = "py_exec"
    py_exec.description = (
        "在沙箱的常驻 Python 内核里执行代码，**变量会保留**（前一次加载的 DataFrame 下一次还能用）。"
        "适合数据分析、批量计算、生成图表；不要用它跑长驻服务。"
        "用 print() 输出结果。超时（默认 30s）后命名空间失效，需要先 py_reset。"
    )
    py_reset.name = "py_reset"
    py_reset.description = "清空常驻内核里的变量（状态混乱或执行超时后使用）。"
    return py_exec, py_reset
