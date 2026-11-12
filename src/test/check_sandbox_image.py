"""验证自建沙箱镜像：建沙箱耗时 + 依赖是否立刻可用。

用法（在 ERP_OPENCLAW 下）：
    .venv\\Scripts\\python.exe src\\test\\check_sandbox_image.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agent.config import SANDBOX_CONFIG  # noqa: E402
from agent.backends.sandbox_setup import setup_sandbox  # noqa: E402


def main() -> int:
    print(f"镜像 = {os.getenv('OPENSANDBOX_IMAGE') or '(默认官方镜像)'}")
    t0 = time.time()
    backend = setup_sandbox(SANDBOX_CONFIG)
    cost = time.time() - t0
    print(f"建沙箱总耗时 = {cost:.1f}s")
    print(f"沙箱 ID = {backend.id}")

    check = backend.execute(
        "/opt/skills-venv/bin/python3 -c "
        "'import numpy,pandas,matplotlib,requests,bs4,openpyxl;"
        "print(\"pandas\", pandas.__version__)'"
    )
    print(f"依赖检查 exit={check.exit_code}: {check.output.strip()[:200]}")

    marker = backend.execute("test -f /opt/.skills-venv-ready && echo READY")
    print(f"镜像标记: {marker.output.strip() or '(无)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
