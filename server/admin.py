"""server/admin.py —— 极简管理端（/admin/*，X-Admin-Token 口令）

server_config.json 的 admin_token 留空 = 整个管理端关闭（返回 404）。
口令比对用常量时间；公网部署建议再叠一层防火墙/内网白名单。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server import config, cards, store
from server import upstream

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(request: Request):
    if not config.ADMIN_TOKEN:
        raise HTTPException(404, "管理端未启用")
    import hmac
    given = request.headers.get("x-admin-token", "")
    if not hmac.compare_digest(given, config.ADMIN_TOKEN):
        raise HTTPException(401, "管理口令错误")
    return True


class GenIn(BaseModel):
    days: int
    count: int = 10


class KeyIn(BaseModel):
    card_key: str


class BanIn(BaseModel):
    machine_code: str
    reason: str = ""


@router.post("/cards")
def gen_cards(body: GenIn, _=Depends(require_admin)):
    if not (1 <= body.days <= 3650):
        raise HTTPException(400, "时长需在 1-3650 天之间")
    if not (1 <= body.count <= 1000):
        raise HTTPException(400, "单次最多生成 1000 张")
    keys, inserted = cards.generate(body.days, body.count)
    return {"generated": inserted, "card_keys": keys}


@router.get("/cards")
def list_cards(status: str = "", limit: int = 200, _=Depends(require_admin)):
    return {"items": store.list_cards(status or None, limit=limit),
            "counts": {s: store.count_cards(s)
                       for s in ("unused", "used", "disabled")}}


@router.post("/cards/disable")
def disable_card(body: KeyIn, _=Depends(require_admin)):
    key = cards.normalize(body.card_key)
    if not key or not store.get_card(key):
        raise HTTPException(404, "卡密不存在")
    store.set_card_status(key, "disabled")
    return {"ok": True, "card_key": key}


@router.post("/ban")
def ban(body: BanIn, _=Depends(require_admin)):
    machine = body.machine_code.strip().upper()
    if not machine:
        raise HTTPException(400, "机器码不能为空")
    store.ban_machine(machine, body.reason)
    return {"ok": True, "machine_code": machine}


@router.post("/unban")
def unban(body: BanIn, _=Depends(require_admin)):
    store.unban_machine(body.machine_code.strip().upper())
    return {"ok": True}


@router.get("/status")
def status(_=Depends(require_admin)):
    """总览：卡库余量、今日提交、线路健康（不含任何上游地址）"""
    return {"cards": {s: store.count_cards(s) for s in ("unused", "used", "disabled")},
            "jobs_today": store.jobs_today(),
            "upstreams": [u.info() for u in upstream.UPSTREAMS]}
