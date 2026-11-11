"""跑 Langfuse 数据集实验：把 dataset 里的题逐条喂给你本地 agent，自动打分并落成一次 Experiment。

和 eval_scores.py 的区别：
    eval_scores.py  期望值写在代码里的 GOLDEN，分数挂到「单条 trace」上。
    本脚本          期望值来自 Langfuse 的 dataset 条目，结果落成「一次 experiment run」，
                    跑两次（baseline-v1 / v2）就能在 Experiments 里对比。

三个你自己填的地方（都标了 ★）：
    ★1 task()     怎么把「题面」变成「一次真实调用」——输入输出格式只有你知道
    ★2 三个 evaluator  判定口径（这里给的是和 eval_scores.py 一致的规则，可以改）
    ★3 run_name   这次实验叫啥（决定对比时看到的两列）

用法：
    # 只看数据集长什么样，不真跑（省 token）
    python src/test/run_experiment.py --dry-run

    # 真跑 2 条试试
    python src/test/run_experiment.py --run-name baseline-v1 --limit 2

    # 全量
    python src/test/run_experiment.py --run-name baseline-v1
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文会乱码 —— 强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = HERE.parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

# 先加载配置（会把 .env.langfuse 写进 os.environ），再 import langfuse
import agent.langfuse_trace  # noqa: F401,E402
from langfuse import Evaluation, get_client  # noqa: E402

# 复用现成的跑批 + SSE 解析，避免两套解析漂移
_spec = importlib.util.spec_from_file_location("eval_routing", HERE / "eval_routing.py")
_ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ev)
run_case = _ev.run_case

DATASET_NAME = "erp-golden"


def _input_question(item) -> str:
    """★1 的一半：题面怎么取。

    你建条目时 Input 存的是 {"question": "..."}，所以这里取 question 字段；
    如果哪天改成直接存字符串，这个函数也能兼容。
    """
    raw = item.input
    if isinstance(raw, dict):
        return raw.get("question") or raw.get("input") or str(raw)
    return str(raw)


def task(*, item, **kwargs) -> dict:
    """★1：一次「被测系统调用」。

    Langfuse 会把 dataset item 传进来，你要返回这条用例的「实际输出」。
    因为你的 agent 是 HTTP + SSE 服务，这里直接打后端的 /api/chat/stream。
    """
    question = _input_question(item)
    question = str(question)
    case_no = (item.metadata or {}).get("case_no", 0)
    result = run_case(BASE_URL, case_no, question, TIMEOUT)

    # ★ 关键：调用失败必须抛错，不能让"空结果"被当成"没委派、没误停 = 通过"。
    #   实测踩过：后端没起来时，hitl_status 反而算出了 1.0（假通过）。
    if result.get("error"):
        raise RuntimeError(f"调用后端失败：{result['error']}")

    return {
        "delegated": result.get("delegated") or [],
        "interrupt_type": result.get("interrupt_type"),
        "tools": result.get("tools") or [],
    }


def _expected_route(expected_output) -> str | None:
    """期望值怎么取：建条目时 Expected output 存的是 {"answer": "..."}。"""
    if isinstance(expected_output, dict):
        return expected_output.get("answer")
    if isinstance(expected_output, str):
        return expected_output
    return None


def eval_route_correct(*, input, output, expected_output, metadata, **kwargs) -> Evaluation:
    """★2-① 路由对不对：实际委派的第一个子 Agent == 期望。"""
    expected = _expected_route(expected_output)
    delegated = (output or {}).get("delegated") or []
    actual = delegated[0] if delegated else "(未委派)"
    return Evaluation(
        name="route_correct",
        value=1 if expected and actual == expected else 0,
        data_type="BOOLEAN",
        comment=f"期望 {expected}｜实际 {actual}",
    )


def eval_hitl_status(*, input, output, expected_output, metadata, **kwargs) -> Evaluation:
    """★2-② 该不该停：correct / missed（漏停）/ spurious（误停）。

    期望值来自条目的 metadata.should_hitl —— 这就是为什么它必须放 metadata 而不是 expected_output：
    expected_output 是「答案」，metadata 是「判定条件」。
    """
    expect_hitl = bool((metadata or {}).get("should_hitl", False))
    actual_hitl = (output or {}).get("interrupt_type") is not None

    if expect_hitl and actual_hitl:
        status = "correct"
    elif expect_hitl and not actual_hitl:
        status = "missed"
    elif (not expect_hitl) and actual_hitl:
        status = "spurious"
    else:
        status = "correct"

    # ⚠️ CATEGORICAL 的 value 必须是**分类标签字符串**，传数字会被 API 拒（400）。
    #    实测：{"value": "correct"} → 200；{"value": 1} → 400。
    return Evaluation(
        name="hitl_status",
        value=status,
        data_type="CATEGORICAL",
        comment=f"期望停={expect_hitl} 实际停={actual_hitl}",
    )


def eval_tools_used(*, input, output, expected_output, metadata, **kwargs) -> Evaluation:
    """★2-③ 顺手记一个事实类指标：这轮用了几个工具。"""
    tools = (output or {}).get("tools") or []
    return Evaluation(
        name="tools_used",
        value=len(tools),
        data_type="NUMERIC",
        comment=", ".join(tools) or "无",
    )


def _port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    global BASE_URL, TIMEOUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-name", default="baseline-v1")   # ★3
    ap.add_argument("--dataset", default=DATASET_NAME)
    ap.add_argument("--base", default="http://127.0.0.1:8091")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    BASE_URL, TIMEOUT = args.base, args.timeout

    client = get_client()
    dataset = client.get_dataset(args.dataset)
    items = list(dataset.items)
    if args.limit:
        items = items[: args.limit]

    print(f"数据集 {args.dataset}：{len(items)} 条")
    for it in items:
        md = it.metadata or {}
        print(f"  - case#{md.get('case_no','?'):>3}  {str(_input_question(it))[:36]}…"
              f"  期望={_expected_route(it.expected_output)}  should_hitl={md.get('should_hitl')}")

    if args.dry_run:
        print("\n--dry-run：只打印条目结构，不跑 agent、不建 experiment。")
        return 0

    if not items:
        print("数据集是空的，先去界面加条目。")
        return 2

    # 起跑前先探端口：服务没起来就去跑，只会得到一堆"假分数"
    from urllib.parse import urlparse

    _u = urlparse(args.base)
    if not _port_open(_u.hostname or "127.0.0.1", _u.port or 80):
        print(f"[中止] 后端 {args.base} 连不上。先启动 8080/8099/8091 再来跑。")
        return 3

    print(f"\n开始跑 experiment：{args.run_name}（并发 2，逐条调 {args.base}）")
    result = client.run_experiment(
        name=args.dataset,
        run_name=args.run_name,
        description=f"本地 agent 自动评测 · run={args.run_name}",
        data=items,
        task=task,
        evaluators=[eval_route_correct, eval_hitl_status, eval_tools_used],
        max_concurrency=2,
    )

    print("\n===== 汇总 =====")
    # SDK 的实验结果对象没有固定字段，优先用自带的格式化输出
    try:
        print(result.format())
    except Exception:  # noqa: BLE001
        print(getattr(result, "__dict__", result))
    print("\n去 Langfuse：Datasets → erp-golden → Experiments，能看到这次 run。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
