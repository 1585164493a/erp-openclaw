"""Text2SQL 工具族（agentic 形态）：让模型自己看结构、抽样、执行。

设计要点（对齐业界的 agentic text2sql）：
1. **不把全库 DDL 塞进 prompt** —— 给 list/describe/sample 三个探索工具，按需取；
2. **执行必带 LIMIT** —— 模型写 `SELECT *` 也炸不了上下文；
3. **只读 + 单语句 + 超时** —— 三层防护，任何一层都能独立挡住误写；
4. **失败回喂** —— SQL 报错时把 MySQL 的错误原文返回，模型据此自我修正。

安全边界（重要）：
- 应用层：关键字白名单 + 只允许 SELECT/WITH + 单语句 + 强制 LIMIT + max_execution_time
- 数据库层：建议另建只读账号（见 `create_readonly_user`），应用层校验被绕过时仍写不进去
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

# 只允许这些语句开头
_ALLOWED_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
# 出现即拒绝的关键字（写操作 / 提权 / 落文件）
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|replace|drop|alter|truncate|create|grant|revoke|"
    r"load_file|outfile|dumpfile|handler|call|execute|prepare|set\s+global|"
    r"lock\s+tables|into\s+outfile|into\s+dumpfile)\b",
    re.IGNORECASE,
)
# 从 SQL 里粗提表名（用于白名单校验）
_TABLE_RE = re.compile(r"\b(?:from|join)\s+`?([A-Za-z0-9_]+)`?", re.IGNORECASE)

DEFAULT_LIMIT = 200
MAX_LIMIT = 500
QUERY_TIMEOUT_MS = 20_000

# 结构信息的进程内缓存：一次请求里模型经常重复问"这表什么结构"，
# 缓存掉就不会每次都打一遍 information_schema（实测一轮里 meta_schema 被调了 6 次）。
_SCHEMA_CACHE: dict[str, Any] = {}


def _conn():
    """每次调用新建连接：简单、无状态，量小完全够用。"""
    import pymysql

    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE", "motorparts_db"),
        charset="utf8mb4",
        connect_timeout=8,
        read_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )


def validate_sql(sql: str, allow_tables: set[str] | None = None) -> str:
    """校验并规范化 SQL；不合法直接抛 ValueError（消息会给模型看，要写人话）。"""
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise ValueError("SQL 为空")
    if ";" in text:
        raise ValueError("只允许单条语句（检测到分号）")
    if not _ALLOWED_START.match(text):
        raise ValueError("只允许 SELECT / WITH 查询（这是只读通道）")
    bad = _FORBIDDEN.search(text)
    if bad:
        raise ValueError(f"包含被禁止的关键字：{bad.group(1)}（只读通道不允许写操作）")

    if allow_tables:
        used = {t.lower() for t in _TABLE_RE.findall(text)}
        outside = sorted(used - {t.lower() for t in allow_tables})
        if outside:
            raise ValueError(f"这些表不在可查白名单里：{', '.join(outside)}")

    if not re.search(r"\blimit\b", text, re.IGNORECASE):
        text = f"SELECT * FROM ({text}) AS _wrapped LIMIT {DEFAULT_LIMIT}"
    return text


def _column_hint(err: str) -> str:
    """SQL 报"未知列"时，从 information_schema 找相似列名回给模型。

    为什么需要：按需取 schema（只取 2~3 张表）虽然省 token，但模型可能用到没取过的列。
    有了这个提示，它一轮就能自我修正，而不是拿现有字段硬凑出一个缺列的答案。
    """
    m = re.search(r"Unknown column '([^']+)'", err)
    if not m:
        return ""
    raw = m.group(1)
    col = raw.split(".")[-1].strip("`")
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                select table_name, column_name from information_schema.columns
                where table_schema = database() and column_name like %s
                limit 12
                """,
                (f"%{col}%",),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return f"库里没有名字接近 `{col}` 的列，先用 sql_list_tables 看看有哪些表"
    items = [f"{r.get('TABLE_NAME') or r.get('table_name')}.{r.get('COLUMN_NAME') or r.get('column_name')}"
             for r in rows]
    return "相似列：" + ", ".join(items) + "（用 sql_schema_overview 取对应表确认）"


