"""验证常驻内核：状态是否真的跨调用保留、reset 是否真的清空。

用法（在 ERP_OPENCLAW 下）：
    .venv\\Scripts\\python.exe src\\test\\check_kernel.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agent.config import SANDBOX_CONFIG  # noqa: E402
from agent.backends.sandbox_setup import setup_sandbox  # noqa: E402


def call(backend, code: str, timeout: int = 30) -> dict:
    payload = json.dumps({"code": code, "ns": "default", "timeout": timeout},
                         ensure_ascii=False).encode("utf-8")
    backend.upload_files([("/tmp/_k.json", payload)])
    r = backend.execute(
        "curl -s -m %d -X POST -H 'Content-Type: application/json' "
        "--data-binary @/tmp/_k.json http://127.0.0.1:9000/exec" % (timeout + 15)
    )
    raw = (r.output or "").strip()
    try:
        return json.loads(raw)
    except Exception:
        return {"ok": False, "error": f"不可解析: {raw[:300]}"}


def main() -> int:
    t0 = time.time()
    backend = setup_sandbox(SANDBOX_CONFIG)
    print(f"建沙箱 {time.time() - t0:.1f}s")

    h = backend.execute("curl -s -m 3 http://127.0.0.1:9000/health")
    print(f"health: {h.output.strip() or '(无响应)'}")

    a = call(backend, "import pandas as pd\nx = 41\nprint('第一次执行，x =', x)")
    print(f"exec#1 ok={a.get('ok')} stdout={ (a.get('stdout') or '').strip() }")

    b = call(backend, "print('第二次执行，x + 1 =', x + 1)\nprint(pd.__version__)")
    print(f"exec#2 ok={b.get('ok')} stdout={ (b.get('stdout') or '').strip() }")

    backend.execute(
        "curl -s -m 10 -X POST -H 'Content-Type: application/json' "
        "-d '{\"ns\":\"default\"}' http://127.0.0.1:9000/reset"
    )
    c = call(backend, "print(x)")
    print(f"reset 后访问 x: ok={c.get('ok')} error={(c.get('error') or '').strip().splitlines()[-1:] }")
    return 0


if __name__ == "__main__":
    sys.exit(main())
