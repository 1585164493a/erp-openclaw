"""基于 MySQL 的 LangGraph BaseStore 实现（Agent 长期记忆持久化）。

================================================================================
为什么需要这个文件
================================================================================
`src/agent/config.py` 里原本是 `STORE = InMemoryStore()` —— 那是**进程内存字典**，
后端一重启，所有用户偏好（`/memories/{user_id}/preferences.md`）和所有已持久化技能
（`/persisted-skills/`）**全部归零**。也就是说"长期记忆"此前从未真正落库。

本文件把 BaseStore 的实现换到 MySQL，让长期记忆真正持久化。
配套：`db/migrations/008_agent_state_store.sql`、`docs/STORAGE_MIGRATION.md`。

================================================================================
只实现 batch / abatch 就够（这是关键设计点）
================================================================================
BaseStore 的抽象方法**只有 `batch` 和 `abatch`** 两个。其余方法都是基类的
默认实现，且**全部委托给 batch**（实测 `langgraph/store/base/__init__.py`）：

    aget(...)    -> await self.abatch([GetOp(...)])[0]
    aput(...)    -> await self.abatch([PutOp(...)])[0]
    asearch(...) -> await self.abatch([SearchOp(...)])[0]
    adelete(...) -> await self.abatch([...])[0]

所以**实现 batch + abatch = 同时得到 aget/aput/asearch/adelete**。
本项目的调用面正好只用到 `aget` / `aput` / `asearch`（见下），完全覆盖。

本项目对 STORE 的全部用法（已 grep 全仓确认）：
    middlewares/memory_update.py:241   await store.aget(namespace, key)
    middlewares/memory_update.py:264   await store.aput(namespace, key, file_value)
    middlewares/user_skills_restore.py:63  await store.asearch(self.namespace)
    tools/assign_skill.py:135          await store.aput(...)

没有 `aindex()` 调用、没有向量检索、没有复杂 filter —— 这也是选 MySQL
而非 MongoDB 的核心依据（Mongo 的文档/检索优势在此场景用不上）。

================================================================================
namespace 元组怎么落进 SQL
================================================================================
BaseStore 的 namespace 是 `tuple[str, ...]`（如 `("user_001",)`、`("skills",)`），
SQL 里拍平成以 `/` 连接的字符串，取回时再 split 回元组。

注意：`str.split("/")` 对空字符串会返回 `[""]`，所以空 namespace 要特判，
否则会得到 `("",)` 而不是 `()`。
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 命名空间编解码
# ---------------------------------------------------------------------------


def _ns_to_str(namespace: tuple[str, ...]) -> str:
    """namespace 元组 → SQL 字符串。`()` → `""`。"""
    return "/".join(namespace)


def _str_to_ns(value: str) -> tuple[str, ...]:
    """SQL 字符串 → namespace 元组。

    空字符串必须特判：`"".split("/")` 得到 `[""]`，会变成 `("",)`，
    而正确的空命名空间是 `()`。
    """
    if not value:
        return ()
    return tuple(value.split("/"))


def _now() -> datetime:
    """带时区的当前时间。

    MySQL 的 DATETIME 不存时区，写入时我们统一按 UTC 落库、读出时补上 UTC，
    避免"服务器换了时区，记忆时间就漂了"。
    """
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime:
    """把从库里读出的 datetime 补成带 UTC 时区。

    MySQL 返回的是朴素 datetime（无 tzinfo）。BaseStore 接受 datetime，
    但我们补上 tz 以免后续比较时 naive/aware 混用报错。
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    # 兜底：字符串（例如手工插入的数据）
    return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# MySQLStore
# ---------------------------------------------------------------------------


