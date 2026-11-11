"""从 Windows 本机验证：能连上 VM 上的 Langfuse 并发出一条 trace。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent.langfuse_trace import get_callback, flush, is_enabled  # noqa: E402

print("enabled:", is_enabled())

from langfuse import observe  # noqa: E402


@observe(name="connectivity-check")
def demo(x: int) -> int:
    return x * 2


print("result:", demo(21))
flush()
print("flushed")
