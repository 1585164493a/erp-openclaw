"""把 ERP_OPENCLAW 中有效的百炼 key 同步到 Multimodal_RAG 的 ALIBABA_API_KEY。"""
import pathlib
import re

ROOT = pathlib.Path(r"D:\pythoncode")


def load(path: pathlib.Path) -> dict:
    d = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        d[k.strip()] = v.strip().strip('"').strip("'")
    return d


src = ROOT / "ERP_OPENCLAW" / ".env"
dst = ROOT / "Multimodal_RAG" / ".env"

new_key = load(src)["DASHSCOPE_API_KEY"]
text = dst.read_text(encoding="utf-8")
text_new, n = re.subn(r"(?m)^ALIBABA_API_KEY=.*$", f"ALIBABA_API_KEY={new_key}", text)

if n == 0:
    raise SystemExit("未找到 ALIBABA_API_KEY 行，未做修改")

dst.write_text(text_new, encoding="utf-8")
print(f"已更新 {dst.name}：替换 {n} 处，新值长度 {len(new_key)}")