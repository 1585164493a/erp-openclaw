"""验证沙箱内的 DB 直连守卫：应当 1 秒内失败并提示改用 sql_query。"""
from __future__ import annotations

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
    t0 = time.time()
    b = setup_sandbox(SANDBOX_CONFIG)
    print(f"建沙箱 {time.time() - t0:.1f}s  id={b.id}")

    r = b.execute("/opt/skills-venv/bin/python3 /tmp/_guard_check.py")
    print("守卫自检:", (r.output or "").strip())

    # 真实场景：用 pymysql 连库，看是否快速失败
    t0 = time.time()
    r2 = b.execute(
        "/opt/skills-venv/bin/python3 -c "
        "\"import pymysql;"
        "pymysql.connect(host='192.168.64.132',port=3306,user='x',password='y',connect_timeout=30)\""
    )
    cost = time.time() - t0
    out = (r2.output or "").strip().splitlines()
    tail = out[-1] if out else ""
    print(f"pymysql 直连耗时 {cost:.1f}s → {tail[:160]}")
    print("结论:", "快速失败 [OK]" if cost < 5 else "仍然慢 [FAIL]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
