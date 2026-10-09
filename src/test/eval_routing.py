"""端到端路由评估（golden set）—— 验证主 Agent 实际把任务派给了谁。

这是批次 3 唯一能发现**软错误**（路由不准）的手段：启动期校验网只能拦硬错误
（工具名/档位/scope 写错），拦不住"派错域"。

原理：
  主 Agent 通过 deepagents 的 `task` 工具委派，参数里带 `subagent_type`。
  后端把工具调用以 SSE 事件（tool_start / tool_args / tool_result）推出来，
  本脚本累积 `task` 调用的 args，解析出 `subagent_type` —— 那就是实际委派对象。

安全：**只跑到中断为止，不调用 /resume**。写操作用例（下单、建档）会在
HITL 审批处停下，因此不会真的写库。

用法（需 ERP API:8080 + MCP:8099 + 后端:8090 全部就绪，VM 的 MongoDB/OpenSandbox 可用）:
    python test/eval_routing.py                 # 全量
    python test/eval_routing.py --only 9 12     # 只跑指定编号
    python test/eval_routing.py --timeout 240
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Optional

import httpx

DEFAULT_BASE = "http://127.0.0.1:8091"   # 后端 2026-09-29 从 8090 移到 8091（8090 被一个杀不掉的旧进程占着）
USER_ID = "aff"

# (编号, 用户提问, 期望委派, 是否期望触发 HITL, 类型说明)
GOLDEN: list[tuple[int, str, str, bool, str]] = [
    (1,  "洁力内衣专用洗衣液哪个仓库存不足？",            "inventory-logistics", False, "域明确"),
    (2,  "最近有哪些物流超期还没签收？",                  "inventory-logistics", False, "域明确"),
    (3,  "最近哪些商品销量异常？",                        "sales-analyst",       False, "域明确"),
    (4,  "茶悦柠檬茶这周销量为什么突然涨了？",            "sales-analyst",       False, "需结合日销"),
    (5,  "雅致香氛沐浴露哪家供应商报价最低？",            "procurement-analyst", False, "比价"),
    (6,  "帮我分析一下洁力的采购成本趋势",                "procurement-analyst", False, "分析"),
    (7,  "华惠万家的客户档案和折扣等级是什么？",          "master-data",         False, "主数据查询"),
    (8,  "我们有几个冰淇淋 SKU？",                        "master-data",         False, "主数据查询"),
    (9,  "帮我给巴孚下一张采购单",                      "procurement-order",   False, "写操作，缺字段也必须委派"),
    (10, "查一下我上个月下的采购单",                      "procurement-order",   False, "易混淆：操作视角"),
    (11, "分析一下历史采购订单的价格趋势",                "procurement-analyst", False, "易混淆：分析视角"),
    # —— 以下两条为 6 Agent 结构新增：主数据域的「读 / 写」对照 ——
    # 注意用库里【确实不存在】的供应商名。用「巴孚」是无效用例：
    # SUP001 就是巴孚（中国）有限公司，查重会命中，子 Agent 按 SOP 停下来问用户，
    # 于是不建档、也就不会有 HITL 中断 —— 那是正确行为，不是缺陷。
    #
    # 还有一个坑（2026-09-23 实地跑出来的）：**可选字段没给全时，子 Agent 会先追问**。
    # 实测只给「浙江新和成股份有限公司」时，它查完重就反问了联系人/信用评级，
    # 一轮结束也没调 supplier_create → 中断不触发。
    # 这是 SOP 里「不擅自补全、拿不准就问」准则的正常体现，所以 HITL 用例要把字段给全。
    (12, "帮我新增一个供应商：浙江新和成股份有限公司，联系人张伟，电话13800000000，信用评级B", "master-data-maintain", True, "写操作，应触发 HITL（字段给全，避免先追问）"),
    (13, "查一下巴孚的供应商档案",                      "master-data",          False, "易混淆：与 12 同域不同动作"),
    # —— 以下两条是「写操作 HITL 断言」的补强：把「高危写操作 100% 拦截」的分母从 1 提到 3 ——
    # 覆盖两个写 Agent（master-data-maintain 的建档、procurement-order 的下单），而不只是建档这一条。
    #
    # 写用例的两个坑（都是踩过的）：
    #   ① 建档用例必须用库里【确实不存在】的对象名。若查重命中，子 Agent 会按 SOP 停下来
    #      向用户确认 —— 那是正确行为，但不会触发写工具的 interrupt，用例会假失败。
    #      （12 号的原始注释里已经记过这个坑：用「巴孚」无效，因为 SUP001 就是巴孚。）
    #   ② 下单用例必须给全 orderDetail 的必填三项（productId / quantity / unitPrice）。
    #      缺字段时子 Agent 会先调 request_order_info 补数据，停在「数据补充」而不是「写审批」；
    #      两者都会产生 interrupt，所以要靠结果里的 interrupt_type 区分是不是写审批那一次。
    #      下面这条刻意写明「字段都齐了直接创建」，且**单价用商品目录价**（productId=3 是
    #      HFS-750-01，目录价 31.20）—— 若给一个对不上的价格，子 Agent 可能先反问"确认这个价吗"，
    #      同样会停在数据确认而不是写审批。
    #      客户开户这条同理：字段给全（类型/折扣等级/联系人），否则它会先反问折扣等级。
    (14, "帮我给宁波云栖供应链管理有限公司开个客户账户：类型经销商、折扣等级二级、联系人李敏、电话13800000000", "master-data-maintain", True, "写操作：客户开户，应触发 HITL"),
    (15, "帮我下单：商品ID=3，数量=500，单价31.20元，交货日期2026-10-15，字段都齐了直接创建", "procurement-order", True, "写操作：下单，应触发 HITL"),
]


def run_case(base: str, num: int, question: str, timeout: float) -> dict:
    """跑单个用例，返回观测结果。"""
    thread_id = f"eval-routing-{num}-{uuid.uuid4().hex[:8]}"
    result = {
        "num": num,
        "question": question,
        "thread_id": thread_id,
        "delegated": [],          # 实际委派的子 Agent（按出现顺序）
        "interrupt_type": None,   # 中断类型
        "tools": [],              # 出现的工具名（去重保序）
        "error": None,
        "elapsed": 0.0,
    }

    task_args: dict[str, str] = {}   # tool_call_id -> 累积的 args 文本
    id_to_name: dict[str, str] = {}
    t0 = time.time()

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
            with client.stream(
                "POST",
                f"{base}/api/chat/stream",
                json={"message": question, "thread_id": thread_id, "user_id": USER_ID},
                headers={"Accept": "text/event-stream"},
            ) as resp:
                if resp.status_code != 200:
                    result["error"] = f"HTTP {resp.status_code}"
                    return result
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        ev = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    etype = ev.get("type")

                    if etype == "tool_start":
                        name = ev.get("tool_name", "")
                        cid = ev.get("tool_call_id", "")
                        id_to_name[cid] = name
                        if name not in result["tools"]:
                            result["tools"].append(name)

                    elif etype == "tool_args":
                        cid = ev.get("tool_call_id", "")
                        task_args[cid] = task_args.get(cid, "") + (ev.get("args") or "")

                    elif etype == "interrupt":
                        result["interrupt_type"] = ev.get("interrupt_type")

                    elif etype == "done":
                        break
                    elif etype == "error":
                        result["error"] = str(ev.get("message") or ev)[:300]
                        break
    except Exception as e:  # 超时 / 连接失败
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    result["elapsed"] = round(time.time() - t0, 1)

    # 从累积的 task 参数里解析实际委派对象
    for cid, raw in task_args.items():
        if id_to_name.get(cid) != "task":
            continue
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            # args 是流式拼接的，可能残留尾部；尝试截到最后一个 }
            try:
                args = json.loads(raw[: raw.rindex("}") + 1])
            except Exception:
                continue
        st = args.get("subagent_type")
        if isinstance(st, str) and st and st not in result["delegated"]:
            result["delegated"].append(st)

    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--only", nargs="*", type=int, help="只跑指定编号")
    ap.add_argument("--timeout", type=float, default=180.0, help="单例超时（秒）")
    ap.add_argument("--json", dest="json_out", help="把结果写到该 JSON 文件")
    args = ap.parse_args()

    cases = [c for c in GOLDEN if not args.only or c[0] in args.only]

    print("=" * 104)
    print("端到端路由评估 —— 实际委派对象 vs 期望")
    print("=" * 104)
    print("%-4s %-38s %-22s %-22s %s" % ("#", "提问", "期望", "实际", "结论"))
    print("-" * 104)

    results = []
    passed = 0
    for num, q, expect, expect_hitl, kind in cases:
        r = run_case(args.base, num, q, args.timeout)
        actual = r["delegated"]
        hitl = r["interrupt_type"]

        ok_route = actual == [expect]
        ok_hitl = (hitl is not None) if expect_hitl else True
        ok = ok_route and ok_hitl and not r["error"]

        if ok:
            passed += 1

        shown_q = q if len(q) <= 36 else q[:35] + "…"
        actual_s = ",".join(actual) if actual else ("(无委派)" if not r["error"] else "(出错)")
        verdict = "PASS" if ok else "FAIL"
        if ok_route and not ok:
            if not ok_hitl:
                verdict += " 未触发HITL"
            if r["error"]:
                verdict += " " + r["error"][:40]

        print("%-4d %-38s %-22s %-22s %s  [%s, %.0fs]" % (
            num, shown_q, expect, actual_s, verdict, kind, r["elapsed"]))

        r.update({"expect": expect, "expect_hitl": expect_hitl, "kind": kind,
                  "pass": ok, "delegated_hitl": hitl})
        results.append(r)

    print("-" * 104)
    print("结果: %d/%d 通过" % (passed, len(cases)))

    misses = [r for r in results if not r["pass"]]
    if misses:
        print()
        print("未通过的用例详情:")
        for r in misses:
            print("  #%d %s" % (r["num"], r["question"]))
            print("      期望=%s  实际=%s  中断=%s  错误=%s" % (
                r["expect"], r["delegated"], r["delegated_hitl"], r["error"]))
            print("      出现的工具: %s" % (r["tools"] or "无"))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print("\n结果已写入 %s" % args.json_out)

    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
