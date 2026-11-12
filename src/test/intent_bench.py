"""意图识别评测：只测"理解"，不跑整个 agent。

做法：把每个问题喂给一个**路由 prompt**，要求模型输出 JSON：
`{domain, intent, slots{time_window, metric, dimension}, should_clarify}`，
然后与 `knowledge/intent_cases.yaml` 的标注比对。

为什么这样测（而不是跑完整 agent）：
- 一次调用 2 秒，60 条 2 分钟跑完，可以快速迭代；
- 错了能定位到"域错 / 槽位漏 / 该问没问"，而跑完整 agent 只能看到"最终答案错"。

用法：
    python src/test/intent_bench.py              # 全量
    python src/test/intent_bench.py --limit 10   # 先跑 10 条看效果
    python src/test/intent_bench.py --show-fail  # 打印错例
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from agent.config import SUMMARY_MODEL  # noqa: E402  （用便宜的小模型做路由）

CASES = ROOT / "knowledge" / "intent_cases.yaml"

ROUTER_PROMPT = """你是供应链 ERP 助手的**意图识别器**。只做理解，不查数据、不写 SQL。

六个可路由的子 Agent：
- sales-analyst：销量、销售额、趋势、异常、占比、目标达成率、奖金
- inventory-logistics：库存、安全库存、补货、覆盖天数、物流、运单、在途、超期
- procurement-analyst：供应商比价、采购成本、采购金额、交付周期、按月统计采购单
- master-data：商品/供应商/客户档案查询、字段含义、SKU 数量
- procurement-order：创建/修改/取消采购单、查某一张单的状态
- master-data-maintain：新增供应商/客户/商品

路由铁律：
1. **统计/分析采购单（数量、金额、周期、按月分布）→ procurement-analyst**；
   只有"创建/修改/取消/查这一单状态"才是 procurement-order。
2. 判"读"还是"写"看**动词**：查/看/有哪些 → 只读域；新增/建档/录入/改成/取消 → 写 Agent。
3. 问候、功能询问、通用知识（什么是 X、行业概念）→ domain = "none"（主 Agent 自己处理）。
4. "这个商品什么规格"→ master-data；"这个商品库存多少"→ inventory-logistics。
5. 系统里**没有**退货率、客户满意度、未来预测数据 → intent = "refuse"。

槽位规则：
- time_window：**句子里出现了明确月份/年份就必须填**（今天是 2026-11-12，所以"11 月"= "2026-11"，
  "10 月" = "2026-10"，"去年 11 月" = "2025-11"，"今年" = "2026"）。
  只有"最近/上个月/这个"这类**没有明确口径**的表述才填 null。
- metric：指标名（销量/销售额/库存覆盖天数/采购金额/目标达成率 …）。
  **查询档案/规格/含义**这类不是指标 → null。
- dimension：分组维度（channel/category/product/supplier/warehouse/month），没有就 null。
- should_clarify：**只在"缺口径会导致答错"时为 true**，典型三种：
  ① "上个月/最近"没有明确自然月还是滚动天数；② "卖得最好"没说是销量还是金额；
  ③ 拿不完整月与完整月直接对比。
  **以下不要设 true**：句子里已给出明确月份/年份；归因类问题（"为什么下降"可以先拆解分析）；
  写操作缺字段（应由写 Agent 索要）。

