"""自检：Text2SQL 工具族（list/describe/sample/query + 安全校验）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agent.tools.sql_tools import create_sql_tools  # noqa: E402


def main() -> int:
    tools = {t.name: t for t in create_sql_tools()}

    print("== sql_list_tables ==")
    print(tools["sql_list_tables"]()[:400])

    print("\n== sql_describe_table(supplier) ==")
    print(tools["sql_describe_table"]("supplier")[:400])

    print("\n== sql_sample_rows(supplier, 2) ==")
    print(tools["sql_sample_rows"]("supplier", 2)[:400])

    print("\n== sql_query: 供应商数量 ==")
    print(tools["sql_query"]("select count(*) as n from supplier"))

    print("\n== sql_query: 自动加 LIMIT ==")
    print(tools["sql_query"]("select id, name from supplier")[:300])

    print("\n== 安全校验：DELETE 应被拒 ==")
    print(tools["sql_query"]("delete from supplier where id=1"))

    print("\n== 安全校验：多语句应被拒 ==")
    print(tools["sql_query"]("select 1; drop table supplier"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
