"""SandboxBackendProxy — 稳定句柄，内部 backend 可热替换 / **可延迟创建**。

1. 显式代理 SandboxBackendProtocol 全部方法 + id property，
   确保 Python MRO 不会找到协议层的 NotImplementedError 默认实现。
2. **懒创建**（2026-11-12）：不再在每次请求开始时无条件分配沙箱。
   只有真正发生沙箱操作（execute / read_file / write_file / py_exec …）时才创建，
   所以"你好""什么是安全库存"这类纯问答完全不占沙箱。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    SandboxBackendProtocol,
    WriteResult,
)


class SandboxBackendProxy(SandboxBackendProtocol):
    """代理所有 SandboxBackendProtocol 方法；支持热替换与延迟创建。"""

    def __init__(self, backend: Optional[SandboxBackendProtocol] = None,
                 factory: Optional[Callable[[], SandboxBackendProtocol]] = None) -> None:
        self._backend = backend
        self._factory = factory
        self._lock = threading.Lock()

    # ---- 延迟绑定 ----
    def bind(self, backend: SandboxBackendProtocol) -> None:
        self._backend = backend

    def is_bound(self) -> bool:
        return self._backend is not None

    def _ensure(self) -> SandboxBackendProtocol:
        """第一次真正用到沙箱时才创建（首次会等 45~90s，之后命中缓存）。"""
        if self._backend is not None:
            return self._backend
        with self._lock:
            if self._backend is None:
                if self._factory is None:
                    raise RuntimeError("沙箱尚未绑定，且没有提供创建工厂")
                self._backend = self._factory()
        return self._backend

    @property
    def id(self) -> str:
        backend = self._backend
        return backend.id if backend is not None else ""

    def replace_backend(self, backend: SandboxBackendProtocol) -> None:
        self._backend = backend

    # ---- 同步方法（全部经过 _ensure） ----

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._ensure().execute(command, timeout=timeout)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        return self._ensure().read(file_path, offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        return self._ensure().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return self._ensure().edit(file_path, old_string, new_string, replace_all)

    def ls_info(self, path: str) -> list:
        return self._ensure().ls_info(path)

    def glob_info(self, pattern: str, path: str = "/") -> list:
        return self._ensure().glob_info(pattern, path)

    def grep_raw(self, pattern: str, path: str | None = None, glob: str | None = None) -> list | str:
        return self._ensure().grep_raw(pattern, path, glob)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._ensure().upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._ensure().download_files(paths)

    # ---- 异步方法 ----

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._ensure().execute(command, timeout=timeout)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        return self._ensure().read(file_path, offset, limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self._ensure().write(file_path, content)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return self._ensure().edit(file_path, old_string, new_string, replace_all)

    async def als_info(self, path: str) -> list:
        return self._ensure().ls_info(path)

    async def aglob_info(self, pattern: str, path: str = "/") -> list:
        return self._ensure().glob_info(pattern, path)

    async def agrep_raw(self, pattern: str, path: str | None = None, glob: str | None = None) -> list | str:
        return self._ensure().grep_raw(pattern, path, glob)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._ensure().upload_files(files)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._ensure().download_files(paths)
