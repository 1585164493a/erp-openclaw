"""
后端配置文件

MongoDB 配置的唯一来源是 agent/config.py —— 此处只做转发，避免两处硬编码漂移。
"""

from agent.config import (
    MONGODB_CHECKPOINT_COLLECTION,
    MONGODB_DB_NAME,
    MONGODB_URI,
)

__all__ = [
    "MONGODB_URI",
    "MONGODB_DB_NAME",
    "MONGODB_CHECKPOINT_COLLECTION",
    "API_TITLE",
    "API_VERSION",
    "API_DESCRIPTION",
]

# ============================================================
# 服务配置
# ============================================================
API_TITLE = "DeepAgent Chat API"
API_VERSION = "1.0.0"
API_DESCRIPTION = "基于 DeepAgent 的 AI 对话系统 API"
