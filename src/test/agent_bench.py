"""Agent 回归对比台：同一批题，跑两个版本，比差异。

它测的是**你的 agent**（和真实用户同一入口 /api/chat/stream），不是裸 LLM。
用途：加 wiki 之前/之后、调 text2sql 之前/之后、任何改动前后。

三个命令：

    # ① 改动前跑一次，冻结成 baseline
    python src/test/agent_bench.py run --tag before-wiki

    # ② 改动后再跑
    python src/test/agent_bench.py run --tag after-wiki

    # ③ 对比（修复/回归/无变化）
    python src/test/agent_bench.py compare before-wiki after-wiki

    # ④ 把某次 run 推到 Langfuse dataset，进 Experiments 视图
    python src/test/agent_bench.py push --tag after-wiki

设计要点：
- 每题新开 thread_id（否则上下文污染，"after"会用着"before"的历史）
- 每次 run 记录 git commit + 时间，否则事后分不清版本
- baseline 是冻结快照，不重跑；改动后只跑新的那一侧
- 判分：有 ground_truth_sql 的用**数据对拍**，其余用关键词/人工
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))

# 跑批结果默认落在工作区根目录下的 _bench_runs（项目子目录可能被沙箱拒绝写入）。
# 需要换位置时设 BENCH_RUNS_DIR。
RUNS_DIR = Path(os.getenv("BENCH_RUNS_DIR") or (ROOT.parent / "_bench_runs"))
CASES_PATH = ROOT / "knowledge" / "trap_questions.yaml"
BASE_URL = os.getenv("ERP_API", "http://127.0.0.1:8091")


# ------------------------------------------------------------------ 基础
def git_commit() -> str:
    """当前代码版本；有未提交改动时带 -dirty，避免把两种代码当成同一版。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
        )
        sha = (out.stdout or "").strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not sha:
            return "unknown"
        return f"{sha}-dirty" if dirty else sha
    except Exception:  # noqa: BLE001
        return "unknown"


def load_cases(only: list[str] | None = None, path: Path | None = None) -> list[dict]:
    data = yaml.safe_load((path or CASES_PATH).read_text(encoding="utf-8"))
    cases = data["questions"]
    if only:
        wanted = {c.upper() for token in only for c in token.replace(",", " ").split()}
        cases = [c for c in cases if c["id"].upper() in wanted]
    return cases


def run_path(tag: str) -> Path:
    return RUNS_DIR / f"{tag}.json"


# ------------------------------------------------------------------ 调 agent
def ask_agent(question: str, timeout: float = 600.0) -> dict:
    """打一次 agent，收集答案 / 工具序列 / 耗时 / 用量。

    ⚠️ SSE 不能设 read timeout：agent 可能几十秒不吐 token（在跑工具），
    httpx 默认超时会把长任务判死（实测 T06 采购单那条就抛 ReadTimeout）。
    改成：connect/write 有超时，read 不限；总时长由外层 deadline 控制。
    """
    thread_id = f"bench-{uuid.uuid4().hex[:12]}"
    payload = {"message": question, "thread_id": thread_id, "user_id": "bench"}
    started = time.time()
    answer_parts: list[str] = []
    tools: list[str] = []
    usage: dict = {}
    interrupted = False

    try:
        with httpx.Client(
            # read 设成 timeout+30：卡死时能被 ReadTimeout 打断。
            # 之前是 read=None（永不超时），结果 SSE 流卡住时整轮挂 16 分钟，
            # 外层 deadline 也轮不到执行（它只在"收到下一行"时才检查）。
            timeout=httpx.Timeout(connect=15.0, read=timeout + 30.0, write=30.0, pool=15.0)
        ) as client:
            with client.stream("POST", f"{BASE_URL}/api/chat/stream", json=payload) as resp:
                if resp.status_code != 200:
                    return {"ok": False, "error": f"HTTP {resp.status_code}", "thread_id": thread_id}
                for line in resp.iter_lines():
                    if time.time() - started > timeout:
                        return {"ok": False, "error": f"超时(>{timeout:.0f}s)",
                                "thread_id": thread_id, "partial_answer": "".join(answer_parts)}
                    if not line:
                        continue
                    text = line.decode("utf-8") if isinstance(line, bytes) else line
                    if text.startswith("data:"):
                        text = text[5:].strip()
                    if not text or text == "[DONE]":
                        continue
                    try:
                        evt = json.loads(text)
                    except Exception:  # noqa: BLE001
                        continue
                    etype = evt.get("type")
                    if etype == "token":
                        answer_parts.append(evt.get("content") or "")
                    elif etype == "tool_start":
                        # 事件字段名是 tool_name（不是 name），之前漏采集导致 tools 恒为空
                        name = evt.get("tool_name") or evt.get("name")
                        if name:
                            tools.append(name)
                    elif etype == "usage":
                        usage = evt
                    elif etype == "interrupt":
                        interrupted = True
                    elif etype == "error":
                        return {"ok": False, "error": str(evt)[:300], "thread_id": thread_id}
    except Exception as e:  # noqa: BLE001 —— 一条题失败不能杀掉整批
        return {"ok": False, "error": f"{e.__class__.__name__}: {str(e)[:200]}",
                "thread_id": thread_id, "partial_answer": "".join(answer_parts)}

    return {
        "ok": True,
        "answer": "".join(answer_parts).strip(),
        "tools": tools,
        "latency": round(time.time() - started, 2),
        "usage": usage,
        "interrupted": interrupted,
        "thread_id": thread_id,
    }


