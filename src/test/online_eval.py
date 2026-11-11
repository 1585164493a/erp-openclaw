"""在线评测：硬规则 + LLM judge，低分/点踩的 trace 自动回流到 bad-cases dataset。

设计原则（对应「记录与判定分离」）：
    - 只读 Langfuse 的 trace / observation，不碰业务进程，不阻塞用户请求
    - **硬规则先跑**：确定性、免费、可解释；明确失败的直接出分并短路
    - **规则判不了的才交给 LLM judge**：主观质量（答得好不好）
    - 判成 bad 的 trace → upsert 进 dataset `bad-cases`，下次 experiment 就能防住

用法：
    python src/test/online_eval.py --dry-run              # 只看会怎么判，不写任何东西
    python src/test/online_eval.py --limit 10             # 真跑，写分数 + 回流
    python src/test/online_eval.py --no-judge             # 只跑硬规则（不花钱）

阈值都在下面的常量里，改完直接跑。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = HERE.parents[1]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env.langfuse", override=True)   # Langfuse 本地实例
load_dotenv(_PROJECT_ROOT / ".env", override=False)           # 大模型 key（不覆盖已有值）

LF_HOST = os.getenv("LANGFUSE_HOST", "").rstrip("/")
LF_AUTH = (os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY"))

BAD_CASE_DATASET = "bad-cases"
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "deepseek-chat")

# ============ 阈值（唯一需要你调的地方）============
SLOW_TRACE_SECONDS = 180.0     # 超过算慢（多 Agent 一轮本来就几十秒，60s 会天天误报）
MAX_TOOL_REPEAT = 3            # 同一工具**连续**被调 >= 3 次，才判绕圈（按总数会误报：读 3 个文件很正常）
MIN_QUALITY = 3                # LLM 质量分 < 3 判为 bad（满分 5）
WRITE_TOOLS = {                # 写工具：调了却没有停下来等人审批 = 违规
    "supplier_create", "order_create", "customer_create",
    "product_create", "order_update", "customer_update",
}
# 中断/审批相关的 observation 名字里通常带这些词
HITL_HINTS = ("hitl", "interrupt", "审批", "中断")

# ============ 答案质量断言（2026-11-11 从一条真实 bad case 提炼）============
# 那次用户问「帮我查一下都有哪些sku」，模型给了统计分析和一条虚构的报告路径。
LIST_INTENT = re.compile(r"(有哪些|列出|清单|明细|多少(个|条|种)|都有哪些)")
RECORD_CODE = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*-\d{2,}\b")   # 形如 HFS-750-01 / SKU-001
REPORT_PATH = re.compile(r"/analysis/report_[A-Za-z0-9_\-]+\.md")
MIN_RECORDS = 3      # 问清单时，答案里至少要出现的记录编号个数


def _score_id(trace_id: str, name: str) -> str:
    """幂等键 = trace_id + 分名，转成合法 UUID。

    两个实测结论（2026-11-11 在你实例上验过）：
    ① id 必须是**合法 UUID/cuid**。我一开始用 "trace:name" 这种带冒号的字符串，
       服务端直接忽略 → 退化成"新增"，分数越堆越多（同 trace 同名堆到 8 条）。
    ② 合法 UUID 重复 POST 是**覆盖**：传同一个 id 两次，库里只有 1 条，value 被更新。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{trace_id}:{name}"))


def lf_get(path: str, params: dict | None = None) -> dict:
    with httpx.Client(timeout=20.0) as c:
        r = c.get(f"{LF_HOST}{path}", params=params, auth=LF_AUTH)
        r.raise_for_status()
        return r.json()


def lf_post(path: str, body: dict) -> tuple[int, dict]:
    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{LF_HOST}{path}", json=body, auth=LF_AUTH)
        try:
            return r.status_code, r.json()
        except Exception:  # noqa: BLE001
            return r.status_code, {}


# ---------------------------------------------------------------- 事实提取
def fetch_observations(trace_id: str) -> list[dict]:
    try:
        return lf_get("/api/public/observations", {"traceId": trace_id, "limit": 100}).get("data") or []
    except Exception:  # noqa: BLE001
        return []


