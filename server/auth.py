"""server/auth.py —— 卡密激活、令牌签发与网关鉴权

令牌（无状态 HMAC）：token = b64url("卡密|机器码|到期戳") + "." + hmac 前 32 位。
服务端不存会话：验签 + 到期比对即可放行，SQLite 只兜底查卡状态与封禁
（作废/封机即时生效，不必等令牌自然到期）。

对外三个口：
- POST /auth/activate  卡密激活/续费（同机再来一张卡 = 到期时间叠加）
- POST /auth/verify    客户端每日联网校验（续期/封禁即时感知的通道）
- require_license      /api/v1/* 全部端点共用的 FastAPI 依赖（头 + 防重放）
"""
import base64
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server import config, cards, store
from server.ratelimit import ACTIVATE_IP, CARD_FAIL, NONCES

router = APIRouter(prefix="/auth", tags=["auth"])

_EXPIRED = {"reason": "expired"}          # 客户端据此弹「已过期，请激活」


# ---------------- 令牌 ----------------

def _sign(payload):
    return hmac.new(config.SECRET.encode("utf-8"), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def issue_token(card_key, machine_code, expire_ts):
    payload = f"{card_key}|{machine_code}|{expire_ts}"
    raw = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return f"{raw}.{_sign(payload)}"


def parse_token(token):
    """解出 (card_key, machine_code, expire_ts)；签名不符返回 None。"""
    try:
        raw, sig = str(token or "").rsplit(".", 1)
        payload = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
        card_key, machine, expire = payload.rsplit("|", 2)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        return card_key, machine, int(expire)
    except Exception:
        return None


# ---------------- 共用校验 ----------------

def _check_card_usable(card_key, machine_code):
    """卡存在、未作废、机器码相符、机器未封禁、未到期。

    返回 (card, http_error)：一切不正常都用 HTTPException 表达，
    401=凭证本身不行（重配/换机/被拉黑），403=过期（引导输新卡）。"""
    card = store.get_card(card_key)
    if not card or card["status"] == "disabled":
        return None, HTTPException(401, detail={"reason": "invalid",
                                                "message": "卡密无效或已作废"})
    if card["status"] != "used" or card["machine_code"] != machine_code:
        return None, HTTPException(401, detail={"reason": "machine_mismatch",
                                                "message": "令牌与本机不匹配，请重新激活"})
    if store.is_banned(machine_code):
        return None, HTTPException(403, detail={"reason": "banned",
                                                "message": "该设备已被封禁，请联系售后"})
    if card["expire_at"] <= int(time.time()):
        return None, HTTPException(403, detail=_EXPIRED | {"message": "授权已到期，请输入新卡密"})
    return card, None


def _client_ip(request):
    """nginx 反代场景优先取 X-Forwarded-For 首段"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


# ---------------- 激活 / 校验 ----------------

class ActivateIn(BaseModel):
    card_key: str
    machine_code: str
    app_version: str = ""


class VerifyIn(BaseModel):
    machine_code: str
    token: str


def _activate_error(key, machine, ip, app_version, code, reason, message=""):
    store.log_activation(key, machine, ip, app_version, reason)
    raise HTTPException(code, detail={"reason": reason, "message": message or reason})


@router.post("/activate")
def activate(body: ActivateIn, request: Request):
    if not config.SECRET:
        raise HTTPException(500, "服务端未配置密钥（server_config.json 的 secret）")
    machine = str(body.machine_code or "").strip().upper()
    if not machine or len(machine) > 64:
        raise HTTPException(400, "机器码缺失")
    ip = _client_ip(request)
    if ACTIVATE_IP.hit(ip) > config.ACTIVATE_RATE_PER_MIN:
        raise HTTPException(429, "尝试太频繁，请稍后再试")

    key = cards.normalize(body.card_key)
    if not key:
        _activate_error("", machine, ip, body.app_version,
                        404, "bad_format", "卡密格式不正确（应为 16 位，分组连字符可选）")
    if store.recent_failures(key, config.CARD_FAIL_WINDOW) >= config.CARD_FAIL_LIMIT:
        _activate_error(key, machine, ip, body.app_version,
                        429, "locked", "该卡密尝试次数过多，请1小时后再试或联系售后")

    card = store.get_card(key)
    if not card:
        _activate_error(key, machine, ip, body.app_version, 404, "not_found", "卡密不存在")
    if card["status"] == "disabled":
        _activate_error(key, machine, ip, body.app_version, 403, "disabled", "卡密已作废")
    if store.is_banned(machine):
        _activate_error(key, machine, ip, body.app_version, 403, "banned", "该设备已被封禁")

    now = int(time.time())
    if card["status"] == "used":
        if card["machine_code"] != machine:
            _activate_error(key, machine, ip, body.app_version,
                            409, "other_machine", "该卡密已在其他电脑使用，请联系售后")
        # 同机重复输同一张卡：幂等重发令牌，不再叠加天数
        expire = card["expire_at"]
        store.log_activation(key, machine, ip, body.app_version, "ok")
        CARD_FAIL.reset(key)
    else:
        # 续费叠加：以该机器当前最晚到期时间为基数（无记录则从现在起算）
        base = max(now, store.machine_expire(machine))
        expire = store.use_card(key, machine, card["days"], base)
        if expire is None:                       # 并发下被抢先激活：按状态重判一次
            again = store.get_card(key)
            if again and again["machine_code"] == machine:
                expire = again["expire_at"]
            else:
                _activate_error(key, machine, ip, body.app_version,
                                409, "other_machine", "该卡密已在其他电脑使用")
        store.log_activation(key, machine, ip, body.app_version, "ok")
        CARD_FAIL.reset(key)

    return {"token": issue_token(key, machine, expire),
            "card_key": key,
            "expire_at": expire,
            "days_remaining": max(0, (expire - now + 86399) // 86400),
            "offline_grace_days": config.OFFLINE_GRACE_DAYS,
            "server_time": now}


@router.post("/verify")
def verify(body: VerifyIn):
    parsed = parse_token(body.token)
    if not parsed:
        raise HTTPException(401, detail={"reason": "invalid", "message": "令牌无效"})
    key, machine, _expire = parsed
    card, err = _check_card_usable(key, machine)
    if err:
        raise err
    now = int(time.time())
    return {"valid": True,
            "card_key": key,
            "expire_at": card["expire_at"],
            "days_remaining": max(0, (card["expire_at"] - now + 86399) // 86400),
            "server_time": now}


# ---------------- 网关鉴权依赖（/api/v1/* 共用） ----------------

def require_license(request: Request):
    """校验 X-License/X-Machine/X-Ts/X-Nonce 四个头。

    时间窗 ±REPLAY_WINDOW + nonce 一次性：抓包重放在 5 分钟窗口内也会被
    nonce 挡下；但防 MITM 靠的是部署层的 HTTPS，这里只是提高脚本门槛。"""
    h = request.headers
    parsed = parse_token(h.get("x-license", ""))
    if not parsed:
        raise HTTPException(401, detail={"reason": "invalid", "message": "缺少或非法的授权令牌"})
    key, machine, expire_ts = parsed
    if h.get("x-machine", "").upper() != machine:
        raise HTTPException(401, detail={"reason": "machine_mismatch", "message": "令牌与机器码不符"})
    try:
        ts = int(h.get("x-ts", "0"))
    except ValueError:
        raise HTTPException(401, detail={"reason": "invalid", "message": "时间戳非法"})
    if abs(int(time.time()) - ts) > config.REPLAY_WINDOW:
        raise HTTPException(401, detail={"reason": "stale", "message": "时间戳偏差过大（本机时间对吗？）"})
    nonce = h.get("x-nonce", "")
    if not nonce or NONCES.seen_or_add(f"{machine}:{nonce}"):
        raise HTTPException(401, detail={"reason": "replay", "message": "重复请求"})
    if expire_ts <= int(time.time()):
        raise HTTPException(403, detail=_EXPIRED | {"message": "授权已到期，请输入新卡密"})
    card, err = _check_card_usable(key, machine)
    if err:
        raise err
    return {"card_key": key, "machine_code": machine, "expire_at": card["expire_at"]}
