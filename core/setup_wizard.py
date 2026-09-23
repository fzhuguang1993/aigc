r"""
setup_wizard.py —— 首次运行配置向导
在隐藏配置家 CONFIG_DIR（默认 %APPDATA%\AIGC视频助手）生成 config.json，之后直接读取。
重新配置：删除该 config.json 后再启动即可。
"""
import json
import sys

from core.paths import CONFIG_DIR, ensure_config_home

ensure_config_home()                     # 保证隐藏配置家已建好（幂等）
CONFIG_JSON = CONFIG_DIR / "config.json"   # 与 core.config.CONFIG_JSON 同一把尺子


def _normalize_base(url):
    """补齐 /api/v1 后缀，去掉多余斜杠"""
    url = url.strip().rstrip("/")
    if not url.endswith("/api/v1"):
        url = url + "/api/v1"
    return url


def _missing_port(base):
    """判断 URL 的 host 部分是否漏填端口（默认 80 几乎连不上，典型错误）"""
    try:
        host = base.split("//", 1)[1].split("/")[0]
    except IndexError:
        return False
    return ":" not in host


def _check_api(base):
    """快速检测接口是否可连通（5 秒超时，失败不阻断）"""
    try:
        import requests
        r = requests.get(f"{base}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def _freeze_accounts(defaults):
    """内置线路要不要抄进 config.json。

    开发机（非打包）＋有内置线路时返回 False：config_local.py 才是凭证的
    数据源，抄一份进 config.json 后，以后改 config_local.py 会被优先级
    更高的 config.json 盖住（维护机现场踩过）。只记姓名，线路每次启动现读内置。"""
    if not defaults:
        return True                       # 使用者自己粘的地址必须落盘
    return bool(getattr(sys, "frozen", False))


def save_first_config(user_name, accounts, freeze_accounts=True):
    """写 config.json（先读旧内容合并，不丢其它字段）；返回写入路径。

    首配弹窗与命令行向导共用这一份，避免两边写出不同格式的 config.json。"""
    data = {}
    if CONFIG_JSON.exists():
        try:
            loaded = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    data["user_name"] = user_name
    if freeze_accounts:
        data["accounts"] = accounts
    else:
        data.pop("accounts", None)
    CONFIG_JSON.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return CONFIG_JSON


def _builtin_defaults():
    """内置默认：core/config_local.py 里的 ACCOUNTS（打包时编进 exe）。

    有内置值时向导只问姓名，同事不该背接口地址。
    口径与 core.config.defaults_accounts 一致（向导赶在 config.json 之前跑，
    不能 import core.config，两份改动要同步）。"""
    try:
        from core.config_local import ACCOUNTS as _A
    except (ImportError, AttributeError):
        return []
    out = []
    for i, a in enumerate(list(_A)):
        a = a if isinstance(a, dict) else {"base": a}
        base = str(a.get("base") or "").strip()
        if not base:
            continue
        try:
            conc = int(a.get("concurrency") or 1)
        except (TypeError, ValueError):
            conc = 1
        out.append({"name": str(a.get("name") or f"acc{i + 1}"),
                    "base": _normalize_base(base),
                    "concurrency": max(conc, 1)})
    return out


def ensure_config():
    """config.json 不存在或损坏时，进入交互向导（开发环境有 config_local.py 则跳过）"""
    defaults = _builtin_defaults()
    if CONFIG_JSON.exists():
        try:
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            # 开发机只存姓名（线路每次现读内置），所以“没 accounts”不等于
            # 没配好——否则 CLI 每次启动都要重问一遍姓名
            if data.get("user_name") and (data.get("accounts") or defaults):
                return
        except Exception:
            print("⚠ config.json 已损坏，重新进入配置向导…")

    print("=" * 52)
    print("  欢迎使用 AIGC 视频生成助手 —— 首次运行配置")
    print(f"  （配置将保存到 {CONFIG_JSON}，之后无需再填）")
    print("=" * 52)

    user_name = ""
    while not user_name:
        user_name = input("\n1) 请输入你的姓名（用于生成视频文件命名）: ").strip()

    if defaults:
        accounts = defaults
        print(f"\n2) 接口地址已由维护人内置（共 {len(accounts)} 条线路），无需填写。")
    else:
        print("\n2) 请输入 API 服务地址（可以有多个，逐个输入，输入空行结束）")
        print("   示例: http://106.75.1.98:7860  或  https://xxx.pod.compshare.cn")
        accounts = []
        while True:
            url = input(f"   接口 {len(accounts) + 1}（空行完成）: ").strip()
            if not url:
                if accounts:
                    break
                print("   ⚠ 至少需要配置一个接口地址")
                continue
            if not url.startswith("http"):
                url = "http://" + url
            base = _normalize_base(url)
            ok = _check_api(base)
            tip = "✓ 连通正常" if ok else "⚠ 暂时无法连通（已保存，可稍后检查网络或地址）"
            if not ok and _missing_port(base):
                tip += ("\n     ✖ 地址未填端口（会默认走 80，几乎必连不通），"
                        "API 服务通常是 7860 端口，如：http://192.168.0.1:7860")
            print(f"   已添加: {base}  [{tip}]")
            accounts.append({"name": f"acc{len(accounts) + 1}",
                             "base": base, "concurrency": 1})

    path = save_first_config(user_name, accounts, freeze_accounts=_freeze_accounts(defaults))
    print("\n" + "=" * 52)
    print(f"  ✓ 配置完成！已保存: {path}")
    print(f"    姓名: {user_name} | 接口数: {len(accounts)}")
    print("  如需重新配置，删除 config.json 后重新启动即可")
    print("=" * 52 + "\n")