def _msg_text(msg: Any) -> str:
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):  # 多模态/分块 content
            return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
        return str(c or "")
    return str(msg or "")


def _extract_question(raw: Any) -> str:
    """trace.input 可能是 {"question": ...}，也可能是 {"messages": [...]}。"""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        if raw.get("question"):
            return str(raw["question"])
        msgs = raw.get("messages") or []
        for m in msgs:
            if isinstance(m, dict) and (m.get("type") == "human" or m.get("role") == "user"):
                return _msg_text(m)
        if msgs:
            return _msg_text(msgs[0])
    return ""


def _extract_answer(raw: Any) -> str:
    """trace.output 是 LangGraph 的 state（{"messages": [...]}），
    最后一条 assistant 的 content 才是「回答」。取不到就返回空串（判官会跳过）。"""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("answer", "output", "content", "text"):
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                return v
        msgs = raw.get("messages") or []
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("type") in {"ai", "AIMessage"} or (
                isinstance(m, dict) and m.get("role") == "assistant"
            ):
                txt = _msg_text(m).strip()
                if txt:
                    return txt
        for m in reversed(msgs):
            txt = _msg_text(m).strip()
            if txt:
                return txt
    return ""


def extract_facts(trace: dict, observations: list[dict], existing_scores: list[dict] | None = None) -> dict:
    """把一条 trace 变成判定需要的「事实」。只描述现状，不做判断。"""
    tool_names = [o.get("name") for o in observations if (o.get("type") or "").upper() == "TOOL"]
    gen_count = sum(1 for o in observations if (o.get("type") or "").upper() == "GENERATION")
    errors = [o for o in observations if (o.get("level") or "").upper() == "ERROR"]

    names_lower = " ".join((o.get("name") or "").lower() for o in observations)
    has_hitl = any(h in names_lower for h in HITL_HINTS)

    # 唯一性计数：**同一工具 + 相同入参**连续重复才算绕圈。
    # 只看工具名会误报——实测一次任务里 product_query 连调 3 次是查 3 个商品，
    # generate_visualization 连调 2 次是画 2 张图，都正常。
    sigs: list[tuple] = []
    for o in observations:
        if (o.get("type") or "").upper() != "TOOL":
            continue
        try:
            args = json.dumps(o.get("input"), sort_keys=True, ensure_ascii=False)[:400]
        except Exception:  # noqa: BLE001
            args = str(o.get("input"))[:400]
        sigs.append((o.get("name"), args))

    best = 0
    run = 0
    prev_sig = None
    for sig in sigs:
        run = run + 1 if sig == prev_sig else 1
        prev_sig = sig
        best = max(best, run)

    # 写文件实际落地的路径（用于判断答案里报的路径是不是编的）
    written_paths: set[str] = set()
    for o in observations:
        if (o.get("type") or "").upper() != "TOOL":
            continue
        if (o.get("name") or "") not in {"write_file", "edit_file"}:
            continue
        inp = o.get("input")
        if isinstance(inp, dict):
            p = inp.get("file_path") or inp.get("path")
            if isinstance(p, str):
                written_paths.add(p)

    return {
        "trace_id": trace.get("id"),
        "input": trace.get("input"),
        "output": trace.get("output"),
        "question_text": _extract_question(trace.get("input")),
        "answer_text": _extract_answer(trace.get("output")),
        "written_paths": written_paths,
        "latency": trace.get("latency") or 0.0,
        "tools": tool_names,
        "tool_repeat": best,
        "generations": gen_count,
        "errors": [e.get("name") for e in errors],
        "has_hitl": has_hitl,
        "user_feedback": _user_feedback(existing_scores or []),
    }


def _fetch_scores(trace_id: str) -> list[dict]:
    """取这条 trace 上的分数。

    ⚠️ 实测 2026-11-11：`/api/public/scores?traceId=xxx` 的过滤参数**不生效**
    （返回的是全项目最近分数），只能先拉一批再在本地按 traceId 过滤。
    """
    try:
        data = lf_get("/api/public/scores", {"limit": 100}).get("data") or []
    except Exception:  # noqa: BLE001
        return []
    return [s for s in data if isinstance(s, dict) and s.get("traceId") == trace_id]


