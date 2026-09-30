r"""
core/trial.py —— 试用版限时锁定（构建期开关 TRIAL_HOURS，日历口径：首次运行起 N 小时）

给「打包一个特别版给别人试用」用，与商用网关授权（core/license.py）互不相干：
- 是否启用、上限几小时，由构建期常量 core/config_local.py 的 TRIAL_HOURS 决定。
  它打包时编译进 exe，运行期改不了（改了要重新打包）；留空 / 0 / 不写 = 正式版行为，
  本模块整体放行，对现有版本零影响。
- 「首次运行时间 / 见过的最大时间戳」落盘到隐藏配置目录 CONFIG_DIR 下的 trial.json，
  并用内置密钥做 HMAC 签名防手改；同时镜像一份进 Windows 注册表（HKCU）作第二锚点，
  只删 json 回不到「全新未用」。
- 判定口径是自然日历：now - first_run >= TRIAL_HOURS 小时即到期。系统时间被往回拨
  （now 明显早于「见过的最大时间戳」）直接判定到期，堵「改系统时间续命」。

诚实边界：纯本地锁一定能被铁了心的人破（反编译改常量、json 和注册表一起删）。它劝退
普通试用者够用；要真正锁死请走服务端网关——core/license.py 已按机器码下发到期令牌。
"""
import hashlib
import hmac
import json
import time

from core.paths import CONFIG_DIR

#: 试用状态落盘位置（隐藏配置目录，随 paths.CONFIG_DIR 走，测试可用 AIGC_CONFIG_DIR 隔离）
TRIAL_FILE = CONFIG_DIR / "trial.json"
#: 签名密钥：打包进 exe。改了它＝旧 trial.json 全部作废（重新计首次运行）
_TRIAL_SECRET = b"aigc-trial-seal-v1::72h::3f9a2c7e1b8d4065"
#: 允许的时钟抖动（秒）：now 比「见过的最大时间戳」还早超过这个量，判为回拨
_ROLLBACK_SLACK = 120
#: 注册表第二锚点（best-effort，非 Windows 静默跳过）
_REG_KEY = r"Software\AIGC视频助手\Trial"


# ---------------- 构建期开关 ----------------

def trial_hours():
    """读取 TRIAL_HOURS（构建期常量）。没配 / 非法 → 0＝不启用试用锁定。"""
    try:
        from core.config_local import TRIAL_HOURS as h
    except (ImportError, AttributeError, ValueError):
        return 0
    try:
        return max(0, int(h))
    except (TypeError, ValueError):
        return 0


def trial_enabled():
    return trial_hours() > 0


# ---------------- 签名 / 读写 ----------------

def _sign(first_run, max_seen):
    body = f"{int(first_run)}|{int(max_seen)}".encode("utf-8")
    return hmac.new(_TRIAL_SECRET, body, hashlib.sha256).hexdigest()[:32]


def _seal(first_run, max_seen):
    return {"first_run": int(first_run), "max_seen": int(max_seen),
            "sig": _sign(first_run, max_seen)}


def _read_json():
    try:
        d = json.loads(TRIAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    try:
        first, seen, sig = int(d["first_run"]), int(d["max_seen"]), str(d["sig"])
    except (KeyError, TypeError, ValueError):
        return None
    # 签名对不上＝被手改过，整份作废（宁可当作缺失走注册表锚点）
    if not hmac.compare_digest(sig, _sign(first, seen)):
        return None
    return {"first_run": first, "max_seen": seen}


def _write_json(first, seen):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TRIAL_FILE.write_text(json.dumps(_seal(first, seen)), encoding="utf-8")
    except OSError:
        pass


def _read_reg():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
            first = int(winreg.QueryValueEx(k, "FirstRun")[0])
            seen = int(winreg.QueryValueEx(k, "MaxSeen")[0])
        return {"first_run": first, "max_seen": seen}
    except Exception:
        return None


def _write_reg(first, seen):
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
            winreg.SetValueEx(k, "FirstRun", 0, winreg.REG_QWORD, int(first))
            winreg.SetValueEx(k, "MaxSeen", 0, winreg.REG_QWORD, int(seen))
    except Exception:
        pass


def _sources():
    return [s for s in (_read_json(), _read_reg()) if s]


# ---------------- 判定 ----------------

def check_and_persist():
    """跑一次试用校验并把时间戳落盘。返回 (ok, message, remaining_seconds)。

    ok=False 时 message 是人话到期原因，调用方（desktop）据此弹窗退出。
    未启用试用（TRIAL_HOURS<=0）→ 直接 (True, "", 0) 放行。
    """
    hours = trial_hours()
    if hours <= 0:
        return True, "", 0
    limit = hours * 3600
    now = int(time.time())
    srcs = _sources()
    if srcs:
        first = min(s["first_run"] for s in srcs)     # 最早的首次运行＝真·第一次
        max_seen = max(s["max_seen"] for s in srcs)   # 见过的最大时间戳，单调不回退
    else:
        first = max_seen = now                         # 全新首次运行
    # 先落盘再判定：first 恒定、max_seen 只增不减（改小系统时间也追不回来）
    _write_json(first, max(max_seen, now))
    _write_reg(first, max(max_seen, now))
    if now + _ROLLBACK_SLACK < max_seen:
        return False, "检测到系统时间被往回拨，试用期已提前结束。", 0
    elapsed = now - first
    if elapsed >= limit:
        return False, f"本试用版自首次运行起 {hours} 小时已到期，感谢体验。", 0
    return True, "", limit - elapsed


def remaining_seconds():
    """剩余试用秒数（未启用返回 0，供界面显示倒计时提示）。"""
    ok, _msg, remain = check_and_persist()
    return remain if ok else 0
