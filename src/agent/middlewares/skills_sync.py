# src/middleware/skills_sync.py
"""
技能同步中间件

在每个 Agent 运行周期开始前，将本地 src/skills/ 下的技能文件与沙箱同步。
检测到变化时，向对话中插入系统通知，提醒 Agent 有新技能可用。
"""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from deepagents.backends.sandbox import BaseSandbox  # 使用通用的沙箱后端协议

from agent.config import LOCAL_SKILLS_DIR, SANDBOX_SKILLS_ROOT

# 同步状态标记放在**沙箱里**，而不是进程内存里。
# 原因（2026-11-11 实测）：中间件是每请求新建的，_last_hashes 永远为空，
# 于是每轮都要对 17 个技能文件各做 2 次沙箱往返（test -f + download），耗时 21.7 秒。
# 改成沙箱内标记后，每轮只需 1 次往返（cat）就能判定"无需同步"。
SYNC_MARKER = f"{SANDBOX_SKILLS_ROOT}/.synced-hash"

# 诊断用日志（一行一次同步，用来核对快路径是否真的生效）。
# 写系统临时目录：项目子目录在某些环境下写不进去（实测 logs/ 被拒）。
import tempfile as _tempfile

_LOG_PATH = Path(_tempfile.gettempdir()) / "skills_sync.log"


def _log(msg: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            import time as _t
            f.write(f"{_t.strftime('%H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def local_skills_fingerprint() -> Optional[str]:
    """本地整个技能目录的内容指纹（路径 + 内容，排序后 sha1）。

    沙箱播种和增量同步共用这一个指纹：播种时写进沙箱标记，
    之后每轮同步只要比对它，就知道"沙箱已是最新"。
    """
    root = Path(LOCAL_SKILLS_DIR)
    if not root.exists():
        return None
    h = hashlib.sha1()
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        h.update(f.relative_to(root).as_posix().encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()


class SkillsSyncMiddleware(AgentMiddleware):
    """技能文件同步中间件，依赖沙箱后端的文件操作能力。"""

    def __init__(self, backend: BaseSandbox) -> None:
        super().__init__()
        self.backend = backend
        # 缓存本地文件哈希，避免重复同步
        self._last_hashes: Dict[str, str] = {}

    # --------------------- 钩子 ---------------------
    def before_agent(self, state: Dict[str, Any], runtime: Any) -> Optional[Dict[str, Any]]:
        # 延迟沙箱：还没创建过就跳过同步 —— 否则每次请求都会为"同步技能"提前拉沙箱，
        # 纯问答（"你好"）也要等 45~90 秒。
        if hasattr(self.backend, "is_bound") and not self.backend.is_bound():
            return None
        new_skills = self._sync_files()
        if new_skills:
            return self._make_notification(new_skills)
        return None

    async def abefore_agent(self, state: Dict[str, Any], runtime: Any) -> Optional[Dict[str, Any]]:
        import asyncio
        if hasattr(self.backend, "is_bound") and not self.backend.is_bound():
            return None
        loop = asyncio.get_running_loop()
        new_skills = await loop.run_in_executor(None, self._sync_files)
        if new_skills:
            return self._make_notification(new_skills)
        return None

    # --------------------- 文件同步 ---------------------
    def _dir_hash(self, root: Path) -> str:
        """兼容旧调用：转调模块级指纹函数。"""
        return local_skills_fingerprint() or ""

    def _remote_marker(self) -> Optional[str]:
        """读沙箱里的同步标记（1 次往返，约 0.6 秒）。读不到返回 None。"""
        try:
            resp = self.backend.execute(f"cat {SYNC_MARKER} 2>/dev/null")
            if resp.exit_code == 0:
                return (resp.output or "").strip() or None
        except Exception:  # noqa: BLE001 —— 探测失败就走完整同步，别影响主流程
            pass
        return None

    def _write_marker(self, fingerprint: str) -> None:
        try:
            self.backend.upload_files([(SYNC_MARKER, fingerprint.encode("utf-8"))])
        except Exception:  # noqa: BLE001
            pass

    def _sync_files(self) -> List[str]:
        """扫描本地技能目录，将新增/修改的文件上传到沙箱。

        Returns:
            发生变化的技能名称列表。
        """
        local_skills_dir = Path(LOCAL_SKILLS_DIR)
        if not local_skills_dir.exists():
            return []

        # 快路径：沙箱里的标记和本地指纹一致 → 沙箱已是最新，直接返回。
        # 这是绝大多数情况（沙箱创建时已播种），耗时从 21.7 秒降到 0.6 秒。
        try:
            wanted = self._dir_hash(local_skills_dir)
        except Exception:  # noqa: BLE001
            wanted = None
        if wanted is not None and self._remote_marker() == wanted:
            _log(f"FAST-PATH hit  fp={wanted[:12]}")
            return []

        _log(f"FULL-SYNC start fp={(wanted or '')[:12]}")

        updated_skills: List[str] = []

        for skill_dir in local_skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_name = skill_dir.name
            sandbox_skill_dir = f"{SANDBOX_SKILLS_ROOT}/{skill_name}"

            files_to_upload: List[Tuple[str, bytes]] = []
            has_changes = False

            for local_file in skill_dir.rglob("*"):
                if not local_file.is_file():
                    continue
                relative_path = local_file.relative_to(skill_dir).as_posix()
                sandbox_path = f"{sandbox_skill_dir}/{relative_path}"

                with open(local_file, "rb") as f:
                    local_content = f.read()
                local_hash = hashlib.md5(local_content).hexdigest()
                cache_key = f"{skill_name}/{relative_path}"

                # 本地哈希未变，跳过
                if self._last_hashes.get(cache_key) == local_hash:
                    continue

                # 对比沙箱文件（先 test -f 避免 download_files 对 404 打 ERROR）
                check = self.backend.execute(f"test -f {sandbox_path}")
                if check.exit_code == 0:
                    try:
                        results = self.backend.download_files([sandbox_path])
                        if results and results[0].content and not results[0].error:
                            remote_content = results[0].content
                            if isinstance(remote_content, str):
                                remote_content = remote_content.encode("utf-8")
                            remote_hash = hashlib.md5(remote_content).hexdigest()
                            if remote_hash == local_hash:
                                self._last_hashes[cache_key] = local_hash
                                continue
                    except Exception:
                        pass  # 读取失败，需要上传

                files_to_upload.append((sandbox_path, local_content))
                self._last_hashes[cache_key] = local_hash
                has_changes = True

            if has_changes:
                self.backend.upload_files(files_to_upload)
                updated_skills.append(skill_name)

        # 同步完（无论有没有变更）刷新沙箱标记，下一轮就能走快路径
        if wanted is not None:
            self._write_marker(wanted)
        _log(f"FULL-SYNC done changed={len(updated_skills)}")

        return updated_skills

    # --------------------- 通知生成 ---------------------
    def _make_notification(self, skill_names: List[str]) -> Dict[str, Any]:
        skills_list = "\n".join(f"- {name}" for name in skill_names)
        notice = (
            f"[系统通知] 以下技能包已更新：\n{skills_list}\n"
            "请使用 `ls /skills/` 查看详情，对当前任务可能有帮助。"
        )
        return {"messages": [SystemMessage(content=notice)]}