def _user_feedback(scores: list[dict]) -> float | None:
    """点踩的读法：trace 上名为 user_feedback（或 thumbs）的分；<=0 视为踩。"""
    for s in scores:
        if not isinstance(s, dict):
            continue
        if (s.get("name") or "").lower() in {"user_feedback", "thumbs", "feedback"}:
            for key in ("value", "stringValue"):
                v = s.get(key)
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
    return None


# ---------------------------------------------------------------- 硬规则层
def run_rules(f: dict) -> tuple[list[dict], list[str]]:
    """返回 (scores, 失败原因)。scores 里每条都是可以直接 POST 的结构。"""
    scores: list[dict] = []
    reasons: list[str] = []

    def add(name: str, value: Any, dtype: str, comment: str = "") -> None:
        # 幂等键：同一条 trace + 同一个分名 -> 同一个 UUID，重复写会覆盖而不是新增
        scores.append({"id": _score_id(f["trace_id"], name), "name": name, "value": value,
                       "dataType": dtype, "comment": comment, "traceId": f["trace_id"]})

    # R1 报错：出现 ERROR 级 observation。硬约束，出现即告警
    if f["errors"]:
        add("error_present", 1, "BOOLEAN", f"报错节点：{', '.join(f['errors'][:3])}")
        reasons.append("error")
    else:
        add("error_present", 0, "BOOLEAN")

    # R2 绕圈：同一工具被反复调用
    if f["tool_repeat"] >= MAX_TOOL_REPEAT:
        add("loop_suspected", 1, "BOOLEAN", f"单工具重复 {f['tool_repeat']} 次")
        reasons.append("loop")
    else:
        add("loop_suspected", 0, "BOOLEAN")

    # R3 写了却没停：调用了写工具，但整条 trace 里没有任何审批/中断痕迹
    wrote = sorted(set(f["tools"]) & WRITE_TOOLS)
    if wrote and not f["has_hitl"]:
        add("write_without_hitl", 1, "BOOLEAN", f"调了写工具但没有中断：{', '.join(wrote)}")
        reasons.append("write_without_hitl")
    else:
        add("write_without_hitl", 0, "BOOLEAN")

    # R4 慢
    if f["latency"] > SLOW_TRACE_SECONDS:
        add("slow_trace", 1, "BOOLEAN", f"{f['latency']:.0f}s > {SLOW_TRACE_SECONDS:.0f}s")
        reasons.append("slow")
    else:
        add("slow_trace", 0, "BOOLEAN")

    # R5 空输出（看提取出来的回答文本，不是看原始 state）
    if not f["answer_text"]:
        add("empty_output", 1, "BOOLEAN", "没有提取到回答文本")
        reasons.append("empty_output")
    else:
        add("empty_output", 0, "BOOLEAN")

    # R6 答非所问：用户要清单，答案里却没有具体记录
    answer = f["answer_text"]
    records = set(RECORD_CODE.findall(answer))
    if LIST_INTENT.search(f["question_text"]) and len(records) < MIN_RECORDS:
        add("answer_has_records", 1, "BOOLEAN",
            f"问清单但只有 {len(records)} 个记录编号（要求 ≥{MIN_RECORDS}）")
        reasons.append("no_records")
    else:
        add("answer_has_records", 0, "BOOLEAN")

    # R7 幻觉路径：答案里报的报告路径，trace 里并没有真的写过
    claimed = set(REPORT_PATH.findall(answer))
    fake = {p for p in claimed if p not in (f.get("written_paths") or set())}
    if fake:
        add("hallucinated_path", 1, "BOOLEAN", f"未实际写入：{', '.join(sorted(fake))[:80]}")
        reasons.append("hallucinated_path")
    else:
        add("hallucinated_path", 0, "BOOLEAN")

    return scores, reasons


