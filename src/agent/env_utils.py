import os

from dotenv import load_dotenv

load_dotenv(override=True)

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
ZHIPU_API_KEY = os.getenv('ZHIPU_API_KEY')
MINIMAX_API_KEY = os.getenv('MINIMAX_API_KEY')
ALIBABA_API_KEY = os.getenv('ALIBABA_API_KEY')
K2_API_KEY = os.getenv('K2_API_KEY')
# 阿里云百炼（DashScope）API Key。
# 除了模型调用，它还用于鉴权百炼托管的 MCP 服务
# （图表 MCP 就是走 Authorization: Bearer <DASHSCOPE_API_KEY> 访问的）
DASHSCOPE_API_KEY = os.getenv('DASHSCOPE_API_KEY')

K2_BASE_URL = os.getenv('K2_BASE_URL')
ALIBABA_BASE_URL = os.getenv('ALIBABA_BASE_URL')
MINIMAX_BASE_URL = os.getenv('MINIMAX_BASE_URL')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL')
DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL')
ZHIPU_BASE_URL = os.getenv('ZHIPU_BASE_URL')