class MySQLStore(BaseStore):
    """LangGraph BaseStore 的 MySQL 实现。

    并发模型：PyMySQL 的 `Connection` **不是线程安全的**，而 `abatch` 会通过
    `asyncio.to_thread` 在工作线程里跑同步 SQL。所以这里用 `threading.local()`
    给每个线程独立连接 —— 比"单连接 + 全局锁"吞吐好，也不需要引入连接池依赖。

    参数：
        host/port/user/password/database: MySQL 连接信息（默认参考 erp_api/db.py 的口径，
            但 database 指向独立的 `agent_state`，不混进业务库 `motorparts_db`）。
        table: 表名，默认 `langgraph_store`。
        connect_timeout: 建连超时（秒）。保持短超时，让"依赖挂了"表现为
            可读诊断而不是长时间卡死（同 `MONGODB_TIMEOUT_MS` 的教训）。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        database: str = "agent_state",
        table: str = "langgraph_store",
        connect_timeout: int = 5,
        charset: str = "utf8mb4",
    ) -> None:
        # 不带索引能力，声明为 False（BaseStore 用它与 ttl_config 判断是否要做向量检索）
        self.index: Any = False
        self.ttl_config = None

        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self._table = table
        self._connect_timeout = connect_timeout
        self._charset = charset

        # 每个线程一个连接（PyMySQL Connection 非线程安全）
        self._local = threading.local()
        # 建库/建表只做一次
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    # ---- 连接管理 ----

    def _conn(self):
        """取本线程的连接；不存在或已断开则重建。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.ping(reconnect=True)
                return conn
            except Exception:
                # 连接坏了，丢弃重建
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None

        import pymysql

        conn = pymysql.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            database=self._database,
            charset=self._charset,
            connect_timeout=self._connect_timeout,
            autocommit=True,  # 单条读写，不需要显式事务
        )
        self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        """确保库表存在（幂等）。

        为什么放在这里而不是只靠 migration 脚本：代码应当能在**没手工跑过脚本**的
        环境里自我修复，否则"忘了执行 migration"会表现为难以定位的运行时错误。
        主路径仍然是 `db/migrations/008_agent_state_store.sql`。
        """
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            import pymysql

            # 先不带 database 连，确保库存在
            conn = pymysql.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                charset=self._charset,
                connect_timeout=self._connect_timeout,
                autocommit=True,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE DATABASE IF NOT EXISTS `{self._database}` "
                        "DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_0900_ai_ci"
                    )
                    cur.execute(f"USE `{self._database}`")
                    cur.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS `{self._table}` (
                          `namespace`  varchar(255) NOT NULL,
                          `key`        varchar(512) NOT NULL,
                          `value`      json         NOT NULL,
                          `created_at` datetime(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                          `updated_at` datetime(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                                    ON UPDATE CURRENT_TIMESTAMP(6),
                          `expires_at` datetime(6)  NULL DEFAULT NULL,
                          PRIMARY KEY (`namespace`, `key`) USING BTREE,
                          INDEX `idx_namespace` (`namespace`) USING BTREE,
                          INDEX `idx_expires` (`expires_at`) USING BTREE
                        ) ENGINE = InnoDB CHARACTER SET = utf8mb4
                          COLLATE = utf8mb4_0900_ai_ci
                        """
                    )
            finally:
                conn.close()
            self._schema_ready = True

    # ---- 内部：单个操作 ----

    def _do_get(self, op: GetOp) -> Optional[Item]:
        self._ensure_schema()
        ns = _ns_to_str(op.namespace)
        sql = (
            f"SELECT `value`, `created_at`, `updated_at` FROM `{self._table}` "
            "WHERE `namespace` = %s AND `key` = %s "
            "AND (`expires_at` IS NULL OR `expires_at` > UTC_TIMESTAMP(6))"
        )
        with self._conn().cursor() as cur:
            cur.execute(sql, (ns, op.key))
            row = cur.fetchone()
        if row is None:
            return None
        value, created_at, updated_at = row
        return Item(
            namespace=op.namespace,
            key=op.key,
            value=self._load_json(value),
            created_at=_as_utc(created_at),
            updated_at=_as_utc(updated_at),
        )

    def _do_put(self, op: PutOp) -> None:
        self._ensure_schema()
        ns = _ns_to_str(op.namespace)

        # value=None 表示删除该键（BaseStore 的约定）
        if op.value is None:
            self._do_delete(op.namespace, op.key)
            return

        expires_at = None
        if op.ttl is not None:
            expires_at = _now() + timedelta(seconds=float(op.ttl))

        payload = json.dumps(op.value, ensure_ascii=False)
        sql = (
            f"INSERT INTO `{self._table}` (`namespace`, `key`, `value`, `expires_at`) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE `value` = VALUES(`value`), "
            "`expires_at` = VALUES(`expires_at`), `updated_at` = CURRENT_TIMESTAMP(6)"
        )
        with self._conn().cursor() as cur:
            cur.execute(sql, (ns, op.key, payload, expires_at))

    def _do_delete(self, namespace: tuple[str, ...], key: str) -> None:
        self._ensure_schema()
        sql = f"DELETE FROM `{self._table}` WHERE `namespace` = %s AND `key` = %s"
        with self._conn().cursor() as cur:
            cur.execute(sql, (_ns_to_str(namespace), key))

    def _do_search(self, op: SearchOp) -> list[SearchItem]:
        """前缀搜索。

        `asearch(namespace_prefix)` 的语义是"取该命名空间下的条目"。
        我们的用法是 `asearch(("skills",))`（拿全部持久化技能）和
        `asearch((user_id,))`，所以做**层级前缀匹配**：
        namespace 等于前缀本身，或以该前缀 + `/` 开头。

        注意：这里**不做向量检索**（`query` 参数忽略），因为本项目没有建索引、
        调用方也从不传 query。若将来要语义检索，需要额外引入 embedding 表。
        """
        self._ensure_schema()
        prefix = _ns_to_str(op.namespace_prefix)

        where = ["(`expires_at` IS NULL OR `expires_at` > UTC_TIMESTAMP(6))"]
        params: list[Any] = []
        if prefix:
            where.append("(`namespace` = %s OR `namespace` LIKE %s)")
            params.extend([prefix, prefix + "/%"])
        if op.filter:
            # 结构化过滤：JSON_CONTAINS 支持 {"字段": 值} 形式的精确匹配
            where.append("JSON_CONTAINS(`value`, %s)")
            params.append(json.dumps(op.filter, ensure_ascii=False))

        sql = (
            f"SELECT `namespace`, `key`, `value`, `created_at`, `updated_at` "
            f"FROM `{self._table}` WHERE {' AND '.join(where)} "
            "ORDER BY `updated_at` DESC, `key` ASC LIMIT %s OFFSET %s"
        )
        params.extend([int(op.limit), int(op.offset)])

        with self._conn().cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        results: list[SearchItem] = []
        for ns_raw, key, value, created_at, updated_at in rows:
            results.append(
                SearchItem(
                    namespace=_str_to_ns(ns_raw),
                    key=key,
                    value=self._load_json(value),
                    created_at=_as_utc(created_at),
                    updated_at=_as_utc(updated_at),
                    score=None,
                )
            )
        return results

    def _do_list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        """列出命名空间。

        本项目未使用该方法（`StoreBackend` 只调 `aget` / `aput`），
        但 BaseStore 的默认 `list_namespaces` 依赖 `batch`，所以一并支持，
        避免将来有人调用时直接抛错。
        """
        self._ensure_schema()
        sql = (
            f"SELECT DISTINCT `namespace` FROM `{self._table}` "
            "ORDER BY `namespace` ASC LIMIT %s OFFSET %s"
        )
        with self._conn().cursor() as cur:
            cur.execute(sql, (int(op.limit), int(op.offset)))
            rows = cur.fetchall()
        namespaces = [_str_to_ns(r[0]) for r in rows]
        if op.max_depth is not None:
            namespaces = [ns[: op.max_depth] for ns in namespaces]
        return sorted(set(namespaces))

    @staticmethod
    def _load_json(raw: Any) -> dict[str, Any]:
        """把库里的 JSON 列解析成 dict。

        PyMySQL 对 JSON 列可能返回 str，也可能返回已解析对象（取决于版本/驱动行为），
        两种都要能吃下。
        """
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
        raise TypeError(f"无法解析 store 的 value 列: {type(raw)!r}")

    # ---- batch（BaseStore 的唯一抽象面）----

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        """同步批量执行。按输入顺序返回结果。"""
        results: list[Result] = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self._do_get(op))
                continue
            if isinstance(op, PutOp):
                self._do_put(op)
                results.append(None)
                continue
            if isinstance(op, SearchOp):
                results.append(self._do_search(op))
                continue
            if isinstance(op, ListNamespacesOp):
                results.append(self._do_list_namespaces(op))
                continue
            # 未知操作类型**明确报错**，而不是静默返回 None ——
            # 静默会让上游拿到 None 后继续跑，错误被推迟到难以定位的地方。
            raise NotImplementedError(
                f"MySQLStore 不支持该操作类型: {type(op).__name__}"
            )
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """异步批量执行。

        PyMySQL 是同步驱动，所以丢到线程池跑（`asyncio.to_thread`），
        避免阻塞事件循环。`aget`/`aput`/`asearch` 都走这里。
        """
        return await asyncio.to_thread(self.batch, list(ops))

    # ---- 便利方法（不属于 BaseStore 接口，供运维/自检用）----

    def healthcheck(self) -> dict[str, Any]:
        """连通性与表状态自检，供启动期诊断使用。"""
        self._ensure_schema()
        with self._conn().cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM `{self._table}`"
            )
            count = cur.fetchone()[0]
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
        return {
            "ok": True,
            "mysql_version": version,
            "database": self._database,
            "table": self._table,
            "rows": count,
        }

    def close(self) -> None:
        """关闭本线程连接（进程退出时清理用）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