def _fmt(rows: list[dict], max_cell: int = 120) -> str:
    """把结果格式化成紧凑表格文本（省 token，模型也好读）。"""
    if not rows:
        return "(0 行)"
    cols = list(rows[0].keys())
    lines = [" | ".join(cols), "-" * 40]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            s = "" if v is None else str(v)
            if len(s) > max_cell:
                s = s[:max_cell] + "…"
            cells.append(s)
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def create_sql_tools(allow_tables: set[str] | None = None) -> list[Any]:
    """创建 4 个 SQL 工具。allow_tables 为 None 时不做表白名单限制。"""

    def sql_list_tables() -> str:
        """列出数据库里所有表和行数。第一次接触数据时先调它。"""
        if "tables" in _SCHEMA_CACHE:
            return _SCHEMA_CACHE["tables"]
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("show tables")
            tables = [list(r.values())[0] for r in cur.fetchall()]
            out = []
            for t in tables:
                try:
                    cur.execute("select count(*) as n from `%s`" % t)
                    n = cur.fetchone()["n"]
                except Exception:  # noqa: BLE001
                    n = "?"
                out.append(f"{t}  ({n} 行)")
            text = "\n".join(out)
            _SCHEMA_CACHE["tables"] = text
            return text
        finally:
            conn.close()

    def sql_schema_overview(tables: str) -> str:
        """看指定表的字段总览（紧凑格式，省 token）。

        Args:
            tables: 表名，多个用逗号分隔，例如 "supplier,product_supplier"。
                    先用 sql_list_tables 确认表名。

        不要用 meta_schema 反复拉全库结构：只取你真正要 join 的 2~3 张表即可。
        """
        wanted = [t.strip() for t in (tables or "").split(",") if t.strip()]
        if not wanted:
            return "请传入表名（逗号分隔），例如 supplier,product"
        key = "overview:" + ",".join(sorted(wanted))
        if key in _SCHEMA_CACHE:
            return _SCHEMA_CACHE[key]
        conn = _conn()
        try:
            cur = conn.cursor()
            placeholders = ",".join(["%s"] * len(wanted))
            cur.execute(
                f"""
                select table_name, column_name, column_type, column_comment
                from information_schema.columns
                where table_schema = database() and table_name in ({placeholders})
                order by table_name, ordinal_position
                """,
                wanted,
            )
            rows = cur.fetchall()
            by_table: dict[str, list[str]] = {}
            for r in rows:
                t = r.get("TABLE_NAME") or r.get("table_name")
                c = r.get("COLUMN_NAME") or r.get("column_name")
                ty = r.get("COLUMN_TYPE") or r.get("column_type")
                cm = r.get("COLUMN_COMMENT") or r.get("column_comment")
                # create_time / update_time 是噪音，不占 token；需要时用 sql_describe_table
                if c in ("create_time", "update_time"):
                    continue
                cell = f"{c} {ty}" + (f"({cm})" if cm else "")
                by_table.setdefault(t, []).append(cell)
            if not by_table:
                return f"没找到这些表：{tables}（先用 sql_list_tables 确认表名）"
            text = "\n".join(f"{t}: " + ", ".join(cols) for t, cols in by_table.items())
            # 附一行"库里还有哪些表"：避免模型以为只有这几张表，拿现有字段硬凑
            cur.execute("show tables")
            others = [list(r.values())[0] for r in cur.fetchall()]
            remaining = [t for t in others if t not in by_table]
            if remaining:
                text += f"\n\n（库中还有：{', '.join(remaining)} —— 需要时再取）"
            _SCHEMA_CACHE[key] = text
            return text
        finally:
            conn.close()

    def sql_describe_table(table: str) -> str:
        """看某张表的字段（类型、可空、默认值、注释）。

        Args:
            table: 表名（先用 sql_list_tables 或 sql_schema_overview 查）。
        """
        key = f"describe:{table}"
        if key in _SCHEMA_CACHE:
            return _SCHEMA_CACHE[key]
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("show full columns from `%s`" % table.replace("`", ""))
            rows = cur.fetchall()
            lines = ["字段 | 类型 | 可空 | 默认 | 注释"]
            for r in rows:
                lines.append(
                    f"{r.get('Field')} | {r.get('Type')} | {r.get('Null')} | "
                    f"{r.get('Default')} | {r.get('Comment') or ''}"
                )
            text = "\n".join(lines)
            _SCHEMA_CACHE[key] = text
            return text
        finally:
            conn.close()

    def sql_sample_rows(table: str, n: int = 3) -> str:
        """抽样看几行真实数据（判断字段取值形态，比如枚举/编码/日期格式）。

        Args:
            table: 表名。
            n: 行数，默认 3，最多 10。
        """
        n = max(1, min(int(n or 3), 10))
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("select * from `%s` limit %d" % (table.replace("`", ""), n))
            return _fmt(cur.fetchall())
        finally:
            conn.close()

    def sql_query(sql: str) -> str:
        """执行一条只读 SELECT 查询并返回结果表。

        - 只允许 SELECT / WITH，单条语句
        - 没写 LIMIT 会自动加 LIMIT 200
        - 报错会把 MySQL 的原始错误返回给你，据此修改后重试

        Args:
            sql: 要执行的 SQL。
        """
        started = time.time()
        try:
            safe = validate_sql(sql, allow_tables)
        except ValueError as e:
            return f"[SQL 被拒绝] {e}"

        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(f"SET SESSION max_execution_time={QUERY_TIMEOUT_MS}")
            cur.execute(safe)
            rows = cur.fetchall()
            body = _fmt(rows)
            return f"{body}\n\n({len(rows)} 行，{time.time() - started:.2f}s)"
        except Exception as e:  # noqa: BLE001 —— 错误原文要给模型看，方便自我修正
            msg = f"[SQL 执行失败] {e.__class__.__name__}: {e}"
            hint = _column_hint(str(e))
            if hint:
                msg += f"\n\n[可用列提示] {hint}"
            return msg
        finally:
            conn.close()

    tools = [sql_list_tables, sql_schema_overview, sql_describe_table,
             sql_sample_rows, sql_query]
    for fn in tools:
        fn.name = fn.__name__
        fn.description = (fn.__doc__ or "").strip()
    return tools


def create_readonly_user(host: str, port: int, user: str, password: str,
                         db: str = "motorparts_db",
                         ro_user: str = "agent_ro", ro_pass: str = "agent_ro_2026") -> str:
    """建一个只读账号（SELECT only）。应用层校验被绕过时的最后一道防线。

    需要当前账号有 GRANT 权限；失败会抛异常，由调用方决定是否忽略。
    """
    import pymysql

    conn = pymysql.connect(host=host, port=port, user=user, password=password,
                           connect_timeout=8)
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s", (ro_user, ro_pass)
        )
        cur.execute("GRANT SELECT ON `%s`.* TO %%s@'%%'" % db % ro_user)
        cur.execute("FLUSH PRIVILEGES")
        return f"{ro_user} 已具备 {db} 的只读权限"
    finally:
        conn.close()