# ------------------------------------------------------------------ 判分
def _sql_tools() -> dict:
    from agent.tools.sql_tools import create_sql_tools

    return {t.name: t for t in create_sql_tools()}


def _rows_from_table(text: str) -> list[dict]:
    """把 sql_query 输出的文本表格解析回行（用于对拍）。

    ⚠️ 单列结果里没有 "|"，所以不能靠 "|" 判断是不是表格——
    改成"找到那条 40 个横线的分隔行"，它的上一行是表头。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    sep = next((i for i, ln in enumerate(lines) if set(ln.strip()) == {"-"}), None)
    if sep is None or sep == 0:
        return []
    header = [c.strip() for c in lines[sep - 1].split("|")]
    rows = []
    for ln in lines[sep + 1:]:
        if ln.startswith("("):          # "(1 行，0.02s)" 这类尾巴
            break
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    return rows


def _norm_num(s: str) -> str:
    """去掉千分位/空格/全角符号，便于数值比对（90,607 与 90607 视为相同）。"""
    return re.sub(r"[,\s，\u00a0]", "", s or "")


def judge(case: dict, answer: str, tools: dict) -> tuple[str, str]:
    """返回 (结果, 理由)。结果为 pass / fail / manual。"""
    if not answer:
        return "fail", "答案为空"

    method = case.get("judge")

    if method == "contain":
        keys = case.get("contain_any") or []
        hit = [k for k in keys if k and k in answer]
        return ("pass" if hit else "fail",
                f"命中关键词 {hit[:2]}" if hit else f"未命中任何关键词：{keys[:3]}")

    # 「不可回答」类：明确拒答 **或** 先澄清口径，都算正确行为
    if method == "clarify":
        keys = case.get("contain_any") or [
            "没有", "无", "不存在", "无法", "缺少", "澄清", "口径", "哪一种", "确认", "希望要",
        ]
        hit = [k for k in keys if k and k in answer]
        return ("pass" if hit else "fail",
                f"命中拒答/澄清关键词 {hit[:2]}" if hit else "既没拒答也没澄清")

    if method == "manual":
        return "manual", "需人工判定"

    if method == "exec":
        gt = case.get("ground_truth_sql")
        if not gt:
            return "manual", "没有标准 SQL"
        try:
            table = tools["sql_query"](gt)
            rows = _rows_from_table(table)
        except Exception as e:  # noqa: BLE001
            return "manual", f"标准 SQL 执行失败：{e}"
        if not rows:
            return "manual", "标准 SQL 无结果"
        # 单值结果：数值归一化后比对；纯数字再给一点容差（4.66 与 4.7 不算错）
        if len(rows) == 1 and len(rows[0]) == 1:
            val = str(next(iter(rows[0].values())))
            ok = _norm_num(val) in _norm_num(answer) or val in answer
            if not ok:
                try:
                    expected = float(_norm_num(val))
                    found = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", _norm_num(answer))]
                    tol = max(0.05, abs(expected) * 0.02)     # 绝对 0.05 或 相对 2%
                    ok = any(abs(x - expected) <= tol for x in found)
                    if ok:
                        return "pass", f"期望值 {val}（容差内命中）"
                except (TypeError, ValueError):
                    pass
            return ("pass" if ok else "fail", f"期望值 {val}")
        # 多行结果：逐个值检查，命中 ≥80% 算过
        values = [str(v) for r in rows for v in r.values() if v not in (None, "")]
        norm_answer = _norm_num(answer)
        hit = sum(1 for v in values if v in answer or _norm_num(v) in norm_answer)
        ratio = hit / max(1, len(values))
        return ("pass" if ratio >= 0.8 else "fail",
                f"命中 {hit}/{len(values)}（{ratio:.0%}）")

    return "manual", f"未知判分方式 {method}"


# ------------------------------------------------------------------ run
def cmd_run(args) -> int:
    cases = load_cases(args.only, Path(args.cases) if args.cases else None)
    tools = _sql_tools()
    print(f"题数={len(cases)}  endpoint={BASE_URL}  tag={args.tag}")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    done: set[str] = set()

    # --resume：已有同名 run 时跳过已完成的题（长跑中途断了不用从头来）
    path = run_path(args.tag)
    if args.resume and path.exists():
        prev = json.loads(path.read_text(encoding="utf-8"))
        results = prev.get("results", [])
        done = {r["id"] for r in results}
        print(f"续跑：已完成 {len(done)} 题，跳过它们")

    def flush() -> None:
        payload = {
            "tag": args.tag,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "git_commit": git_commit(),
            "endpoint": BASE_URL,
            "cases_path": str(args.cases or CASES_PATH),
            "results": results,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    for i, case in enumerate(cases, 1):
        if case["id"] in done:
            continue
        print(f"[{i}/{len(cases)}] {case['id']} {case['question'][:30]} …", end=" ", flush=True)
        got = ask_agent(case["question"], timeout=args.timeout)
        if not got.get("ok"):
            print(f"ERROR {got.get('error')}")
            results.append({**case, "result": "fail", "reason": got.get("error", ""),
                            "answer": got.get("partial_answer", ""), "tools": [],
                            "latency": None, "thread_id": got.get("thread_id")})
            flush()
            continue
        try:
            verdict, reason = judge(case, got["answer"], tools)
        except Exception as e:  # noqa: BLE001 —— 判分器自己出错不能杀掉整批
            verdict, reason = "manual", f"判分异常：{e}"
        mark = {"pass": "OK", "fail": "FAIL", "manual": "MANUAL"}[verdict]
        print(f"{mark} ({got['latency']}s, {len(got['tools'])} 工具) {reason}")
        results.append({
            **case,
            "result": verdict,
            "reason": reason,
            "answer": got["answer"][:4000],
            "tools": got["tools"],
            "latency": got["latency"],
            "usage": got.get("usage") or {},
            "thread_id": got.get("thread_id"),
        })
        flush()          # 每题落盘一次：中途断了也不丢进度

    flush()
    passed = sum(1 for r in results if r["result"] == "pass")
    failed = sum(1 for r in results if r["result"] == "fail")
    manual = sum(1 for r in results if r["result"] == "manual")
    lat = [r["latency"] for r in results if r.get("latency")]
    summary = f"结果：pass={passed} fail={failed} manual={manual}"
    if lat:
        summary += f"  平均耗时={sum(lat) / len(lat):.1f}s"
    print("\n" + summary)
    print(f"已保存：{path}")
    return 0


# ------------------------------------------------------------------ show
def cmd_show(args) -> int:
    """看某一题的完整记录（答案、工具序列、判分理由），排查用。"""
    path = run_path(args.tag)
    if not path.exists():
        print(f"找不到 {path}")
        return 1
    run = json.loads(path.read_text(encoding="utf-8"))
    ids = [i.upper() for i in args.id.replace(",", " ").split()]
    hit = [r for r in run["results"] if r["id"].upper() in ids]
    if not hit:
        print(f"{args.tag} 里没有这些题：{args.id}")
        return 1
    for r in hit:
        print("=" * 72)
        print(f"{r['id']}  [{r.get('category','')}]  {r.get('question','')}")
        print(f"结果: {r.get('result')}  耗时: {r.get('latency')}s  工具: {len(r.get('tools') or [])}")
        print(f"理由: {r.get('reason')}")
        print(f"thread: {r.get('thread_id')}   （Langfuse 里按这个 sessionId 能搜到 trace）")
        print("- 工具序列 -")
        print("  " + " → ".join(r.get("tools") or []) if r.get("tools") else "  （无）")
        print("- 答案 -")
        print(r.get("answer") or "（空）")
        if r.get("ground_truth_sql"):
            print("- 标准 SQL -")
            print(r["ground_truth_sql"])
    return 0


# ------------------------------------------------------------------ report
def cmd_report(args) -> int:
    """把一次 run 渲染成 Markdown 表格（同时写到 run 同目录的 .md）。"""
    path = run_path(args.tag)
    if not path.exists():
        print(f"找不到 {path}")
        return 1
    run = json.loads(path.read_text(encoding="utf-8"))
    rs = run["results"]

    agg: dict[str, int] = {}
    for r in rs:
        agg[r.get("result")] = agg.get(r.get("result"), 0) + 1
    lat = [r["latency"] for r in rs if r.get("latency")]
    avg = sum(lat) / len(lat) if lat else 0

    out: list[str] = []
    out.append(f"# agent bench · {run['tag']}")
    out.append("")
    out.append(f"- commit: `{run['git_commit']}`")
    out.append(f"- 时间: {run['created_at']}")
    out.append(f"- 题数: {len(rs)}　结果: "
               + " / ".join(f"{k} {v}" for k, v in sorted(agg.items()))
               + f"　平均耗时: {avg:.1f}s")
    out.append("")
    out.append("| 题号 | 陷阱类型 | 问题 | 结果 | 耗时 | 工具数 | 判定依据 |")
    out.append("|---|---|---|---|---|---|---|")
    for r in rs:
        q = (r.get("question") or "").replace("|", "\\|")[:24]
        reason = (r.get("reason") or "").replace("|", "\\|")[:34]
        lt = f"{r['latency']:.0f}s" if r.get("latency") else "—"
        out.append(
            f"| {r['id']} | {r.get('category','')} | {q} | {r['result']} | "
            f"{lt} | {len(r.get('tools') or [])} | {reason} |"
        )

    fails = [r for r in rs if r.get("result") == "fail"]
    if fails:
        out.append("")
        out.append("## 失败详情")
        for r in fails:
            out.append("")
            out.append(f"### {r['id']} {r.get('question','')}")
            out.append(f"- 判定：{r.get('reason','')}")
            out.append(f"- 工具：{', '.join((r.get('tools') or [])[:12]) or '无'}")
            ans = (r.get("answer") or "").strip()
            out.append(f"- 答案片段：{ans[:300] if ans else '（空）'}")

    text = "\n".join(out)
    print(text)
    # 默认不落文件：某些环境下临时目录不可写，要文件就显式 --out
    if args.out:
        out_path = Path(args.out)
        try:
            out_path.write_text(text, encoding="utf-8")
            print(f"\n已保存：{out_path}")
        except Exception as e:  # noqa: BLE001
            print(f"\n写文件失败（表格已打印在上面）：{e}")
    return 0


# ------------------------------------------------------------------ compare
def cmd_compare(args) -> int:
    a_path, b_path = run_path(args.left), run_path(args.right)
    for p in (a_path, b_path):
        if not p.exists():
            print(f"找不到 {p}，先跑 run --tag <tag>")
            return 1
    left = json.loads(a_path.read_text(encoding="utf-8"))
    right = json.loads(b_path.read_text(encoding="utf-8"))

    lmap = {r["id"]: r for r in left["results"]}
    rmap = {r["id"]: r for r in right["results"]}

    rows, fixed, broke = [], 0, 0
    # 用两次 run 里出现过的题号，而不是重新读 YAML —— 题集可能已经换成新一批
    ids = [r["id"] for r in left["results"]] + [r["id"] for r in right["results"]]
    for cid in dict.fromkeys(ids):
        l, r = lmap.get(cid), rmap.get(cid)
        if not l or not r:
            continue
        lv, rv = l.get("result"), r.get("result")
        if lv == "fail" and rv == "pass":
            change, fixed = "修复", fixed + 1
        elif lv == "pass" and rv == "fail":
            change, broke = "**回归**", broke + 1
        elif lv == rv:
            change = "无变化"
        else:
            change = f"{lv}→{rv}"
        rows.append((cid, l.get("category", ""), lv, rv, change))

    print(f"\n## {left['tag']} → {right['tag']}\n")
    print(f"commit: {left['git_commit']} → {right['git_commit']}\n")
    print("| 题号 | 陷阱 | before | after | 变化 |")
    print("|---|---|---|---|---|")
    for cid, cat, lv, rv, change in rows:
        print(f"| {cid} | {cat} | {lv} | {rv} | {change} |")

    def rate(run, key):
        vals = [r.get(key) for r in run["results"] if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0

    def pass_rate(run):
        scored = [r for r in run["results"] if r["result"] in ("pass", "fail")]
        if not scored:
            return 0.0
        return sum(1 for r in scored if r["result"] == "pass") / len(scored)

    print(f"\n修复 {fixed} 题 / 回归 {broke} 题 / 共 {len(rows)} 题")
    print(f"通过率 {pass_rate(left):.0%} → {pass_rate(right):.0%}")
    print(f"平均耗时 {rate(left, 'latency'):.1f}s → {rate(right, 'latency'):.1f}s")
    if broke:
        print("\n⚠ 有回归，逐条看：")
        for cid, _cat, _lv, _rv, change in rows:
            if change == "**回归**":
                print(f"  - {cid}：before 有这个答案，after 没了")
    return 0


# ------------------------------------------------------------------ push（进 Langfuse Experiments）
def cmd_push(args) -> int:
    """把一次 run 的结果写成 Langfuse dataset items，让它在 Datasets/Experiments 里可见。"""
    path = run_path(args.tag)
    if not path.exists():
        print(f"找不到 {path}")
        return 1
    run = json.loads(path.read_text(encoding="utf-8"))

    host = (os.getenv("LANGFUSE_HOST") or "").rstrip("/")
    pk = os.getenv("LANGFUSE_PUBLIC_KEY")
    sk = os.getenv("LANGFUSE_SECRET_KEY")
    # 本地自建 Langfuse 的配置在 .env.langfuse 里（由 langfuse_trace 模块加载）
    if not (host and pk and sk):
        try:
            from agent.langfuse_trace import _CONFIG as LF

            host = (LF.get("LANGFUSE_HOST") or "").rstrip("/")
            pk = LF.get("LANGFUSE_PUBLIC_KEY")
            sk = LF.get("LANGFUSE_SECRET_KEY")
        except Exception:  # noqa: BLE001
            pass
    if not (host and pk and sk):
        print("没配 LANGFUSE_HOST / PUBLIC_KEY / SECRET_KEY，跳过")
        return 1

    dataset = args.dataset
    auth = (pk, sk)
    created = 0
    with httpx.Client(timeout=30) as c:
        # 数据集不存在就建
        c.post(f"{host}/api/public/datasets",
               json={"name": dataset, "description": "agent 回归题集（陷阱题）"}, auth=auth)
        for r in run["results"]:
            item = {
                "datasetName": dataset,
                "input": {"question": r["question"]},
                "expectedOutput": {"expected": r.get("expected", ""), "reason": r.get("reason", "")},
                "metadata": {
                    "case_id": r["id"],
                    "category": r.get("category"),
                    "result": r.get("result"),
                    "run_tag": run["tag"],
                    "git_commit": run["git_commit"],
                    "latency": r.get("latency"),
                    "tools": r.get("tools"),
                },
            }
            if r.get("thread_id") or r.get("trace_id"):
                item["sourceTraceId"] = r.get("trace_id")
            resp = c.post(f"{host}/api/public/dataset-items", json=item, auth=auth)
            if resp.status_code in (200, 201, 207):
                created += 1
            else:
                print(f"  {r['id']} 失败：{resp.status_code} {resp.text[:120]}")
    print(f"已推送 {created} 条到 dataset「{dataset}」→ 去 Datasets / Experiments 看")
    return 0


def main() -> int:
    # Windows 控制台是 GBK，答案里可能带 ⚠ 之类的字符，不能让它把整次运行崩掉
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="agent 回归对比台")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑一次并冻结成快照")
    r.add_argument("--tag", required=True, help="这次跑的标签，如 before-wiki")
    r.add_argument("--only", nargs="*", default=None, help="只跑指定题号，如 T01 T05")
    r.add_argument("--cases", default=None, help="题集 YAML 路径（默认 knowledge/trap_questions.yaml）")
    r.add_argument("--timeout", type=float, default=600.0,
                   help="单题总时长上限（秒），超时记 fail 并继续下一题")
    r.add_argument("--resume", action="store_true",
                   help="续跑：跳过同名 run 里已完成的题")
    r.set_defaults(func=cmd_run)

    cp = sub.add_parser("compare", help="对比两次 run")
    cp.add_argument("left")
    cp.add_argument("right")
    cp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("show", help="看某几题的完整记录")
    sp.add_argument("--tag", required=True)
    sp.add_argument("--id", required=True, help="题号，多个用逗号分隔：H03,H04")
    sp.set_defaults(func=cmd_show)

    rp = sub.add_parser("report", help="把一次 run 渲染成表格")
    rp.add_argument("--tag", required=True)
    rp.add_argument("--out", default=None, help="另存为 markdown 文件（可选）")
    rp.set_defaults(func=cmd_report)

    ps = sub.add_parser("push", help="把一次 run 推进 Langfuse dataset")
    ps.add_argument("--tag", required=True)
    ps.add_argument("--dataset", default="agent-bench")
    ps.set_defaults(func=cmd_push)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
