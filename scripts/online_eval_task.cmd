@echo off
REM 每 15 分钟跑一次在线评测（只跑硬规则，不调大模型 -> 零成本）
REM 幂等：已评过的 trace 会跳过，所以跑得再勤也不会重复写分
cd /d D:\pythoncode\ERP_OPENCLAW
if not exist logs mkdir logs
.venv\Scripts\python.exe src\test\online_eval.py --since-minutes 30 --limit 50 --no-judge >> logs\online_eval.log 2>&1
