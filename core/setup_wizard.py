"""
setup_wizard.py —— 首次运行配置向导
在程序运行目录下生成 config.json，之后运行直接读取，不再询问。
重新配置：删除 config.json 后再启动即可。
"""
import json
import os
from pathlib import Path

# 与 core.config 的 RUNTIME_DIR 保持一致（支持 AIGC_HOME 环境变量覆盖）
CONFIG_JSON = Path(os.environ.get("AIGC_HOME") or Path.cwd()) / "config.json"


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


def ensure_config():
    """config.json 不存在或损坏时，进入交互向导（开发环境有 config_local.py 则跳过）"""
    if CONFIG_JSON.exists():
        try:
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            if data.get("accounts"):
                return
        except Exception:
            print("⚠ config.json 已损坏，重新进入配置向导…")
    # 开发环境：core/config_local.py 存在则跳过向导
    try:
        from core import config_local  # noqa: F401
        if getattr(config_local, "ACCOUNTS", None):
            return
    except ImportError:
        pass

    print("=" * 52)
    print("  欢迎使用 AIGC 视频生成助手 —— 首次运行配置")
    print("  （配置将保存到当前目录 config.json，之后无需再填）")
    print("=" * 52)

    user_name = ""
    while not user_name:
        user_name = input("\n1) 请输入你的姓名（用于生成视频文件命名）: ").strip()

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

    data = {"user_name": user_name, "accounts": accounts}
    CONFIG_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print("\n" + "=" * 52)
    print(f"  ✓ 配置完成！已保存: {CONFIG_JSON}")
    print(f"    姓名: {user_name} | 接口数: {len(accounts)}")
    print("  如需重新配置，删除 config.json 后重新启动即可")
    print("=" * 52 + "\n")
