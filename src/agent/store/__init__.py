"""Agent 侧存储实现包。

- `mysql_store.MySQLStore`：LangGraph BaseStore 的 MySQL 实现，承载**长期记忆**
  （用户偏好 + 持久化技能）。见 `docs/STORAGE_MIGRATION.md` Phase 1。

短期记忆（任务台账 / 展示消息）走 Redis，不在本包内。
"""

from agent.store.mysql_store import MySQLStore

__all__ = ["MySQLStore"]
