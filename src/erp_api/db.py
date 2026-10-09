"""ERP API 数据访问层：直连本地 MySQL motorparts_db。"""
import os
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor

# ⚠️ 2026-09-29 修复：这个进程必须【自己】加载 .env
#
#   问题现象：前端问销量 → `/api/products/search` 返回 500，
#             日志里是 pymysql 的 `socket.create_connection` 失败（连接被拒）。
#
#   根因：全项目的 load_dotenv() 只在 `agent/env_utils.py` 里那一处。
#        Agent 后端（8090）会 import agent.*，所以它能读到 .env；
#        但 **ERP API 是独立进程**（`uvicorn erp_api.main:app`），
#        它从不 import agent 包 → **.env 从来没被加载** →
#        MYSQL_HOST 回退成默认的 127.0.0.1，而 MySQL 实际在 VM（127.0.0.1）上。
#
#   修法：显式加载【项目根】的 .env（不依赖 cwd，也不依赖别的包的 import 副作用）。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")

# 连接参数支持环境变量覆盖（与 motorparts_db.sql 导入目标一致）
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "123456")
MYSQL_DB = os.getenv("MYSQL_DB", "motorparts_db")


def _connect() -> pymysql.Connection:
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
    )


class Connection:
    """上下文管理器：成功自动提交，异常自动回滚并关闭连接。"""

    def __enter__(self) -> pymysql.cursors.DictCursor:
        self._conn = _connect()
        self._cur = self._conn.cursor()
        return self._cur

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._cur.close()
            self._conn.close()
        return False
