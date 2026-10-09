"""
DeepAgent Web 应用一键启动脚本。

服务编排（多进程，各自独立端口）：
  1. ERP API   : uvicorn erp_api.main:app            → http://127.0.0.1:8080   （Python 实现的数据后端，替代原 Java）
  2. MCP Server: python -m mcp_server.server_main    → http://127.0.0.1:8099/mcp（erp-api 工具源）
  3. 后端 API  : uvicorn api_view.web_main:app        → http://localhost:8091   （Agent 对话/SSE）
  4. 前端 Vite : npm run dev                          → http://localhost:3000

启动顺序按依赖关系：先 ERP API → MCP → 后端（后端启动时会预计算 MCP 工具）→ 前端。
"""

import atexit
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request


def _base_env():
    """构造子进程环境：确保 PYTHONPATH 含 src 目录。"""
    project_root = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(project_root, "src")
    env = os.environ.copy()
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    return project_root, env


def _project_python() -> str:
    """用【项目自己的 .venv 解释器】跑子进程，而不是 sys.executable。

    ⚠️ 2026-09-29 修复。原实现三个子进程都用 sys.executable，含义是
    「谁跑 start_web.py 就用谁去起服务」。于是：

        用 conda 环境跑  ->  D:\\conda_envs\\langchain_v1\\python.exe -m uvicorn ...
                        ->  ModuleNotFoundError: No module named 'pymysql'

    因为依赖（pymysql / pymongo / deepagents …）装在【项目的 .venv】里，
    conda 那个环境没有。报错信息只说缺 pymysql，很容易误判成代码问题。

    现在优先用 .venv\\Scripts\\python.exe；找不到才退回 sys.executable，
    这样无论谁（conda / 系统 python / 双击 bat）启动，都用对解释器。
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(project_root, ".venv", "Scripts", "python.exe")
    if os.path.isfile(cand):
        return cand
    print(f"[WARN] 未找到项目 .venv 解释器（{cand}），退回当前解释器 {sys.executable}")
    return sys.executable


def _start(desc, cmd, env, cwd=None):
    """启动子进程并实时转发输出。"""
    print(f"[System] 启动 {desc}: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )

    def relay():
        try:
            for line in proc.stdout:
                if line:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        print(f"[{desc}] {text}")
        except Exception:
            pass

    threading.Thread(target=relay, daemon=True).start()
    return proc


def wait_http(url, needle=None, max_wait=60, label="HTTP"):
    """轮询等待 HTTP 就绪。"""
    start = time.time()
    while time.time() - start < max_wait:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    if needle is None or needle in body:
                        print(f"[System] {label} 就绪: {url}")
                        return True
        except Exception:
            pass
        time.sleep(1.5)
    print(f"[ERROR] {label} 等待超时: {url} ({max_wait}s)")
    return False


# 我们这套服务占用的端口（后端 2026-09-29 从 8090 移到 8091，
# 因为 8090 被一个杀不掉的旧进程长期占着）
OUR_PORTS = (8080, 8099, 8091, 3000)


def _listen_pids(port: int):
    """用 netstat 找出监听指定端口的 PID。

    为什么不用 Get-NetTCPConnection / psutil：前者要提权，后者未必装了；
    netstat -ano 在普通权限下就能跑，是这里最稳的判据。
    """
    pids = []
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return []
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[1].endswith(f":{port}"):
            try:
                pids.append(int(parts[-1]))
            except ValueError:
                pass
    return sorted(set(pids))


def _free_our_ports(verbose: bool = False) -> None:
    """把仍占着 OUR_PORTS 的进程清掉（按进程树杀）。

    两个时机用它：
      1. 启动前 —— 保证上一次的残留不会让这次 bind 失败；
      2. 退出时 —— 兜住 cleanup 没覆盖到的（父进程不是我们的那种）。
    """
    me = os.getpid()
    for port in OUR_PORTS:
        for pid in _listen_pids(port):
            if pid == me:
                continue
            if verbose:
                print(f"[System] 端口 {port} 被 pid {pid} 占用，清理（含进程树）")
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            except Exception:
                if verbose:
                    print(f"[System]   pid {pid} 清理失败（可能权限不足）")



def wait_tcp(host, port, max_wait=60, label="TCP"):
    """轮询等待 TCP 端口开放。"""
    start = time.time()
    while time.time() - start < max_wait:
        try:
            with socket.create_connection((host, port), timeout=3):
                print(f"[System] {label} 就绪: {host}:{port}")
                return True
        except OSError:
            pass
        time.sleep(1.5)
    print(f"[ERROR] {label} 等待超时: {host}:{port} ({max_wait}s)")
    return False


def run_erp_api():
    """启动 Python ERP API（数据后端，端口 8080）。"""
    project_root, env = _base_env()
    return _start(
        "ERP-API",
        [_project_python(), "-m", "uvicorn", "erp_api.main:app",
         "--host", "0.0.0.0", "--port", "8080"],
        env,
        cwd=project_root,
    )


def run_mcp():
    """启动 MCP erp-api Server（端口 8099，供 Agent 预计算拉取工具）。"""
    project_root, env = _base_env()
    return _start(
        "MCP",
        [_project_python(), "-m", "mcp_server.server_main"],
        env,
        cwd=project_root,
    )


def run_backend():
    """启动 Agent 对话后端（uvicorn api_view.web_main:app，端口 8091）。"""
    project_root, env = _base_env()
    return _start(
        "Backend",
        [_project_python(), "-m", "uvicorn", "api_view.web_main:app",
         "--reload", "--host", "0.0.0.0", "--port", "8091"],
        env,
        cwd=project_root,
    )


def run_frontend():
    """启动 Vue3 前端（Vite，端口 3000）。"""
    project_root = os.path.dirname(os.path.abspath(__file__))
    frontend_dir = os.path.join(project_root, "frontend")
    if not os.path.exists(os.path.join(frontend_dir, "node_modules")):
        print("[Frontend] 未发现 node_modules，先执行 npm install ...")
        result = subprocess.run(
            "npm install",
            cwd=frontend_dir,
            shell=True,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("[ERROR] 前端依赖安装失败:")
            print(result.stderr)
            return None
        print("[Frontend] 依赖安装完成")
    # ⚠️ 2026-09-29 修复：Windows 上不能直接传 "npm"
    #
    #   原因：subprocess（shell=False）最终走 CreateProcess，而 CreateProcess
    #   **不应用 PATHEXT** —— 传 "npm" 不会自动找到 npm.cmd，直接
    #   FileNotFoundError: [WinError 2] 系统找不到指定的文件。
    #   实测后果：一键启动时 ERP API / MCP / 后端都正常，**只有前端必失败**，
    #   而失败信息埋在 stderr 的 traceback 里，很容易被忽略。
    #
    #   修法：用 shutil.which 解析出真实路径（本机是 D:\nodejs\npm.cmd）。
    npm_exe = shutil.which("npm") or shutil.which("npm.cmd") or "npm"
    print(f"[Frontend] 使用 npm: {npm_exe}")
    return _start(
        "Frontend",
        [npm_exe, "run", "dev"],
        os.environ.copy(),
        cwd=frontend_dir,
    )


def main():
    print("=" * 60)
    print("         ERP_OPENCLAW Web Application Launcher")
    print("=" * 60)
    print("NOTE: Agent 初始化（预计算 MCP 工具 + 沙箱）需要 30-60 秒")
    print("      请耐心等待 /health 返回 healthy")
    print()

    # 0) 启动前先清端口 —— 上一次 Ctrl+C 若留下残留，这次就不会 bind 失败
    print("[System] 启动前清理端口占用...")
    _free_our_ports(verbose=True)

    procs = []
    stopped = threading.Event()

    def _kill_tree(pid: int, why: str = "") -> None:
        """按【整棵进程树】杀。

        ⚠️ 2026-09-29 修复「Ctrl+C 关了服务、端口却还占着」：
           原来 cleanup() 只调 proc.terminate()，那**只杀直接子进程**。
           而这两个子进程自己还会 fork：
               Backend  : uvicorn --reload → reloader → worker
               Frontend : npm run dev → cmd → node → vite
           孙进程会被留下、继续占着端口；下次启动就 bind 不上，
           表现为"点了启动没反应 / 后端 health 一直不通"。
           taskkill /T 会连同整棵子树一起杀。
        """
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        if why:
            print(f"[System]   {why}")

    def cleanup():
        print("\n\n[System] 停止全部服务...")
        stopped.set()
        for name, proc in procs:
            if proc and proc.poll() is None:
                print(f"[System] 终止 {name} (pid={proc.pid}) —— 含其子进程树")
                _kill_tree(proc.pid)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        # 再兜一遍：清理所有仍占着我们端口的进程
        # （覆盖"父进程不是我们"的情况，例如之前某次启动留下的残留）
        _free_our_ports(verbose=True)
        print("[System] 全部服务已停止")

    # 兜底：无论从哪条路径退出（含未捕获异常），都走一次清理
    atexit.register(cleanup)

    # 1) ERP API
    proc = run_erp_api()
    procs.append(("ERP-API", proc))
    if not wait_http("http://127.0.0.1:8080/health", "healthy", max_wait=90, label="ERP-API"):
        cleanup()
        sys.exit(1)

    # 2) MCP Server
    proc = run_mcp()
    procs.append(("MCP", proc))
    if not wait_tcp("127.0.0.1", 8099, max_wait=60, label="MCP"):
        cleanup()
        sys.exit(1)

    # 3) 后端（启动时预计算 MCP 工具 + 初始化 Agent）
    proc = run_backend()
    procs.append(("Backend", proc))
    if not wait_http("http://localhost:8091/health", "healthy", max_wait=240, label="Backend"):
        print("[ERROR] 后端未能在 240 秒内就绪，请检查上方日志（常见原因：MongoDB/OpenSandbox 未启动）")
        cleanup()
        sys.exit(1)

    # 4) 前端
    proc = run_frontend()
    if proc:
        procs.append(("Frontend", proc))

    print()
    print("=" * 60)
    print("  All services started successfully!")
    print("=" * 60)
    print()
    print("  Please access:")
    print("    - ERP API  : http://localhost:8080/docs")
    print("    - MCP      : http://127.0.0.1:8099/mcp")
    print("    - Frontend : http://localhost:3000")
    print("    - API docs : http://localhost:8091/docs")
    print()
    print("  Press Ctrl+C to stop all services")
    print("=" * 60)

    try:
        while True:
            time.sleep(1)
            if all(p.poll() is not None for _, p in procs if p):
                print("[System] 全部子进程已退出")
                break
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()