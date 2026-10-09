"""
沙箱管理器 — Per-User 沙箱生命周期管理 + 预热机制。

每个用户维护一个独立的 OpenSandbox，同一个用户的多条对话线程共享。
启动时预热一个沙箱，第一个用户连接时直接认领，无需等待创建。

沙箱 ID 通过 MongoDB 持久化，重启后可重连到已有沙箱。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pymongo import MongoClient

from agent.backends.sandbox_proxy import SandboxBackendProxy
from agent.config import SANDBOX_CONFIG

logger = logging.getLogger(__name__)

SANDBOX_COLLECTION = "sandbox_registry"

# ---- 全局状态 ----

SANDBOX_BACKENDS: dict[str, SandboxBackendProxy] = {}

_warm_reserve: SandboxBackendProxy | None = None
_warm_lock = asyncio.Lock()

_mongo_client: MongoClient | None = None
_collection = None


def _sandbox_collection():
    if _mongo_client is None:
        raise RuntimeError("沙箱管理器未初始化，请先调用 initialize()")
    from agent.config import MONGODB_DB_NAME
    return _mongo_client[MONGODB_DB_NAME][SANDBOX_COLLECTION]


# ---- 公开 API ----


async def initialize(mongo_client: MongoClient) -> None:
    """启动时初始化 MongoDB 连接。"""
    global _mongo_client, _collection
    _mongo_client = mongo_client
    _collection = _sandbox_collection()
    _collection.create_index("user_id", unique=True)
    logger.info("沙箱管理器已初始化")


async def pre_warm() -> None:
    """启动时预热一个沙箱，供新用户零等待认领。失败不阻塞启动。

    但如果注册表里已经有沙箱绑定，就跳过预热 ——
    说明上次留下的沙箱还没过期，重启时应优先复用它们（复用路径已排在认领之前）。
    此时再额外建一个备用沙箱纯属浪费：每建一个都要重跑 venv + 装 5 个包 ≈235s。

    这里只是「不建」，**不会**去认领别人名下的沙箱 ——
    预热沙箱是无主的，而注册表里的沙箱属于具体用户，混用会破坏隔离。
    """
    global _warm_reserve

    try:
        existing = _sandbox_collection().find_one({})
    except Exception:
        existing = None
        logger.warning("读取沙箱注册表失败，仍按原逻辑预热", exc_info=True)

    if existing and existing.get("sandbox_id"):
        logger.info(
            "注册表已有沙箱绑定（user_id=%s），跳过预热，下次请求走复用路径",
            existing.get("user_id"),
        )
        return

    logger.info("注册表为空（首次运行或沙箱均已过期），开始预热沙箱...")
    try:
        from agent.backends.sandbox_setup import setup_sandbox
        sandbox_backend = await asyncio.to_thread(setup_sandbox, SANDBOX_CONFIG)
        _warm_reserve = SandboxBackendProxy(sandbox_backend)
        logger.info("预热沙箱就绪: %s", sandbox_backend.id)
    except Exception:
        logger.warning("预热沙箱失败，将在第一个用户连接时创建", exc_info=True)
        _warm_reserve = None


async def ensure_sandbox_for_user(user_id: str) -> SandboxBackendProxy:
    """
    获取或创建某个用户的沙箱（五态生命周期）。

    1. 内存缓存命中 → ping 健康检查 → 失败则重建
    2. 注册表有该用户的 sandbox_id → connect(id) → 失败则重建   ← 老用户复用自己的
    0. _warm_reserve 有货 → 认领分配给 user                    ← 只服务没有历史记录的新用户
    3. 无任何记录 → 创建新沙箱

    顺序说明：2 必须排在 0 之前。否则每次启动 pre_warm() 备好的新沙箱会被优先认领，
    老用户永远拿不到自己上次那个（venv 已就绪、sentinel 都在、≈18s 可用）。
    """
    # 状态 1: 内存缓存命中
    proxy = SANDBOX_BACKENDS.get(user_id)
    if proxy is not None:
        try:
            await asyncio.to_thread(proxy.execute, "echo ok")
            logger.info("用户 %s 命中沙箱缓存: %s", user_id, proxy.id)
            return proxy
        except Exception:
            logger.warning("用户 %s 的沙箱 %s 不可达，重建中...", user_id, proxy.id)
            return await _recreate_sandbox(user_id, proxy)

    # 状态 2: 查注册表重连
    #   ⚠️ 顺序关键：必须排在「认领预热沙箱」之前。
    #   pre_warm() 每次启动都会备一个新沙箱；若先认领预热沙箱，
    #   老用户也会被分到那个新的，自己上次的沙箱永远轮不到 —— 复用形同虚设。
    doc = _sandbox_collection().find_one({"user_id": user_id})
    if doc and doc.get("sandbox_id"):
        sandbox_id = doc["sandbox_id"]
        logger.info("用户 %s 尝试重连沙箱: %s", user_id, sandbox_id)
        try:
            from agent.backends.sandbox_setup import setup_sandbox
            sandbox_backend = await asyncio.to_thread(
                setup_sandbox, SANDBOX_CONFIG, sandbox_id=sandbox_id,
            )
        except Exception:
            logger.warning("重连沙箱 %s 失败，将创建新沙箱", sandbox_id)
            return await _create_sandbox_for_user(user_id)

        try:
            await asyncio.to_thread(sandbox_backend.execute, "echo ok")
        except Exception:
            logger.warning("已连接的沙箱 %s 不可达，创建新沙箱", sandbox_id)
            return await _recreate_sandbox(user_id, None)

        proxy = SandboxBackendProxy(sandbox_backend)
        SANDBOX_BACKENDS[user_id] = proxy
        # ⚠️ 2026-09-29 修复：这里原来只更新 updated_at，【不写回 sandbox_id】。
        #
        #   为什么必须要写：setup_sandbox() 在"连接已有沙箱失败"时会
        #   **自己 catch 掉并静默新建一个**（见 sandbox_setup.py:43-44），
        #   然后正常返回。于是上面那个
        #       except: return await _create_sandbox_for_user(...)
        #   分支【永远走不到】，而成功路径只更新 updated_at ——
        #   注册表就一直指向那个已经过期销毁的旧沙箱。
        #
        #   后果：每次重启都重连失败 → 又静默新建（约 235s），
        #         注册表永远不更新，问题反复出现。
        #   修法：成功后一律把当前真实 id 写回，注册表自然自愈。
        _sandbox_collection().update_one(
            {"user_id": user_id},
            {"$set": {
                "sandbox_id": sandbox_backend.id,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        return proxy

    # 状态 0: 认领预热沙箱（留给没有历史记录的新用户）
    async with _warm_lock:
        global _warm_reserve
        if _warm_reserve is not None:
            proxy = _warm_reserve
            _warm_reserve = None
            sandbox_id = proxy.id

            SANDBOX_BACKENDS[user_id] = proxy
            _sandbox_collection().update_one(
                {"user_id": user_id},
                {"$set": {
                    "sandbox_id": sandbox_id,
                    "updated_at": datetime.now(timezone.utc),
                    "created_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            logger.info("用户 %s 认领了预热沙箱: %s", user_id, sandbox_id)
            asyncio.create_task(_replenish_warm())
            return proxy

    # 状态 3: 无记录，创建新沙箱
    return await _create_sandbox_for_user(user_id)


async def ping_user_sandbox(user_id: str) -> bool:
    """公开的沙箱健康检查，供中间件调用。"""
    proxy = SANDBOX_BACKENDS.get(user_id)
    if proxy is None:
        return False
    try:
        await asyncio.to_thread(proxy.execute, "echo ok")
        return True
    except Exception:
        return False


async def recreate_user_sandbox(user_id: str) -> SandboxBackendProxy:
    """
    公开的沙箱重建方法，供 SandboxHealthMiddleware 在检测到沙箱不可用时调用。

    创建新沙箱（含 skills 播种 + venv），替换 proxy 内的 backend，
    删除旧沙箱，更新 MongoDB 绑定。不包含 AGENTS.md 上传，由调用方负责。
    """
    proxy = SANDBOX_BACKENDS.get(user_id)
    return await _recreate_sandbox(user_id, proxy)


async def cleanup_user(user_id: str) -> None:
    """销毁某用户的沙箱。"""
    proxy = SANDBOX_BACKENDS.pop(user_id, None)
    if proxy is not None:
        try:
            from opensandbox import SandboxSync
            await asyncio.to_thread(SandboxSync.delete, proxy.id)
        except Exception:
            logger.warning("删除沙箱 %s 失败", proxy.id, exc_info=True)

    _sandbox_collection().delete_one({"user_id": user_id})
    logger.info("用户 %s 沙箱已清理", user_id)


async def shutdown() -> None:
    """关闭沙箱管理器。

    **刻意不删除沙箱，也不清 sandbox_registry：**
      - 沙箱自带 2 小时超时，由 OpenSandbox 平台自动回收，不需要我们手动销毁；
      - 保留沙箱 + 保留注册表记录，下次启动才能走「查注册表重连」的复用路径
        （复用 ≈18s；新建要重跑 venv 并重装 5 个包 ≈235s）。

    原实现会在关闭时删掉预热沙箱、并对每个用户调 cleanup_user()（连带删除注册表记录），
    结果是：复用的线索被自己抹掉 → 每次重启都退化成全新建 → 启动必等 4 分钟。
    VM 上堆的那一堆 Running 孤儿沙箱也是这么来的（删远端失败、但记录已删，再也找不回）。

    需要显式销毁某个用户的沙箱时，仍可单独调用 cleanup_user()。
    """
    global _warm_reserve, _mongo_client
    logger.info("正在关闭沙箱管理器（保留沙箱供下次复用）...")

    # 只释放进程内引用，不碰远端资源
    _warm_reserve = None
    SANDBOX_BACKENDS.clear()

    if _mongo_client is not None:
        _mongo_client.close()
        _mongo_client = None

    logger.info("沙箱管理器已关闭（沙箱保留，平台 2h 后自动回收）")


# ---- 内部函数 ----


async def _create_sandbox_for_user(user_id: str) -> SandboxBackendProxy:
    """创建新沙箱并绑定到用户。"""
    from agent.backends.sandbox_setup import setup_sandbox

    logger.info("为用户 %s 创建新沙箱...", user_id)
    sandbox_backend = await asyncio.to_thread(setup_sandbox, SANDBOX_CONFIG)
    sandbox_id = sandbox_backend.id

    proxy = SandboxBackendProxy(sandbox_backend)
    SANDBOX_BACKENDS[user_id] = proxy

    _sandbox_collection().update_one(
        {"user_id": user_id},
        {"$set": {
            "sandbox_id": sandbox_id,
            "updated_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )

    logger.info("用户 %s 沙箱创建完成: %s", user_id, sandbox_id)
    return proxy


async def _recreate_sandbox(
    user_id: str, existing_proxy: SandboxBackendProxy | None,
) -> SandboxBackendProxy:
    """重建沙箱（原沙箱不可达时）。"""
    from agent.backends.sandbox_setup import setup_sandbox

    logger.info("为用户 %s 重建沙箱...", user_id)
    sandbox_backend = await asyncio.to_thread(setup_sandbox, SANDBOX_CONFIG)
    sandbox_id = sandbox_backend.id

    if existing_proxy is not None:
        existing_proxy.replace_backend(sandbox_backend)
    else:
        existing_proxy = SandboxBackendProxy(sandbox_backend)
        SANDBOX_BACKENDS[user_id] = existing_proxy

    # 尝试删除旧沙箱
    old_doc = _sandbox_collection().find_one({"user_id": user_id})
    if old_doc and old_doc.get("sandbox_id"):
        old_id = old_doc["sandbox_id"]
        try:
            from opensandbox import SandboxSync
            await asyncio.to_thread(SandboxSync.delete, old_id)
        except Exception:
            pass

    _sandbox_collection().update_one(
        {"user_id": user_id},
        {"$set": {
            "sandbox_id": sandbox_id,
            "updated_at": datetime.now(timezone.utc),
        }},
    )

    logger.info("用户 %s 沙箱重建完成: %s", user_id, sandbox_id)
    return existing_proxy


async def _replenish_warm() -> None:
    """后台补充预热沙箱（被认领后异步触发）。"""
    global _warm_reserve
    if _warm_reserve is not None:
        return

    try:
        from agent.backends.sandbox_setup import setup_sandbox
        sandbox_backend = await asyncio.to_thread(setup_sandbox, SANDBOX_CONFIG)
        async with _warm_lock:
            if _warm_reserve is None:
                _warm_reserve = SandboxBackendProxy(sandbox_backend)
                logger.info("后台补充预热沙箱就绪: %s", sandbox_backend.id)
    except Exception:
        logger.warning("后台补充预热沙箱失败", exc_info=True)