只输出一个 JSON 对象，不要解释：
{"domain":"...","intent":"query|analysis|action|clarify|refuse","slots":{"time_window":...,"metric":...,"dimension":...},"should_clarify":true|false}
"""


def _json_of(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}


def _norm_time(v) -> str:
    """把时间窗口表述归一化，避免"11 月（年份未指定）"这种差异被算错。

    归一化目标：
      "2026-11 自然月" / "11 月" / "2026-11"  →  "2026-11"
      "近 30 天" / "30 天"                    →  "30d"
      "2025 全年" / "全部历史"                 →  "all" 或 "2025"
      "月初至今" / "本月至今"                  →  "mtd"
      None / ""                                →  ""
    """
    if v in (None, "", "null"):
        return ""
    s = str(v)
    if re.search(r"近\s*30\s*天|30\s*天", s):
        return "30d"
    if re.search(r"月初至今|本月至今|至今", s):
        return "mtd"
    m = re.search(r"(20\d{2})\s*[-年/]\s*(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.search(r"(20\d{2})\s*全年", s)
    if m:
        return f"{m.group(1)}-all"
    if re.search(r"全部|历史|all", s):
        return "all"
    return re.sub(r"\s+", "", s)


def _norm_metric(v) -> str:
    """指标归一化：去掉修饰词，只比"指标骨架"。

    做法：取关键词集合的交集（如"库存低于安全库存" 与 "库存安全度" 都含"库存"+"安全"）。
    """
    if v in (None, "", "null"):
        return ""
    s = re.sub(r"[\s，,。.、（）()]+", "", str(v))
    return s


def _metric_match(pred, gold) -> bool:
    """指标是否语义相近。

    注意：gold 为 null 表示"这一题不要求识别指标"（如档案查询），
    此时模型填什么都不扣分 —— 否则测的是措辞而不是理解。
    """
    if gold in (None, "", "null"):
        return True
    g, p = _norm_metric(gold), _norm_metric(pred)
    if not p:
        return False
    if g in p or p in g:
        return True
    grams = {g[i:i + 2] for i in range(len(g) - 1)}
    return any(gram in p for gram in grams)


def _same_slot(pred, gold) -> bool:
    """通用槽位比对（时间窗口走专门归一化，指标走关键词匹配）。"""
    if gold in (None, "", "null"):
        return pred in (None, "", "null")
    if not pred:
        return False
    a, b = str(pred).strip(), str(gold).strip()
    return a == b or a in b or b in a


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show-fail", action="store_true")
    args = ap.parse_args()

    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
    if args.limit:
        cases = cases[: args.limit]

    domain_ok = slot_tw = slot_metric = clarify_ok = 0
    fails = []
    for c in cases:
        try:
            resp = SUMMARY_MODEL.invoke([
                {"role": "system", "content": ROUTER_PROMPT},
                {"role": "user", "content": c["q"]},
            ])
            text = getattr(resp, "content", "") or ""
            pred = _json_of(text if isinstance(text, str) else str(text))
        except Exception as e:  # noqa: BLE001
            pred = {}
            print(f"  [{c['id']}] 调用失败: {e}")

        gold_slots = c.get("slots") or {}
        pred_slots = (pred.get("slots") or {}) if isinstance(pred, dict) else {}
        d_ok = (pred.get("domain") == c["domain"])
        tw_ok = _norm_time(pred_slots.get("time_window")) == _norm_time(gold_slots.get("time_window"))
        m_ok = _metric_match(pred_slots.get("metric"), gold_slots.get("metric"))
        cl_ok = (bool(pred.get("should_clarify")) == bool(c.get("should_clarify")))

        domain_ok += d_ok
        slot_tw += tw_ok
        slot_metric += m_ok
        clarify_ok += cl_ok
        if not (d_ok and tw_ok and m_ok and cl_ok):
            fails.append((c, pred, dict(domain=d_ok, tw=tw_ok, metric=m_ok, clarify=cl_ok)))

    n = len(cases)
    print(f"\n用例数: {n}")
    print(f"  域准确率      {domain_ok}/{n} = {domain_ok * 100 // n}%")
    print(f"  时间窗口准确率 {slot_tw}/{n} = {slot_tw * 100 // n}%")
    print(f"  指标准确率    {slot_metric}/{n} = {slot_metric * 100 // n}%")
    print(f"  澄清决策准确率 {clarify_ok}/{n} = {clarify_ok * 100 // n}%")
    full = sum(1 for c in cases if c["id"] not in {f[0]["id"] for f in fails})
    print(f"  全部正确      {full}/{n} = {full * 100 // n}%")

    if args.show_fail and fails:
        print("\n=== 错例 ===")
        for c, pred, flags in fails[:15]:
            bad = [k for k, v in flags.items() if not v]
            print(f"\n[{c['id']}] {c['q']}")
            print(f"  错在: {bad}")
            print(f"  期望: domain={c['domain']} slots={c.get('slots')} clarify={c.get('should_clarify')}")
            print(f"  实际: {json.dumps(pred, ensure_ascii=False)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
