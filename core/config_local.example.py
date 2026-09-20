# ============================================================
# config_local.example.py —— 本地敏感配置模板
# 首次使用：复制本文件为 config_local.py，并填入真实信息
#   cp core/config_local.example.py core/config_local.py
# ============================================================

# 账号列表（API 服务地址，支持多账号负载均衡与故障转移）
ACCOUNTS = [
    {"name": "acc1", "base": "http://127.0.0.1:7860/api/v1", "concurrency": 1},
    # {"name": "acc2", "base": "http://<服务地址>:7860/api/v1", "concurrency": 1},
]