# ---------------------------------------------------------------- LLM 判官层
JUDGE_PROMPT = """你是严格的质检员。给你一条企业助手对话的「问题」和「回答」，请按下面标准打分。

评分标准（1-5）：
5 = 准确完整，直接解决问题，没有编造
4 = 基本正确，只有无关紧要的瑕疵
3 = 部分正确，但缺关键信息或表述含糊
2 = 明显有问题：答非所问、或漏掉关键步骤
1 = 错误/幻觉/答非所问

只输出 JSON：{"score": <1-5 的整数>, "reason": "<不超过 40 字>"}

问题：%QUESTION%
回答：%ANSWER%
"""


def judge_quality(question: str, answer: str) -> tuple[float | None, str]:
    key = os.getenv("DEEPSEEK_API_KEY")
    base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if not key:
        return None, "未配置 DEEPSEEK_API_KEY，跳过 judge"
    payload = {
        "model": JUDGE_MODEL,
        "temperature": 0,                     # 判官必须可复现
        "messages": [{"role": "user", "content": JUDGE_PROMPT.replace("%QUESTION%", question).replace("%ANSWER%", answer)}],
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}"}, json=payload)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        return float(data.get("score")), str(data.get("reason", ""))[:200]
    except Exception as exc:  # noqa: BLE001
        return None, f"judge 失败：{type(exc).__name__}: {exc}"[:200]


# ---------------------------------------------------------------- 回流
def ensure_bad_case_dataset() -> None:
    code, _ = lf_post("/api/public/datasets", {"name": BAD_CASE_DATASET,
                                               "description": "低分/点踩的线上 trace 回流"})
    if code not in (200, 201, 409):
        print(f"  [warn] 建数据集返回 {code}")


def _existing_quality(scores: list[dict], name: str) -> float | None:
    """读 Langfuse judge 已经写在这条 trace 上的质量分（plan B：judge 在平台跑，脚本只读）。"""
    for s in scores:
        if isinstance(s, dict) and (s.get("name") or "") == name:
            for key in ("value", "stringValue"):
                try:
                    return float(s.get(key))
                except (TypeError, ValueError):
                    continue
    return None


def existing_bad_trace_ids() -> set[str]:
    """bad-cases 里已有的 traceId —— 回流也要幂等，别每次都新增一条。"""
    try:
        data = lf_get("/api/public/dataset-items",
                      {"datasetName": BAD_CASE_DATASET, "limit": 100}).get("data") or []
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for it in data:
        tid = (it.get("metadata") or {}).get("traceId")
        if tid:
            out.add(str(tid))
    return out


