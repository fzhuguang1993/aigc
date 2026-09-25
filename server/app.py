"""server/app.py —— FastAPI 入口：激活服务 + 转发网关 + 管理端 一个进程全扛

启动：uvicorn server.app:app --host 127.0.0.1 --port 8000
（生产走 nginx HTTPS 反代；worker 数固定 1——限流/nonce/探活都是进程内状态）
"""
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from server import config, store, upstream
from server.admin import router as admin_router
from server.auth import router as auth_router
from server.gateway import router as gateway_router

_stop = threading.Event()


@asynccontextmanager
async def _lifespan(_app):
    store.init_db()
    upstream.load_upstreams()
    if not upstream.UPSTREAMS:
        print("⚠ upstreams.json 未配置任何线路：/api/v1/jobs 提交会报 502，"
              "激活/查询功能不受影响")
    probe = threading.Thread(target=upstream.probe_loop, args=(_stop,),
                             daemon=True, name="probe")
    probe.start()
    threading.Thread(target=_spool_loop, daemon=True, name="spool").start()
    yield
    _stop.set()


app = FastAPI(title="AIGC Gate", docs_url=None, redoc_url=None,
              lifespan=_lifespan)
app.include_router(auth_router)
app.include_router(gateway_router)
app.include_router(admin_router)


def _spool_loop():
    """每小时清一次过期的参考图暂存（提交链路只在几分钟内用到）"""
    while not _stop.wait(3600):
        try:
            store.pop_expired_assets(config.ASSET_TTL)
        except Exception as e:
            print(f"⚠ 暂存清理失败：{e}")


@app.get("/")
def root():
    # 只报存活，不报版本/路由细节，减少公网指纹
    return {"ok": True}
