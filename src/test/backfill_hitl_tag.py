"""给已经存在的 HITL trace 补打 hitl 标签（一次性回填）。

新跑出来的 trace 由 patch_trace_io 自动打标签；这个脚本只处理历史数据，
让 Langfuse 的 Rule 过滤「Tags 不含 hitl」立刻生效。
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import httpx

HOST = "http://192.168.64.132:3000"
AUTH = ("pk-lf-erp-openclaw-local", "sk-lf-erp-openclaw-local")
HINTS = ("humanintheloop", "interrupt", "审批", "中断")


def main() -> int:
    with httpx.Client(timeout=20.0) as c:
        traces = (c.get(f"{HOST}/api/public/traces", params={"limit": 30}, auth=AUTH).json() or {}).get("data") or []
        tagged = 0
        for t in traces:
            tags = list(t.get("tags") or [])
            if "hitl" in tags:
                continue
            obs = (c.get(f"{HOST}/api/public/observations",
                         params={"traceId": t.get("id"), "limit": 100}, auth=AUTH).json() or {}).get("data") or []
            names = " ".join((o.get("name") or "").lower() for o in obs)
            if not any(h in names for h in HINTS):
                continue
            tags.append("hitl")
            event = {
                "id": str(uuid.uuid4()),
                "type": "trace-create",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "body": {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "sessionId": t.get("sessionId"),
                    "userId": t.get("userId"),
                    "tags": tags,
                    "metadata": t.get("metadata") or {},
                },
            }
            r = c.post(f"{HOST}/api/public/ingestion", json={"batch": [event]}, auth=AUTH)
            if r.status_code in (200, 207):
                tagged += 1
                print(f"tagged {t.get('id')[:8]} {t.get('name')[:30]}")
        print(f"完成：回填 {tagged} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
