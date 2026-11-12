"""校验难题集：每条标准 SQL 能不能跑通、结果是否非空、返回什么。

用法：python src/test/validate_cases.py knowledge/text2sql_hard.yaml
没有标准 SQL 的题会跳过；结果为空会标 WARN（空结果无法对拍）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "src"))

from agent.tools.sql_tools import create_sql_tools  # noqa: E402


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "knowledge" / "trap_questions_v2.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = data["questions"]
    q = {t.name: t for t in create_sql_tools()}["sql_query"]

    ok = warn = bad = skip = 0
    for c in cases:
        gt = c.get("ground_truth_sql")
        if not gt:
            skip += 1
            continue
        out = q(gt)
        if out.startswith("[SQL"):
            bad += 1
            print(f"BAD   {c['id']}  {out.splitlines()[0][:120]}")
            continue
        head = out.strip().splitlines()
        # 用正则取行数：直接判断 "0 行" 会误伤 "10 行"/"20 行"（子串匹配）
        m = re.search(r"\((\d+)\s*行", out)
        n_rows = int(m.group(1)) if m else -1
        if n_rows == 0:
            warn += 1
            print(f"WARN  {c['id']}  标准 SQL 无结果（无法对拍）")
        else:
            ok += 1
            print(f"OK    {c['id']}  {n_rows} 行  →  {head[2][:70] if len(head) > 2 else ''}")

    print(f"\n合计：ok={ok} bad={bad} warn={warn} skip(无标准SQL)={skip}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
