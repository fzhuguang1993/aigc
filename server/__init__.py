"""商用网关服务端（发卡激活 + 请求转发），与客户端同仓不同包，互不 import。

本地启动（开发）：
    pip install -r server/requirements.txt
    python -m server.cards gen --days 30 --count 10 --out 卡密.txt   # 先造一批卡
    uvicorn server.app:app --host 0.0.0.0 --port 8000
生产部署见计划：nginx(HTTPS) 反代 + systemd 托管。
"""
