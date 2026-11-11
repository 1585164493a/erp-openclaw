"""探测：哪些模型支持 structured output（response_format=json_schema）。

Langfuse 的 LLM-as-a-Judge 需要 json_schema；只支持 json_object 的模型会报
"This response_format type is unavailable now"。
本脚本只打印状态码和错误信息，不打印任何 key。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[2]   # src/test/xxx.py -> 项目根
from dotenv import load_dotenv  # noqa: E402
import os  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

CASES = [
    ("deepseek", "https://api.deepseek.com/v1", os.getenv("DEEPSEEK_API_KEY"), "deepseek-flash"),
    ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1", os.getenv("DASHSCOPE_API_KEY"), "qwen-plus"),
]

SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["score", "reason"],
    "additionalProperties": False,
}


def probe(name: str, base: str, key: str | None, model: str) -> None:
    if not key:
        print(f"{name:<10} 没配 key，跳过")
        return
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "给这句话打分：你好。只输出 JSON。"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "verdict", "schema": SCHEMA, "strict": True}},
        "max_tokens": 64,
    }
    try:
        with httpx.Client(timeout=25.0) as c:
            r = c.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
        if r.status_code == 200:
            txt = r.json()["choices"][0]["message"]["content"][:80].replace("\n", " ")
            print(f"{name:<10} {model:<16} 200 OK  -> {txt}")
        else:
            try:
                msg = r.json().get("error", {}).get("message") or r.text
            except Exception:  # noqa: BLE001
                msg = r.text
            print(f"{name:<10} {model:<16} {r.status_code}  -> {str(msg)[:120]}")
    except Exception as exc:  # noqa: BLE001
        print(f"{name:<10} {model:<16} FAIL -> {type(exc).__name__}: {str(exc)[:80]}")


for c in CASES:
    probe(*c)
