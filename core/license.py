r"""
core/license.py —— 商用激活体系（卡密激活 / 机器码 / 令牌缓存 / 防离线滥用）

分工与信任边界（商用改造第一阶段，详见 docs 计划）：
- 一切"是不是正版"的最终裁决在服务端（/auth/activate、/auth/verify）；
- 本地 license.json 只是缓存：让有网的正常用户不必每次输卡密，
  断网时按离线宽限撑几天。客户端拦不住改包的人——真正值钱的
  上游地址/密钥在第二阶段网关化后已全部收进服务端，破解版没有
  令牌就打不动网关，本地校验只负责劝退与体验。

关键行为：
- 机器码：Windows MachineGuid + 物理 MAC 哈希，同机恒定、重装大版本才变；
- 网关地址：core/config_local.py 的 GATEWAY_BASE（打包进 exe，不进仓库）。
  没配 = 开发/内网直连模式，本模块整体放行（老行为零变化）；
- 校验节奏：启动必查一次联网 verify；此后每 24h 一次；
  联网失败时按缓存到期时间继续放行，最多 OFFLINE_GRACE 天，
  超宽限必须联网成功一次才恢复（服务端过期/封禁即时生效的通道）。
"""
import hashlib
import json
import os
import secrets
import socket
import sys
import time

from core.paths import CONFIG_DIR

LICENSE_FILE = CONFIG_DIR / "license.json"
VERIFY_INTERVAL = 24 * 3600        # 每 24 小时联网复核一次
OFFLINE_GRACE_DEFAULT = 3 * 86400  # 断网宽限兜底值（服务端响应可覆盖）
NET_ERROR = "offline"              # verify 返回值：网络问题，按缓存判定

_MACHINE = None
_CACHE = None
_loaded = False


# ---------------- 服务器地址 ----------------

def _gateway_base():
    """GATEWAY_BASE：网关根地址（http(s)://host[:port]）。未配置返回空串。"""
    try:
        from core.config_local import GATEWAY_BASE as _G
    except (ImportError, AttributeError):
        return ""
    return str(_G or "").strip().rstrip("/")


def gateway_root():
    """去掉可能带上的 /api/v1 尾巴：/auth/* 挂在站点根上"""
    base = _gateway_base()
    if base.endswith("/api/v1"):
        base = base[: -len("/api/v1")]
    return base.rstrip("/")


def gateway_api_base():
    """"/api/v1" 形式；配置里没写自动补，与账号 base 归一化同规矩。"""
    root = gateway_root()
    if root and not root.endswith("/api/v1"):
        root += "/api/v1"
    return root


def gateway_mode():
    """商用网关模式：配了 GATEWAY_BASE 才启用激活/鉴权；否则维持开发直连。"""
    return bool(gateway_root())


# ---------------- 机器码 ----------------

def _win_machine_guid():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as k:
            return str(winreg.QueryValueEx(k, "MachineGuid")[0])
    except Exception:
        return ""                    # 非 Windows/注册表读不到：回退 MAC+主机名


def machine_code():
    """稳定机器码（进程内算一次）：MachineGuid+MAC+主机名 → 32 位大写十六进制。

    不把用户名编进去：同一台电脑换登录账号不该触发"卡密在别的电脑使用"。
    """
    global _MACHINE
    if _MACHINE is None:
        parts = [_win_machine_guid(),
                 "{:012X}".format(uuid_getnode_safe()),
                 socket.gethostname()]
        raw = "|".join(p for p in parts if p)
        _MACHINE = hashlib.sha256(("aigc-gate-v1|" + raw).encode("utf-8")) \
            .hexdigest()[:32].upper()
    return _MACHINE


def uuid_getnode_safe():
    try:
        import uuid
        return uuid.getnode()
    except Exception:
        return 0


# ---------------- 本地缓存 ----------------

def _load():
    global _CACHE, _loaded
    if not _loaded:
        _loaded = True
        try:
            data = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
            _CACHE = data if isinstance(data, dict) else None
        except Exception:
            _CACHE = None
    return _CACHE


def _save(data):
    global _CACHE, _loaded
    _CACHE, _loaded = data, True
    LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LICENSE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, LICENSE_FILE)                 # 原子落盘，断电不留半截


def clear_cache():
    """测试/重新激活用"""
    global _CACHE, _loaded
    _CACHE, _loaded = None, True


def _detail(resp, fallback):
    """FastAPI 的 detail 可能是 {reason,message} 结构，统一压成一句话"""
    try:
        d = resp.json().get("detail")
    except Exception:
        d = None
    if isinstance(d, dict):
        return d.get("message") or d.get("reason") or fallback, d.get("reason", "")
    if isinstance(d, str) and d:
        return d, ""
    return fallback, ""


# ---------------- 激活 / 校验 ----------------

