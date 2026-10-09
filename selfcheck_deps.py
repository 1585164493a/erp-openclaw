"""ERP_OPENCLAW 启动前依赖自检。

四个进程/依赖：
  ① MySQL        127.0.0.1:3306        库 agent_state（长期记忆）+ motorparts_db（业务）
  ② MongoDB      127.0.0.1:27017  库 langchain_db（checkpoint + 任务账本 + 展示消息）
  ③ OpenSandbox  127.0.0.1:8080   per-user 沙箱
  ④ MCP Server   127.0.0.1:8099        本项目自己的（要先起来，Agent 才加载得到工具）
  ⑤ 图表 MCP     阿里云百炼托管（走 443，需要 DASHSCOPE_API_KEY 鉴权）
  ⑥ 三进程端口   8090 Agent / 8099 MCP / 8080 ERP API

只读，不改数据。
"""
from __future__ import annotations

import os
import socket
import sys

PASS, FAIL, WARN = [], [], []


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
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:70]}"


print("=" * 80)
print("① 外部依赖")
print("=" * 80)
for label, host, port, must in (
    ("MySQL 127.0.0.1:3306", "127.0.0.1", 3306, True),
    ("MongoDB 127.0.0.1:27017", "127.0.0.1", 27017, True),
    ("OpenSandbox 127.0.0.1:8080", "127.0.0.1", 8080, True),
    ("DeepSeek 443", "api.deepseek.com", 443, True),
    ("阿里百炼 443（图表 MCP 走它）", "dashscope.aliyuncs.com", 443, True),
):
    ok, err = tcp(host, port)
    check(label, ok, "可达" if ok else err, optional=not must)

print()
print("=" * 80)
print("② 本项目三进程（现在应该都没起）")
print("=" * 80)
for label, port in (("ERP API 8080", 8080), ("MCP Server 8099", 8099), ("Agent 后端 8090", 8090)):
    ok, err = tcp("127.0.0.1", port)
    check(label, ok, "在监听" if ok else "未启动", optional=True)

print()
print("=" * 80)
print("③ 关键配置是否齐（只看有没有值，不打印值）")
print("=" * 80)
envp = os.path.join(r"D:\pythoncode\ERP_OPENCLAW", ".env")
check(".env 文件存在", os.path.exists(envp), envp)
if os.path.exists(envp):
    keys = {}
    with open(envp, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                keys[k.strip()] = v.strip()
    for k in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ALIBABA_API_KEY",
              "MONGODB_URI", "MYSQL_HOST", "OPENSANDBOX_DOMAIN"):
        has = bool(keys.get(k))
        check(f".env 有 {k}", has, "已配置" if has else "缺（会走代码默认值）",
              optional=(k != "DEEPSEEK_API_KEY" and k != "DASHSCOPE_API_KEY"))

print()
print("=" * 80)
print(f"结果：通过 {len(PASS)}   失败 {len(FAIL)}   可选未就绪 {len(WARN)}")
if FAIL:
    print("❌ 阻塞项：" + " | ".join(FAIL))
if WARN:
    print("⚪ 未启动/可选：" + " | ".join(WARN))
print("=" * 80)
sys.exit(1 if FAIL else 0)
