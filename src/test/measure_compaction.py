"""量化上下文压缩效果：拿真实 trace 的工具输出当语料。

两种压缩分开算，因为它们的杠杆完全不同：
1. **单条压缩**（日志正则 + 大结果下沉）—— 治"单条结果巨大"
2. **历史折叠**（老的工具结果折成一行）—— 治"结果累积"（实测这才是主因）

用法：python src/test/measure_compaction.py [trace 数量]
token 估算按 4 字符/token 粗算，只看相对变化。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "test"))

import httpx  # noqa: E402

import inspect_trace as it  # noqa: E402
from agent.middlewares.context_compaction import (  # noqa: E402
    FOLD_PREFIX_CHARS,
    KEEP_RECENT_RESULTS,
    compact,
)

_ = FOLD_PREFIX_CHARS, KEEP_RECENT_RESULTS  # 供文档参考


def fold_size(sizes: list[int], keep: int = 3, fold_chars: int = 240) -> int:
    """历史折叠后的总字符：最近 keep 条保留原文，其余折成 fold_chars。"""
    if len(sizes) <= keep:
        return sum(sizes)
    kept = sum(sizes[-keep:])
    folded = sum(min(s, fold_chars) + 30 for s in sizes[:-keep])
    return kept + folded


def main() -> int:
    n_traces = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    host, auth = it.lf_config()
    rows = []
    t_single_before = t_single_after = 0
    t_hist_before = t_hist_after = 0
    t_combo_after = 0

    with httpx.Client(timeout=90) as c:
        traces = (c.get(f"{host}/api/public/traces", params={"limit": n_traces}, auth=auth)
                  .json() or {}).get("data") or []
        for t in traces:
            if not (t.get("name") or "").startswith("erp-agent"):
                continue
            obs = it.fetch_obs(c, host, auth, t["id"])
            tools = sorted([o for o in obs if (o.get("type") or "").upper() == "TOOL"],
                           key=lambda o: o.get("startTime") or "")
            texts = [it._text(o.get("output")) for o in tools]
            texts = [x for x in texts if len(x) >= 200]
            if not texts:
                continue

            sizes = [len(x) for x in texts]
            compacted = [len(compact(x, "", None)) for x in texts]
            single_after = sum(compacted)
            hist_after = fold_size(sizes)                 # 只折叠
            combo_after = fold_size(compacted)            # 先下沉再折叠

            rows.append((t.get("name") or "", len(texts), sum(sizes),
                         single_after, hist_after, combo_after))
            t_single_before += sum(sizes)
            t_single_after += single_after
            t_hist_before += sum(sizes)
            t_hist_after += hist_after
            t_combo_after += combo_after

    print(f"{'任务':36s}{'结果数':>6s}{'原始':>8s}{'单条压缩':>9s}{'历史折叠':>9s}{'叠加':>9s}")
    for name, n, b, a1, a2, a3 in rows:
        print(f"{name[:34]:36s}{n:6d}{b:8d}{a1:9d}{a2:9d}{a3:9d}")

    if t_hist_before:
        print(f"\n合计（工具输出部分）")
        print(f"  原始           {t_hist_before:8d} 字符  ≈ {t_hist_before // 4} tokens")
        print(f"  单条压缩后     {t_single_after:8d} 字符  ≈ {t_single_after // 4} tokens"
              f"  降 {100 - t_single_after * 100 // t_hist_before}%")
        print(f"  历史折叠后     {t_hist_after:8d} 字符  ≈ {t_hist_after // 4} tokens"
              f"  降 {100 - t_hist_after * 100 // t_hist_before}%")
        print(f"  两者叠加       {t_combo_after:8d} 字符  ≈ {t_combo_after // 4} tokens"
              f"  降 {100 - t_combo_after * 100 // t_hist_before}%")
    else:
        print("没有可统计的工具输出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
