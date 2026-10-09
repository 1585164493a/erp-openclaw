import os
from datetime import timedelta
from pathlib import Path
from typing import Optional

import httpx
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.mongodb import MongoDBSaver
from opensandbox.config import ConnectionConfigSync
from pymongo import MongoClient

from agent.env_utils import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
from agent.store.mysql_store import MySQLStore

# ---------- 模型配置 ----------
# 模型名走环境变量，方便按成本来回切，不用改代码：
#   deepseek-v4-flash  便宜（当前默认）
#   deepseek-v4-pro    贵、更强
# 注意：6 个子 Agent 的 YAML 里 model 字段全部是注释状态 → 它们继承主 Agent 模型，
# 所以改这一处就等于整体切换（摘要模型本来就用 flash）。
MAIN_MODEL_NAME = os.getenv("MAIN_MODEL_NAME", "deepseek-v4-flash")
SUMMARY_MODEL_NAME = os.getenv("SUMMARY_MODEL_NAME", "deepseek-v4-flash")

# 主 Agent 模型
MAIN_MODEL = ChatOpenAI(
    model=MAIN_MODEL_NAME,
    temperature=1.1,
    openai_api_key=DEEPSEEK_API_KEY,
    openai_api_base=DEEPSEEK_BASE_URL,
    max_tokens=2560000,
    model_kwargs={
        "extra_body": {
            "thinking": {"type": "disabled"}
        }
    }
)
# 摘要专用模型（摘要需要稳定输出，temperature 设为较低值）
SUMMARY_MODEL = ChatOpenAI(
    model=SUMMARY_MODEL_NAME,
    temperature=0.3,
    openai_api_key=DEEPSEEK_API_KEY,
    openai_api_base=DEEPSEEK_BASE_URL,
    max_tokens=2560000,
    model_kwargs={
        "extra_body": {
            "thinking": {"type": "disabled"}
        }
    }
)

# ---------- 沙箱配置 ----------
# OpenSandbox 沙箱配置连接
SANDBOX_CONFIG = ConnectionConfigSync(
    # 沙箱控制面地址可用环境变量覆盖（VM 走 DHCP，IP 会在 127.0.0.1/130 之间变）。
    # 不设该变量时行为与原来完全一致。
    domain=os.getenv("OPENSANDBOX_DOMAIN", "http://127.0.0.1:8080"),
    use_server_proxy=True,
    request_timeout=timedelta(seconds=60),
    transport=httpx.HTTPTransport(limits=httpx.Limits(max_connections=20)),
)

# ---------- 路径常量 ----------
EXAMPLE_DIR = Path(__file__).parent.parent
print(f'当前代码执行的工作目录为：{EXAMPLE_DIR}')
# 沙箱内技能根路径
SANDBOX_SKILLS_ROOT = "/skills"
# 沙箱内记忆根路径（用户私有记忆存放处）
SANDBOX_MEMORIES_ROOT = "/memories"
# 沙箱内分析中间文件存放目录
SANDBOX_ANALYSIS_ROOT = "/analysis"
# 沙箱内数据文件存放目录
SANDBOX_DATA_ROOT = "/data"
# 本地技能资源目录（项目内的路径，相对于项目根）
LOCAL_SKILLS_DIR = EXAMPLE_DIR / "skills"
# 本地下载目录（从沙箱下载文件的目标路径）
DOWNLOAD_DIR = EXAMPLE_DIR / "download"
# 本地子 Agent 配置目录
LOCAL_SUBAGENT_CONFIG_DIR = EXAMPLE_DIR / "agent/subagents"
# 本地的Agent记忆文件
LOCAL_AGENTS_MD = EXAMPLE_DIR / "agent/memory/AGENTS.md"

# ---------- 文件名常量 ----------
# 主 Agent 只读指引文件（上传到沙箱 /AGENTS.md）
AGENTS_MD_FILENAME = "/AGENTS.md"
# 用户偏好文件名（在 /memories/{user_id}/ 下）
USER_PREFERENCES_FILENAME = "preferences.md"

# ---------- 用户技能持久化 ----------
# 技能 StoreBackend 命名空间（按 Agent scope 组织，无用户隔离）。
#
# ⚠️ 这个常量曾被注释掉，但 main_agent.py 有 3 处 import/使用它
#    （StoreBackend 的 namespace、assign_skill 的 skills_namespace、
#      UserSkillsRestoreMiddleware 的第二个参数）—— 于是 `import agent.main_agent`
#    必然抛 ImportError。此前它被模块顶层的 Mongo 连接错误挡在后面，没暴露出来。
SKILLS_STORE_NAMESPACE = ("skills",)

# 技能持久化 StoreBackend 路由路径（当前无引用，保留待用）
# PERSISTED_SKILLS_ROOT = "/persisted-skills"

# ---------- 中间件参数 ----------

# ---------- MongoDB 配置（用于持久化 Agent 短期记忆/checkpoint） ----------
# 连接串走环境变量，避免明文凭据写死在代码里（见 REFACTOR_PLAN 的 A3）
MONGODB_URI = os.getenv(
    "MONGODB_URI",
    "mongodb://root:123456@127.0.0.1:27017/?authSource=admin",
)
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "langchain_db")
MONGODB_CHECKPOINT_COLLECTION = os.getenv("MONGODB_CHECKPOINT_COLLECTION", "checkpoints")

# 选主超时。驱动默认 30s，会把「依赖挂了」表现成"启动卡半分钟 + 一段不可读的栈"；
# 3s 足够覆盖正常网络抖动，又能让启动期快速失败并给出可读诊断。
MONGODB_TIMEOUT_MS = int(os.getenv("MONGODB_TIMEOUT_MS", "3000"))

