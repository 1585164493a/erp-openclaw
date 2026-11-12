"""沙箱内的常驻代码执行服务（纯标准库，不依赖 fastapi/uvicorn）。

为什么存在：Agent 每次 `python xxx.py` 都要起进程 + 重新 import pandas，
一条分析 trace 里实测 25 次 execute、约 30~50 秒全花在启动和重复 import 上。
这里把状态留在常驻进程的 globals 里，第二次之后几乎零启动成本。

协议（只用 curl 调，所以不引入任何第三方依赖）：
    GET  /health              -> {"ok": true, "ns": <命名空间个数>}
    POST /reset {"ns": "..."} -> 清空该命名空间的变量
    POST /exec  {"ns": "...", "code": "...", "timeout": 30}
                              -> {"ok": true, "stdout": "...", "result": "..."}
                                 或 {"ok": false, "stdout": "...", "error": "..."}
"""
from __future__ import annotations

import contextlib
import io
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 命名空间 -> globals 字典。默认只有一个，按需可以多个（每对话一个）。
NAMESPACES: dict[str, dict] = {}
LOCK = threading.Lock()          # 同一时刻只跑一段代码，避免状态互相踩
TAINTED: set[str] = set()        # 超时过的命名空间：不再执行，等 reset


def _namespace(ns: str) -> dict:
    return NAMESPACES.setdefault(ns or "default", {"__name__": "__main__"})


def _run(code: str, ns: str, timeout: float) -> dict:
    g = _namespace(ns)
    if ns in TAINTED:
        return {"ok": False, "stdout": "",
                "error": "该命名空间上次执行超时，状态不可信；请先 /reset"}

    buf = io.StringIO()
    box: dict = {}

    def target() -> None:
        try:
            with contextlib.redirect_stdout(buf):
                exec(code, g)                      # noqa: S102 —— 沙箱内执行，边界由容器提供
            box["ok"] = True
        except Exception:  # noqa: BLE001
            box["ok"] = False
            box["error"] = traceback.format_exc()

    with LOCK:
        th = threading.Thread(target=target, daemon=True)
        th.start()
        th.join(timeout)

    if th.is_alive():
        # 线程杀不掉，只能标记不可信，避免下次读到半执行的状态
        TAINTED.add(ns)
        return {"ok": False, "stdout": buf.getvalue(),
                "error": f"执行超时（>{timeout:g}s），命名空间已标记不可信，请 /reset 后重试"}

    if box.get("ok"):
        return {"ok": True, "stdout": buf.getvalue()}
    return {"ok": False, "stdout": buf.getvalue(), "error": box.get("error", "")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:        # 静音，避免刷日志
        return

    def _reply(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                     # noqa: N802
        if self.path.startswith("/health"):
            self._reply({"ok": True, "ns": len(NAMESPACES)})
        else:
            self._reply({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:                    # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            self._reply({"ok": False, "error": "invalid json"}, 400)
            return

        if self.path.startswith("/reset"):
            ns = req.get("ns") or "default"
            NAMESPACES.pop(ns, None)
            TAINTED.discard(ns)
            self._reply({"ok": True, "ns": ns})
            return

        if self.path.startswith("/exec"):
            code = req.get("code") or ""
            if not code.strip():
                self._reply({"ok": False, "error": "empty code"}, 400)
                return
            result = _run(code, req.get("ns") or "default",
                          float(req.get("timeout") or 30))
            self._reply(result)
            return

        self._reply({"ok": False, "error": "not found"}, 404)


def main() -> None:
    port = 9000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[kernel] listening on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
