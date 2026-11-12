"""沙箱内 sitecustomize（被 sandbox_setup 上传到 venv 的 site-packages）。

两个作用：
1. **默认中文字体**：matplotlib 画图时中文不再变方框（镜像里已有，这里是兜底/覆盖）。
2. **禁止直连数据库**：沙箱到 MySQL(3306) 是不通的，实测超时会让一题烧掉 1361 秒。
   这里把 3306 的连接改成**立即失败**并给出明确指引，让模型一秒内就知道该改用 sql_query。
"""
import socket

try:  # 字体设置：matplotlib 缺失时静默跳过
    import matplotlib

    matplotlib.rcParams["font.sans-serif"] = [
        "WenQuanYi Micro Hei",
        "WenQuanYi Zen Hei",
        "DejaVu Sans",
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False
except Exception:  # noqa: BLE001
    pass


_DB_PORTS = {3306, 3307}
_HINT = (
    "沙箱内禁止直连 MySQL(3306)：业务数据请改用 sql_query / "
    "sql_schema_overview / sql_list_tables 工具查询（此限制只为让失败快速返回，避免长时间超时）"
)

_orig_create_connection = socket.create_connection


def _guarded_create_connection(address, *args, **kwargs):
    if isinstance(address, tuple) and len(address) >= 2 and address[1] in _DB_PORTS:
        raise ConnectionRefusedError(_HINT)
    return _orig_create_connection(address, *args, **kwargs)


socket.create_connection = _guarded_create_connection

# 有些驱动直接 socket.socket().connect(...)，绕开 create_connection
_orig_socket_connect = socket.socket.connect


def _guarded_connect(self, address, *args, **kwargs):
    if isinstance(address, tuple) and len(address) >= 2 and address[1] in _DB_PORTS:
        raise ConnectionRefusedError(_HINT)
    return _orig_socket_connect(self, address, *args, **kwargs)


socket.socket.connect = _guarded_connect
