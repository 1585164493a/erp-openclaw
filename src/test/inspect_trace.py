"""看一条 trace 慢在哪：慢 span / 工具分布 / 时间空档 / 报错。

用法：
    python src/test/inspect_trace.py <trace_id>
    python src/test/inspect_trace.py --last          # 看最近一条
    python src/test/inspect_trace.py <trace_id> --top 15

不需要问人：它是自包含的，直接读 Langfuse 的公共 API。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))


def lf_config() -> tuple[str, tuple[str, str]]:
    host = (os.getenv("LANGFUSE_HOST") or "").rstrip("/")
    pk, sk = os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")
    if not (host and pk and sk):
        # 本地自建配置在 .env.langfuse
        from agent.langfuse_trace import _CONFIG as LF

        host = (LF.get("LANGFUSE_HOST") or "").rstrip("/")
        pk, sk = LF.get("LANGFUSE_PUBLIC_KEY"), LF.get("LANGFUSE_SECRET_KEY")
    return host, (pk, sk)


def fetch_trace(c: httpx.Client, host: str, auth, tid: str) -> dict:
    r = c.get(f"{host}/api/public/traces/{tid}", auth=auth)
    r.raise_for_status()
    return r.json()


def fetch_obs(c: httpx.Client, host: str, auth, tid: str, max_pages: int = 10) -> list[dict]:
    """取一条 trace 的观测。

    注意：本版 Langfuse 的 /observations **不接受 page 参数**（传了会 422），
    所以这里先不带 page 取一次；拿满 100 条时再尝试用 page 翻页，失败就算了。
    """
    r = None
    for attempt in range(3):
        r = c.get(f"{host}/api/public/observations",
                  params={"traceId": tid, "limit": 100}, auth=auth)
        if r.status_code == 200:
            break
        # 实测这个接口偶发 422（连发多次时），重试一次就好
        time.sleep(2)
    if r is None or r.status_code != 200:
        return []
    data = (r.json() or {}).get("data") or []
    out = list(data)
    if len(data) < 100:
        return out
    for page in range(2, max_pages + 1):
        r2 = c.get(f"{host}/api/public/observations",
                   params={"traceId": tid, "limit": 100, "page": page}, auth=auth)
        if r2.status_code != 200:
            break
        more = (r2.json() or {}).get("data") or []
        if not more:
            break
        out.extend(more)
        if len(more) < 100:
            break
    return out


def dur(o: dict) -> float:
    try:
        from datetime import datetime

        s = datetime.fromisoformat(o["startTime"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(o["endTime"].replace("Z", "+00:00"))
        return (e - s).total_seconds()
    except Exception:  # noqa: BLE001
        return 0.0


def _text(v) -> str:
    """把 observation 的 input/output 压成一段可读文本。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.replace("\n", " ")
    if isinstance(v, dict):
        msgs = v.get("messages")
        if isinstance(msgs, list) and msgs:
            tail = msgs[-1]
            if isinstance(tail, dict):
                c = tail.get("content")
                if isinstance(c, str) and c.strip():
                    return c.replace("\n", " ")
                tcs = tail.get("tool_calls") or []
                if tcs:
                    names = [t.get("name") for t in tcs if isinstance(t, dict)]
                    return "tool_calls: " + ", ".join(n for n in names if n)
            return str(tail)[:400].replace("\n", " ")
        for key in ("content", "output", "text"):
            if isinstance(v.get(key), str):
                return v[key].replace("\n", " ")
    try:
        import json

        return json.dumps(v, ensure_ascii=False)[:400].replace("\n", " ")
    except Exception:  # noqa: BLE001
        return str(v)[:400].replace("\n", " ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace_id", nargs="?", default=None)
    ap.add_argument("--last", action="store_true", help="用最近一条 trace")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--gens", action="store_true", help="逐次打印模型调用的输入/输出")
    args = ap.parse_args()

    host, auth = lf_config()
    with httpx.Client(timeout=90) as c:
        tid = args.trace_id
        if args.last or not tid:
            data = c.get(f"{host}/api/public/traces", params={"limit": 1}, auth=auth).json()["data"]
            tid = data[0]["id"]

        t = fetch_trace(c, host, auth, tid)
        obs = fetch_obs(c, host, auth, tid)

    print(f"trace  {tid}")
    print(f"名字    {t.get('name')}")
    print(f"总耗时  {t.get('latency')}s    观测数 {len(obs)}")

    tools = [o for o in obs if (o.get("type") or "").upper() == "TOOL"]
    gens = [o for o in obs if (o.get("type") or "").upper() == "GENERATION"]
    errs = [o for o in obs if (o.get("level") or "").upper() == "ERROR"]
    gsum = sum(dur(o) for o in gens)

    print(f"工具调用 {len(tools)} 次　模型调用 {len(gens)} 次（合计 {gsum:.1f}s）")

    # 1) 最慢的 span
    print(f"\n== 最慢的 {args.top} 个 span ==")
    for o in sorted(obs, key=dur, reverse=True)[: args.top]:
        print(f"  {dur(o):8.1f}s  {(o.get('type') or ''):10s} {o.get('name')}")

    # 2) 工具分布
    if tools:
        print("\n== 工具调用分布 ==")
        cnt: dict[str, int] = {}
        tsum: dict[str, float] = {}
        for o in tools:
            n = o.get("name") or "?"
            cnt[n] = cnt.get(n, 0) + 1
            tsum[n] = tsum.get(n, 0.0) + dur(o)
        for n, k in sorted(cnt.items(), key=lambda kv: -kv[1])[:12]:
            print(f"  {n:28s} x{k:<3d} 合计 {tsum[n]:7.1f}s")

    # 3) 时间空档：相邻 span 之间 > 20s 的静默
    rows = []
    for o in obs:
        try:
            from datetime import datetime

            rows.append((datetime.fromisoformat(o["startTime"].replace("Z", "+00:00")),
                         datetime.fromisoformat(o["endTime"].replace("Z", "+00:00")),
                         o))
        except Exception:  # noqa: BLE001
            continue
    rows.sort(key=lambda r: r[0])
    gaps = []
    prev_end = None
    for s, e, o in rows:
        if prev_end and (s - prev_end).total_seconds() > 20:
            gaps.append(((s - prev_end).total_seconds(), o))
        prev_end = e if prev_end is None or e > prev_end else prev_end
    if gaps:
        print("\n== 时间空档 > 20s ==")
        for g, o in sorted(gaps, reverse=True)[:5]:
            print(f"  {g:8.1f}s 期间无 span → 之后是 {(o.get('type') or '')} {o.get('name')}")

    # 4) 报错
    if errs:
        print(f"\n== 报错 {len(errs)} 条 ==")
        for o in errs[:5]:
            msg = (o.get("statusMessage") or "")[:120]
            print(f"  {(o.get('type') or '')} {o.get('name')}: {msg}")

    # 5) 逐次模型调用的输入/输出（排查死循环用）
    if args.gens:
        gens = sorted([o for o in obs if (o.get("type") or "").upper() == "GENERATION"],
                      key=lambda o: o.get("startTime") or "")
        print(f"\n== 模型调用逐次明细（共 {len(gens)} 次）==")
        for idx, o in enumerate(gens, 1):
            out = _text(o.get("output"))
            inp = _text(o.get("input"))
            print(f"\n[{idx}] {dur(o):.1f}s  model={o.get('model')}")
            print(f"  输入尾部: {inp[-260:] if inp else '（空）'}")
            print(f"  输出: {out[:260] if out else '（空）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