def activate(card_key, server=None, timeout=(10, 20)):
    """用卡密激活本机。成功返回 (True, 信息 dict)，失败 (False, 一句话原因)。

    服务端幂等：同机重复输同一张卡不会叠天数，只会重发令牌；
    续费 = 直接输一张新的未用卡，到期时间自动叠加。"""
    root = server or gateway_root()
    if not root:
        return False, "未配置激活服务器地址（GATEWAY_BASE）"
    card_key = str(card_key or "").strip()
    if not card_key:
        return False, "请输入卡密"
    import requests
    try:
        r = requests.post(f"{root}/auth/activate", timeout=timeout,
                          json={"card_key": card_key,
                                "machine_code": machine_code(),
                                "app_version": _app_version()})
    except requests.RequestException as e:
        return False, f"无法连接激活服务器：{type(e).__name__}"
    if r.status_code != 200:
        msg, _reason = _detail(r, f"激活失败（HTTP {r.status_code}）")
        return False, msg
    info = r.json()
    _save({"card_key": info.get("card_key", ""),
           "machine": machine_code(),
           "token": info.get("token", ""),
           "expire_at": int(info.get("expire_at") or 0),
           "last_verify": int(time.time()),
           "offline_grace": int(info.get("offline_grace_days", 3)) * 86400})
    return True, info


def verify_online(timeout=(10, 15)):
    """联网复核：返回 (state, info)。

    state："ok" 有效（info 带最新 expire_at/days_remaining）
          "expired" 已到期（该弹激活窗）
          "invalid" 令牌作废/机器码不符/被封禁
          "offline" 网络不通——调用方看 local_valid() 决定是否宽限
    """
    root = gateway_root()
    if not root:
        return "ok", {}
    lic = _load()
    if not lic or not lic.get("token"):
        return "invalid", {"message": "尚未激活"}
    import requests
    try:
        r = requests.post(f"{root}/auth/verify", timeout=timeout,
                          json={"machine_code": machine_code(),
                                "token": lic["token"]})
    except requests.RequestException:
        return NET_ERROR, {}
    if r.status_code == 200:
        info = r.json()
        lic["expire_at"] = int(info.get("expire_at") or lic.get("expire_at", 0))
        lic["last_verify"] = int(time.time())
        _save(lic)
        return "ok", info
    msg, reason = _detail(r, f"校验失败（HTTP {r.status_code}）")
    if r.status_code == 403 and reason in ("expired",):
        return "expired", {"message": msg}
    if r.status_code == 403:
        return "invalid", {"message": msg}       # 封禁
    if r.status_code == 401:
        return "expired" if not _local_unexpired() else "invalid", {"message": msg}
    return NET_ERROR, {"message": msg}


def local_valid():
    """不联网的本机判定：有令牌且没到过期时间。"""
    return bool(_load() and _local_unexpired())


def _local_unexpired():
    lic = _load() or {}
    return int(lic.get("expire_at") or 0) > int(time.time())


def offline_ok():
    """verify 走网络失败时：缓存没过期、或过期还在宽限期内，都算可继续用。"""
    lic = _load()
    if not lic or not lic.get("token"):
        return False
    grace = int(lic.get("offline_grace") or OFFLINE_GRACE_DEFAULT)
    return int(lic.get("expire_at") or 0) + grace > int(time.time())


def needs_recheck():
    """距上次成功联网校验是否已超过 VERIFY_INTERVAL"""
    lic = _load() or {}
    return int(time.time()) - int(lic.get("last_verify") or 0) > VERIFY_INTERVAL


def check_on_startup():
    """启动/定时复核的统一入口（GUI/控制台共用一条口径）。

    返回 (True, info)=可用；(False, 人话原因)=需要激活。
    网关未配置时直接放行（开发模式）。"""
    if not gateway_mode():
        return True, {"mode": "dev"}
    lic = _load()
    if not lic or not lic.get("token"):
        return False, "未激活：请输入卡密"
    state, info = verify_online()
    if state == "ok":
        return True, info
    if state == NET_ERROR:
        if offline_ok():
            return True, {"mode": "offline", **info}
        return False, "授权已过期，且超过离线宽限期——联网后输入新卡密"
    return False, info.get("message") or ("授权已到期" if state == "expired" else "授权无效")


def days_remaining():
    lic = _load() or {}
    left = int(lic.get("expire_at") or 0) - int(time.time())
    return max(0, (left + 86399) // 86400) if left > 0 else 0


def expire_at():
    return int((_load() or {}).get("expire_at") or 0)


def has_token():
    lic = _load() or {}
    return bool(lic.get("token"))


# ---------------- 网关鉴权头 ----------------

def _app_version():
    try:
        from core.config import APP_VERSION       # 可选常量，没定义也不影响激活
        return str(APP_VERSION)
    except Exception:
        return "dev"


def request_headers():
    """api_client / 素材提取 / 翻译共用的鉴权头（四件套，防重放）。

    非网关模式返回空 dict——直连内网线路时一个头都不多加。
    """
    if not gateway_mode():
        return {}
    lic = _load() or {}
    if not lic.get("token"):
        return {}
    return {"X-License": lic["token"],
            "X-Machine": machine_code(),
            "X-Ts": str(int(time.time())),
            "X-Nonce": secrets.token_hex(8)}
