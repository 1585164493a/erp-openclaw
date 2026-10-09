# src/tools/download_sandbox_file.py
"""
沙箱文件下载工具

将沙箱中的指定文件下载到本地 EXAMPLE_DIR/download/ 目录。
"""
from __future__ import annotations

from pathlib import Path


def _read_from_store(store, path: str):
    """从 LangGraph Store 读取 /memories/ 这类【虚拟命名空间】下的文件。

    ⚠️ 2026-09-29 修复「写成功但下载不到」：
       CompositeBackend 把 /memories/ 路由到 StoreBackend —— 文件正文写进
       MySQL 的 agent_state.langgraph_store，**容器文件系统里没有这个文件**。
       而本工具原来是纯去容器读的，于是必然 file_not_found。
       Agent 看到的现象就是"写入返回成功，下载却说不存在"，还会误判成挂载坏了。

    路径映射（已实测确认，不是猜的）：
        /memories/aff/report_x.md  ->  namespace=("aff",)  key="/aff/report_x.md"
        即：去掉 /memories 前缀后的剩余路径，首段做 namespace，整段做 key。

    返回正文（str）或 None（不在 Store 里 / 读不到）。
    """
    if store is None:
        return None
    p = path
    for root in ("/memories", "/persisted-skills"):
        if p.startswith(root + "/"):
            p = p[len(root):]          # /memories/aff/x.md -> /aff/x.md
            break
    else:
        return None                    # 不在虚拟命名空间下，不用回退
    segs = [s for s in p.split("/") if s]
    if not segs:
        return None

    item = None
    try:
        item = store.get((segs[0],), p)
    except Exception:
        item = None

    # ⚠️ key 布局不统一，直接映射可能落空：
    #    有的是 ns=("aff",) + key="/aff/report_x.md"，有的只是 key="/report_x.md"。
    #    实测就见过后者。所以映射落空时，在该 namespace 下【按文件名搜一遍】。
    if item is None:
        name = segs[-1]
        try:
            for it in store.search((segs[0],), limit=200):
                if (getattr(it, "key", "") or "").endswith(name):
                    item = it
                    break
        except Exception:
            item = None

    if item is None:
        return None
    val = item.value or {}
    content = val.get("content")
    # Store 里文件内容是【按行的列表】，不是字符串
    if isinstance(content, list):
        content = "\n".join("" if x is None else str(x) for x in content)
    return content


def create_download_tool(sandbox_backend, download_dir: Path, store=None):
    """
    创建 download_sandbox_file 工具工厂函数。

    Args:
        sandbox_backend: OpenSandboxBackend 实例，用于从沙箱读取文件。
        download_dir: 本地下载目标目录（如 EXAMPLE_DIR/download/）。
        store: 可选的 LangGraph Store；给了就能下载 /memories/ 下的文件
               （见 _read_from_store 的说明）。

    Returns:
        download_sandbox_file 工具函数。
    """
    from langchain_core.tools import tool

    # 确保本地目录存在
    download_dir.mkdir(parents=True, exist_ok=True)

    @tool
    def download_sandbox_file(sandbox_path: str, local_filename: str = "") -> str:
        """
        将沙箱中的指定文件下载到本地 download/ 目录。

        适用场景：
        - 下载生成的分析报告（/analysis/report_*.md）
        - 下载可视化图表（/data/chart_*.png）
        - 下载分析结果数据（/data/analysis_result.json）
        - 下载沙箱中的任意文件到本地

        Args:
            sandbox_path: 沙箱中的绝对文件路径（如 "/analysis/report_20260513.md"）
            local_filename: 本地保存的文件名（可选，为空则使用沙箱中的原始文件名）

        Returns:
            下载确认信息，含本地文件路径。
        """
        # 1. 校验路径
        if not sandbox_path or not sandbox_path.startswith("/"):
            return f"错误：sandbox_path 必须是绝对路径（以 / 开头），收到: {sandbox_path}"

        # 2. 从沙箱下载文件
        try:
            results = sandbox_backend.download_files([sandbox_path])
        except Exception as e:
            return f"错误：从沙箱读取文件失败: {e}"

        if not results:
            return f"错误：下载返回空结果"

        dl = results[0]
        content = None

        # 先按容器路径读
        if dl and not dl.error and dl.content is not None:
            content = dl.content
        else:
            # 读不到就回退到 Store —— /memories/ 下的文件在 Store 里，不在容器里
            content = _read_from_store(store, sandbox_path)
            if content is None:
                why = dl.error if dl else "no result"
                return (
                    f"错误：文件不存在或无法读取（{why}），路径: {sandbox_path}\n"
                    f"提示：如果这份文件是写到 /memories/ 等虚拟命名空间下的，"
                    f"请确认 Store 中确实存在该 key。"
                )

        # 3. 确定本地文件名
        if not local_filename:
            local_filename = Path(sandbox_path).name
        # 安全检查：防止路径穿越
        local_filename = Path(local_filename).name

        local_path = download_dir / local_filename

        # 4. 写入本地文件
        try:
            if isinstance(content, str):
                content_bytes = content.encode("utf-8")
            elif isinstance(content, bytes):
                content_bytes = content
            else:
                content_bytes = str(content).encode("utf-8")

            local_path.write_bytes(content_bytes)
        except Exception as e:
            return f"错误：写入本地文件失败: {e}"

        # 5. 返回结果
        file_size = len(content_bytes)
        size_str = (
            f"{file_size} B" if file_size < 1024
            else f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024
            else f"{file_size / (1024 * 1024):.1f} MB"
        )

        return (
            f"✅ 文件已下载到本地\n"
            f"沙箱路径: {sandbox_path}\n"
            f"本地路径: {local_path}\n"
            f"文件大小: {size_str}"
        )

    download_sandbox_file.name = "download_sandbox_file"
    return download_sandbox_file
