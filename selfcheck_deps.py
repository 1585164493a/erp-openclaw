"""ERP_OPENCLAW 启动前依赖自检。

修过的坑（2026-10-12）：
    老版本把 MySQL / MongoDB / OpenSandbox 的目标**写死成 127.0.0.1** —— 但它们在 VM 上，
    所以永远报 FAIL（还会让人误判"服务挂了"）。OpenSandbox 那条更糟：本机 127.0.0.1:8080
    其实是 **ERP API**，于是它"通过"了，但通过的是另一个服务，属于假阳性。
    现在改为**从 .env 读真实地址**（MYSQL_HOST / MONGODB_URI / OPENSANDBOX_DOMAIN）。

六个依赖：
  ① MySQL        库 agent_state（长期记忆）+ motorparts_db（业务）
  ② MongoDB      库 langchain_db（checkpoint + 任务账本 + 展示消息）
  ③ OpenSandbox  per-user 沙箱控制面
  ④ ERP API      本项目自己的数据后端（8080）
  ⑤ MCP Server   本项目自己的工具层（8099）
  ⑥ Agent 后端   8091（2026-09-29 从 8090 迁到这里）
  ⑦ 图表 MCP     阿里云百炼托管（走 443，需要 DASHSCOPE_API_KEY 鉴权）

只读，不改数据。
"""
from __future__ import annotations

import os
import socket
import sys
from urllib.parse import urlparse

PASS, FAIL, WARN = [], [], []
PROJECT = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(PROJECT, ".env")

AGENT_PORT = 8091  # 8090 已被一个杀不掉的残留进程占用，后端迁到 8091


def check(name, ok, detail="", optional=False):
    if ok:
        PASS.append(name); mark = "OK  "
    elif optional:
        WARN.append(name); mark = "WARN"
    else:
        FAIL.append(name); mark = "FAIL"
    print(f"   [{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def tcp(host, port, timeout=3.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:70]}"


# ---------- 先读 .env（后面的地址都从这里来）----------
env: dict[str, str] = {}
if os.path.exists(ENV_PATH):
    with open(ENV_PATH, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")


def parse_mongo(uri: str):
    """mongodb://user:pass@host:port/?... -> (host, port)"""
    try:
        u = urlparse(uri if "://" in uri else "mongodb://" + uri)
        hosts = (u.netloc.split("@")[-1] or "").split(",")[0]
        host, _, port = hosts.partition(":")
        return host or "127.0.0.1", int(port or 27017)
    except Exception:
        return "127.0.0.1", 27017


def parse_url(url: str, default_port: int = 8080):
    try:
        u = urlparse(url if "://" in url else "http://" + url)
        return u.hostname or "127.0.0.1", u.port or default_port
    except Exception:
        return "127.0.0.1", default_port


mysql_host = env.get("MYSQL_HOST", "127.0.0.1")
mongo_host, mongo_port = parse_mongo(env.get("MONGODB_URI", "mongodb://127.0.0.1:27017"))
sandbox_host, sandbox_port = parse_url(env.get("OPENSANDBOX_DOMAIN", "http://127.0.0.1:8080"))

print("=" * 80)
print("① 外部依赖（地址从 .env 读，不是写死的 127.0.0.1）")
print("=" * 80)
for label, host, port in (
    (f"MySQL {mysql_host}:3306",          mysql_host,   3306),
    (f"MongoDB {mongo_host}:{mongo_port}", mongo_host,   mongo_port),
    (f"OpenSandbox {sandbox_host}:{sandbox_port}", sandbox_host, sandbox_port),
    ("DeepSeek 443",        "api.deepseek.com",     443),
    ("阿里百炼 443（图表 MCP）", "dashscope.aliyuncs.com", 443),
):
    ok, err = tcp(host, port)
    check(label, ok, "可达" if ok else err)

print()
print("=" * 80)
print("② 本项目进程")
print("=" * 80)
for label, port in (("ERP API 8080", 8080), ("MCP Server 8099", 8099),
                    (f"Agent 后端 {AGENT_PORT}", AGENT_PORT)):
    ok, err = tcp("127.0.0.1", port)
    check(label, ok, "在监听" if ok else "未启动", optional=True)

print()
print("=" * 80)
print("③ 关键配置是否齐（只看有没有值，不打印值）")
print("=" * 80)
check(".env 文件存在", os.path.exists(ENV_PATH), ENV_PATH)
for k in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ALIBABA_API_KEY",
          "MONGODB_URI", "MYSQL_HOST", "OPENSANDBOX_DOMAIN"):
    has = bool(env.get(k))
    check(f".env 有 {k}", has, "已配置" if has else "缺（会走代码默认值）",
          optional=k not in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"))

print()
print("=" * 80)
print(f"结果：通过 {len(PASS)}   失败 {len(FAIL)}   可选未就绪 {len(WARN)}")
if FAIL:
    print("❌ 阻塞项：" + " | ".join(FAIL))
if WARN:
    print("⚪ 未启动/可选：" + " | ".join(WARN))
print("=" * 80)
sys.exit(1 if FAIL else 0)
