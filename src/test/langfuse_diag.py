"""模拟后端进程的真实顺序：先 load 主 .env（Cloud 配置），再加载我们的模块。"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "src"))

from dotenv import load_dotenv

load_dotenv(root / ".env", override=True)
import os

print("[before] PK =", (os.getenv("LANGFUSE_PUBLIC_KEY") or "")[:14], "HOST =", os.getenv("LANGFUSE_HOST"))

import agent.langfuse_trace as t

print("[after import] PK =", (os.getenv("LANGFUSE_PUBLIC_KEY") or "")[:14], "HOST =", os.getenv("LANGFUSE_HOST"))

from langfuse.langchain import CallbackHandler

try:
    h = CallbackHandler()
    print("[ok] handler:", type(h).__name__)
except Exception as exc:  # noqa: BLE001
    print("[fail] CallbackHandler raised:", type(exc).__name__, exc)

from langfuse import get_client

c = get_client()
print("[client] base_url =", getattr(c, "_base_url", None), "| enabled =", getattr(c, "_tracing_enabled", None))
