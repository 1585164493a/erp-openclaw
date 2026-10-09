import os

# ERP 数据后端 API 地址（原为 Java 后端 http://localhost:8080/api）
# 本仓库提供 Python 实现：erp_api（uvicorn erp_api.main:app --port 8080）。
# 可通过环境变量 ERP_API_BASE_URL 覆盖。
ERP_API_BASE_URL = os.getenv("ERP_API_BASE_URL", "http://localhost:8080/api")
# 兼容旧命名：仍叫 JAVA_API_BASE_URL，方便其它模块引用不炸
JAVA_API_BASE_URL = ERP_API_BASE_URL

# MCP 服务监听配置（独立进程，供 Agent 通过 127.0.0.1:8099/mcp 获取工具）
# 注意：Agent 后端 uvicorn 占用 8090，因此 MCP 独立监听 8099 避免端口冲突。
MCP_HOST = "127.0.0.1"
MCP_PORT = int(os.getenv("MCP_PORT", "8099"))
MCP_PATH = "/mcp"

# Agent 侧 MultiServerMCPClient 使用的 erp-api 地址（127.0.0.1:{MCP_PORT}{MCP_PATH}）
ERP_MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}{MCP_PATH}"