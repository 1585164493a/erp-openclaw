"""自动打分：跑 golden set → 取实际值 → 与期望比对 → 写回 Langfuse。

和 eval_routing.py 的分工：
    eval_routing.py  只负责「跑」和「打印」，人看。
    本脚本负责「判」和「上报」——把人的判断固化成代码，分数自动落到 Langfuse。

断言规则（全部来自 GOLDEN 里的期望值，不需要人再点）：
    route_correct  BOOLEAN   实际委派的第一个子 Agent == 期望
    hitl_status    CATEGORICAL  correct / missed（该停没停）/ spurious（不该停却停了）
    tools_used     NUMERIC    本轮调用过的工具个数

用法（需 8080 ERP API + 8099 MCP + 8091 后端 都在跑）：
    python test/eval_scores.py                # 全量 15 条
    python test/eval_scores.py --only 1 12    # 只跑指定编号
    python test/eval_scores.py --dry-run      # 只打印判定结果，不写回 Langfuse
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = HERE.parents[1]

# 复用现成的跑批/解析逻辑，避免两套 SSE 解析漂移
_spec = importlib.util.spec_from_file_location("eval_routing", HERE / "eval_routing.py")
if _spec is None or _spec.loader is None:  # pragma: no cover
    raise RuntimeError("找不到 eval_routing.py")
_ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ev)
GOLDEN = _ev.GOLDEN
run_case = _ev.run_case

# Langfuse 配置只认 .env.langfuse（主 .env 里那份是 Cloud 的旧配置）
try:
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env.langfuse", override=True)
except Exception:  # noqa: BLE001
    pass

LF_HOST = os.getenv("LANGFUSE_HOST", "").rstrip("/")
LF_AUTH = (os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY"))

HITL_VALUE = {"correct": 1, "missed": 0, "spurious": 0.5}


def find_trace_id(session_id: str, wait: float = 25.0) -> str | None:
    """trace 是异步入库的，按 sessionId（= thread_id）轮询拿 trace id。"""
    if not LF_HOST or not LF_AUTH[0]:
        return None
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(
                    f"{LF_HOST}/api/public/traces",
                    params={"sessionId": session_id, "limit": 1},
                    auth=LF_AUTH,
                )
                data = r.json().get("data") or []
                if data:
                    return data[0]["id"]
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return None


def post_score(trace_id: str, name: str, value, data_type: str, comment: str = "") -> int:
    payload = {
        "traceId": trace_id,
        "name": name,
        "value": value,
        "dataType": data_type,
        "comment": comment,
    }
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.post(f"{LF_HOST}/api/public/scores", json=payload, auth=LF_AUTH)
            return r.status_code
    except Exception:  # noqa: BLE001
        return 0


def judge(case: tuple, result: dict) -> list[dict]:
    """把「期望 vs 实际」变成三条分数。"""
    num, question, expected_route, expected_hitl, note = case[:5]
    delegated: list[str] = result.get("delegated") or []
    interrupt = result.get("interrupt_type")
    actual_hitl = interrupt is not None

    actual_route = delegated[0] if delegated else "(未委派)"
    route_ok = 1 if delegated and delegated[0] == expected_route else 0

    if expected_hitl and actual_hitl:
        hitl = "correct"
    elif expected_hitl and not actual_hitl:
        hitl = "missed"
    elif (not expected_hitl) and actual_hitl:
        hitl = "spurious"
    else:
        hitl = "correct"

    return [
        {
            "name": "route_correct",
            "value": route_ok,
            "dataType": "BOOLEAN",
            "comment": f"期望 {expected_route}｜实际 {actual_route}",
        },
        {
            "name": "hitl_status",
            "value": HITL_VALUE[hitl],
            "dataType": "CATEGORICAL",
            "comment": f"{hitl}｜期望停={expected_hitl} 实际停={actual_hitl}（interrupt={interrupt}）",
        },
        {
            "name": "tools_used",
            "value": len(result.get("tools") or []),
            "dataType": "NUMERIC",
            "comment": f"工具：{', '.join(result.get('tools') or []) or '无'}",
        },
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8091")
    ap.add_argument("--only", nargs="*", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cases = [c for c in GOLDEN if args.only is None or c[0] in args.only]
    print(f"用例 {len(cases)} 条｜Langfuse {LF_HOST or '(未配置)'}｜dry_run={args.dry_run}\n")

    rows = []
    for case in cases:
        num, question, expected_route, expected_hitl, _note = case[:5]
        print(f"--- #{num} {question[:28]}…", flush=True)
        result = run_case(args.base, num, question, args.timeout)

        if result.get("error"):
            print(f"    跳过：{result['error']}")
            rows.append((num, "ERR", "-", "-", result["error"]))
            continue

        scores = judge(case, result)
        trace_id = None if args.dry_run else find_trace_id(result["thread_id"])

        line = []
        for s in scores:
            code = ""
            if trace_id:
                code = str(post_score(trace_id, s["name"], s["value"], s["dataType"], s["comment"]))
            elif not args.dry_run:
                code = "no-trace"
            line.append(f"{s['name']}={s['value']}{('/' + code) if code else ''}")
        print("    " + "  ".join(line))
        rows.append((num, str(scores[0]["value"]), str(scores[1]["value"]),
                     str(scores[2]["value"]), trace_id or "-"))

    print("\n===== 汇总 =====")
    print(f"{'#':>3} {'route':>6} {'hitl':>8} {'tools':>6}  trace")
    for r in rows:
        print(f"{r[0]:>3} {r[1]:>6} {r[2]:>8} {r[3]:>6}  {r[4]}")

    ok = sum(1 for r in rows if r[1] == "1")
    total = sum(1 for r in rows if r[1] in {"0", "1"})
    if total:
        print(f"\n路由正确率：{ok}/{total} = {ok / total:.1%}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
