"""Agent 版本信息：给 trace 打版本戳，方便"这条失败是哪个版本产生的"。

为什么需要（2026-11-12）：
- 线上 trace 会长期累积，但代码在不停改；
- 从历史 trace 挖回归题时，必须能区分"这条失败在最新版本上还会不会复现"；
- 只靠时间戳不行（同一天改了好几次）。

版本来源优先级：
1. 环境变量 AGENT_VERSION（发布时手工钉，最稳）
2. git 短 commit（工作区脏时加 -dirty 后缀）
3. 兜底 "dev"
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]      # ERP_OPENCLAW


def _git_commit() -> tuple[str, bool]:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(_ROOT), capture_output=True, text=True,
                             timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"],
                                    cwd=str(_ROOT), capture_output=True, text=True,
                                    timeout=5).stdout.strip())
        return sha or "", dirty
    except Exception:  # noqa: BLE001
        return "", False


@lru_cache(maxsize=1)
def version_info() -> dict:
    sha, dirty = _git_commit()
    explicit = os.getenv("AGENT_VERSION")
    if explicit:
        version = explicit
    elif sha:
        version = f"{sha}-dirty" if dirty else sha
    else:
        version = "dev"
    return {
        "agent_version": version,
        "git_commit": sha or "unknown",
        "git_dirty": dirty,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sandbox_image": os.getenv("OPENSANDBOX_IMAGE") or "default",
    }


def version_tag() -> str:
    """短标签，用于 trace tags 过滤（如 v:dd6ccd6）。"""
    return f"v:{version_info()['agent_version']}"