def push_bad_case(f: dict, reasons: list[str], quality: float | None, judge_reason: str) -> int:
    body = {
        "datasetName": BAD_CASE_DATASET,
        "input": f["question_text"] or (f["input"] if isinstance(f["input"], (dict, str)) else str(f["input"])),
        "metadata": {
            "traceId": f["trace_id"],
            "reasons": reasons,
            "quality": quality,
            "judge_reason": judge_reason,
            "tools": f["tools"][:10],
            "latency": f["latency"],
            "source": "online_eval",
        },
        "sourceTraceId": f["trace_id"],
    }
    code, _ = lf_post("/api/public/dataset-items", body)
    return code


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20, help="看最近多少条 trace")
    ap.add_argument("--since-minutes", type=int, default=24 * 60, help="只看多久以内的")
    ap.add_argument("--judge", action="store_true",
                    help="自己调大模型打分（默认不调：优先读 Langfuse LLM-as-a-Judge 写的分）")
    ap.add_argument("--no-judge", action="store_true", help="（兼容旧用法，等价于不加 --judge）")
    ap.add_argument("--quality-score-name", default="quality",
                    help="Langfuse judge 写回来的分数名，脚本直接读它用于 bad 判定")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写分数不回流")
    ap.add_argument("--force", action="store_true", help="已经评过的 trace 也重新评（默认跳过，避免分数重复堆积）")
    ap.add_argument("--environment", default="default",
                    help="只评这个 environment 的 trace（实验/SDK 自测产生的 trace 通常不是 default）")
    ap.add_argument("--skip-names", default="connectivity-check",
                    help="按 trace 名排除测试流量，逗号分隔")
    args = ap.parse_args()

    if not LF_HOST or not LF_AUTH[0]:
        print("没配 Langfuse 密钥，检查 .env.langfuse")
        return 2

    traces = lf_get("/api/public/traces", {"limit": args.limit}).get("data") or []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.since_minutes)
    traces = [t for t in traces if _ts_after(t.get("timestamp"), cutoff)]
    if args.environment:
        traces = [t for t in traces if (t.get("environment") or "default") == args.environment]
    # 排除测试流量：连通性自测、SDK 冒烟测试的 trace 不该进 bad-cases
    skip_names = {n.strip().lower() for n in (args.skip_names or "").split(",") if n.strip()}
    if skip_names:
        traces = [t for t in traces if (t.get("name") or "").lower() not in skip_names]

    print(f"待评 trace：{len(traces)} 条（limit={args.limit}, since={args.since_minutes}min）\n")
    if not traces:
        return 0

    if not args.dry_run:
        ensure_bad_case_dataset()
    bad_seen = existing_bad_trace_ids()

    pushed = 0
    skipped = 0
    our_scores = {"error_present", "loop_suspected", "write_without_hitl",
                  "slow_trace", "empty_output", "quality_score"}
    for t in reversed(traces):                       # 从旧到新
        existing = _fetch_scores(t.get("id"))
        seen = {s.get("name") for s in existing if isinstance(s, dict)}
        if not args.force and (seen & our_scores):
            skipped += 1
            print(f"skip {str(t.get('id'))[:8]} | 已评过（--force 可重评）")
            continue
        obs = fetch_observations(t.get("id"))
        f = extract_facts(t, obs, existing)

        # 质量分：优先读 Langfuse judge 写的（plan B：judge 搬进 Langfuse，脚本只读结果）
        quality, jreason = _existing_quality(existing, args.quality_score_name), ""
        if quality is not None:
            jreason = f"来自 Langfuse judge（{args.quality_score_name}）"
        elif args.judge and f["question_text"] and f["answer_text"]:
            quality, jreason = judge_quality(f["question_text"][:1500], f["answer_text"][:2000])

        scores, reasons = run_rules(f)

        if quality is not None:
            # 只写 trace 级的 quality_score：名字和平台 judge 的 quality 区分开。
            # ⚠️ 别再往 trace 级写同名的 quality —— 会和 judge 的 observation 级同名冲突，
            #    表格列取不到值（实测 2026-11-11，踩过一次）。
            scores.append({"id": _score_id(f["trace_id"], "quality_score"), "name": "quality_score",
                           "value": quality, "dataType": "NUMERIC",
                           "comment": jreason, "traceId": f["trace_id"]})
            if quality < MIN_QUALITY:
                reasons.append(f"quality<{MIN_QUALITY}")

        # 点踩也算 bad
        if f["user_feedback"] is not None and f["user_feedback"] <= 0:
            reasons.append("thumbs_down")

        tag = "BAD " if reasons else "ok  "
        print(f"{tag} {str(f['trace_id'])[:8]} | {f['latency']:6.1f}s | tools={len(f['tools']):2d} "
              f"| q={quality if quality is not None else '-'} | {','.join(reasons) or '-'}")

        if args.dry_run:
            continue

        for s in scores:
            lf_post("/api/public/scores", s)

        if reasons:
            if f["trace_id"] in bad_seen:
                print("       → 已在 bad-cases，跳过")
            else:
                code = push_bad_case(f, reasons, quality, jreason)
                bad_seen.add(f["trace_id"])
                pushed += 1
                print(f"       → bad-cases dataset: {code}")

    if args.dry_run:
        print("\n--dry-run：没有写任何分数，也没有回流。")
    else:
        print(f"\n完成：回流 {pushed} 条到 {BAD_CASE_DATASET}；跳过已评 {skipped} 条。")
    return 0


def _ts_after(ts: str | None, cutoff: datetime) -> bool:
    if not ts:
        return True
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff
    except ValueError:
        return True


if __name__ == "__main__":
    raise SystemExit(main())
