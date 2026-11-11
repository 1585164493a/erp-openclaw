"""把 eval_routing.py 里的 GOLDEN 15 条灌进 Langfuse 的 dataset。

为什么要有这一步：
    dataset 是「题库」，experiment 只认 dataset，不认代码里的 GOLDEN。
    所以在跑 baseline-v2 之前，得先把 15 条题搬进去。

幂等：已经存在的 case_no 会跳过（不会重复灌）。

用法：
    python src/test/seed_dataset.py --dry-run     # 只看要灌什么
    python src/test/seed_dataset.py               # 真灌
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = HERE.parents[1]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env.langfuse", override=True)

LF_HOST = os.getenv("LANGFUSE_HOST", "").rstrip("/")
LF_AUTH = (os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY"))
DATASET = "erp-golden"

_spec = importlib.util.spec_from_file_location("eval_routing", HERE / "eval_routing.py")
_ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ev)
GOLDEN = _ev.GOLDEN


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=DATASET)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not LF_HOST or not LF_AUTH[0]:
        print("没配 Langfuse 密钥，检查 .env.langfuse")
        return 2

    with httpx.Client(timeout=30.0) as c:
        # 1) 现有条目 → 按 case_no 去重
        existing = c.get(f"{LF_HOST}/api/public/dataset-items",
                         params={"datasetName": args.dataset, "limit": 100}, auth=LF_AUTH)
        items = (existing.json().get("data") or []) if existing.status_code == 200 else []
        have = {(i.get("metadata") or {}).get("case_no") for i in items}
        print(f"数据集 {args.dataset} 现有 {len(items)} 条，已含 case_no: {sorted(x for x in have if x)}")

        todo = [g for g in GOLDEN if g[0] not in have]
        print(f"待灌 {len(todo)} 条\n")

        for num, question, expected, should_hitl, note in todo:
            body = {
                "datasetName": args.dataset,
                "input": {"question": question},
                "expectedOutput": {"answer": expected},
                "metadata": {
                    "case_no": num,
                    "expected_route": expected,
                    "should_hitl": bool(should_hitl),
                    "max_steps": 8,
                    "type": "write" if should_hitl else "read",
                    "note": note,
                },
            }
            if args.dry_run:
                print(f"  [dry] #{num:>2} {question[:30]}… → {expected}  hitl={should_hitl}")
                continue
            r = c.post(f"{LF_HOST}/api/public/dataset-items", json=body, auth=LF_AUTH)
            print(f"  {'ok ' if r.status_code in (200, 201) else 'ERR'} #{num:>2} "
                  f"{question[:30]}… → {expected} ({r.status_code})")

    if args.dry_run:
        print("\n--dry-run：没有写入。")
    else:
        print("\n完成。去界面确认：Datasets → erp-golden → Items 应该是 15 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