# ---------- 长期记忆持久化（MySQL）----------
# 为什么从 InMemoryStore 换成 MySQL（这是一个真 bug 的修复，不是重构偏好）：
#   `InMemoryStore()` 是**进程内存字典**。用户偏好（/memories/{user_id}/preferences.md）
#   和已持久化技能（/persisted-skills/）全都只活在当前进程里 ——
#   后端一重启就**全部归零**。也就是说"长期记忆"此前从未真正落库。
#
# 为什么长期记忆选 MySQL 而不是 Redis：
#   判据是**数据的可重建性**。用户偏好量小、变化慢、**丢了用户立刻能感知**
#   （下次对话个性化就没了），而且要能单独备份/审计。Redis 是内存数据库、
#   容量受物理内存限制、原生没有 binlog 这类可审计的变更日志 ——
#   Redis 只放会话状态（任务台账/展示消息），那部分丢了可以重建。
#   完整论证见 docs/STORAGE_MIGRATION.md §0。
#
# 为什么单独一个库（agent_state）而不是塞进业务库 motorparts_db：
#   访问模式不同（业务表按条件查 / 本表按 namespace+key 取整篇正文）、
#   权限边界不同、备份粒度不同。同实例、异库。
AGENT_STATE_DB = os.getenv("AGENT_STATE_DB", "agent_state")
AGENT_STORE_TABLE = os.getenv("AGENT_STORE_TABLE", "langgraph_store")
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "123456")

# ----------------------------------------------------------------
# 长期记忆 Store —— 懒加载（与 get_checkpointer 同一模式）
# ----------------------------------------------------------------
# 与 A2 那个坑同一个道理：依赖连接的构造**不要写在模块顶层**。
# 否则 MySQL 不可达时 `import agent.config` 会直接抛异常，
# 服务根本走不到启动期的配置校验网，而且报错不可读。
# 懒加载后：启动期只依赖配置本身，依赖故障推迟到真正需要它的那一刻。
_store: Optional[MySQLStore] = None


def get_store() -> MySQLStore:
    """懒加载并复用长期记忆 Store（进程内单例）。

    承载 /memories/（用户偏好）与 /persisted-skills/（持久化技能）两处路由。

    Raises:
        pymysql.err.OperationalError: MySQL 不可达（超过 connect_timeout）
    """
    global _store
    if _store is None:
        _store = MySQLStore(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=AGENT_STATE_DB,
            table=AGENT_STORE_TABLE,
        )
    return _store

# ----------------------------------------------------------------
# MongoDB checkpointer —— 懒加载（REFACTOR_PLAN A2）
# ----------------------------------------------------------------
# 为什么必须懒加载（实测结论，不是推测）：
#   - `MongoClient(uri)` 构造**不会**连库，0.2s 就返回；
#   - 真正触发连接的是 **`MongoDBSaver(...)` 的构造函数**。
# 原先两者都写在模块顶层，于是 VM 不可达时表现为 `import agent.config` 直接抛
# ServerSelectionTimeoutError（实测 32.6s 后失败）—— 服务根本起不来，
# **也就走不到启动期的配置校验网**，而且报错完全不可读。
# 改成首次使用时才构造之后，启动期只依赖配置本身；
# 依赖故障被推迟到真正需要 checkpointer 的那一刻（即第一个对话请求）。
_mongodb_client: Optional[MongoClient] = None
_checkpointer: Optional[MongoDBSaver] = None


def get_mongo_client() -> MongoClient:
    """懒加载并复用 MongoClient（进程内单例）。"""
    global _mongodb_client
    if _mongodb_client is None:
        _mongodb_client = MongoClient(
            MONGODB_URI, serverSelectionTimeoutMS=MONGODB_TIMEOUT_MS
        )
    return _mongodb_client


def get_checkpointer() -> MongoDBSaver:
    """懒加载并复用 checkpointer（进程内单例）。

    支撑 Human-in-the-Loop（interrupt 状态持久化）与跨重启对话恢复。

    Raises:
        ServerSelectionTimeoutError: MongoDB 不可达（超过 MONGODB_TIMEOUT_MS）
    """
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = MongoDBSaver(
            client=get_mongo_client(),
            db_name=MONGODB_DB_NAME,
            checkpoint_collection_name=MONGODB_CHECKPOINT_COLLECTION,
        )
    return _checkpointer

# ---------- 第三方框架补丁 ----------
# deepagents 的 read_file 默认只读 100 行（DEFAULT_READ_LIMIT），
# 且超限时【静默切掉】不告知模型 —— 导致 SKILL.md 尾部无声丢失、
# 模型也不知道该分页补读。这里把行数闸门让位给 token 闸门
# （tool_token_limit_before_evict 按真实成本算，且截断时会追加提示）。
#
# 必须在本模块导入期执行：默认参数在中间件构造时求值，晚于此就无效。


import deepagents.middleware.filesystem as _fs

if hasattr(_fs, "DEFAULT_READ_LIMIT"):
    _fs.DEFAULT_READ_LIMIT = 10 ** 6
    # print("[INFO] read_file 默认行数上限已放开（100 → 1000000），改由 token 上限兜底")
else:
    print(
        "[WARN] deepagents 已无 DEFAULT_READ_LIMIT，read_file 默认行数补丁失效 —— "
        "技能文件可能被静默截断到 100 行，请核对 deepagents 版本"
    )