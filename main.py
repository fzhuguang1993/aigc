"""
main.py —— 入口
"""
import sys
import threading
from datetime import datetime

# 首次运行：交互式生成 config.json（姓名 + API 接口），必须在加载配置前执行
from core.setup_wizard import ensure_config
ensure_config()


def _unlock_admin():
    """救急通道：管理员忘密码时命令行重置（防组织登录把整机锁死）。
    放在卡密校验之前调用：授权过期也要能进得来。"""
    from store import db as _db, org_store
    _db.init()
    members = org_store.list_members()
    if not members:
        print("组织未启用：库里没有任何成员，无需解锁（GUI 可直接进）")
        return
    print("成员列表：")
    for m in members:
        tag = "启用" if m["active"] else "已停用"
        dept = " · " + m["dept"] if m["dept"] else ""
        role = org_store.ROLES.get(m["role"], m["role"])
        print("  %s（%s%s · %s）" % (m["name"], role, dept, tag))
    name = input("输入要重置密码的姓名（留空取消）: ").strip()
    if not name:
        print("已取消")
        return
    if not org_store.get_member(name):
        print("成员不存在: " + name)
        return
    if input("确认为「%s」设置新密码？(y/N): " % name).strip().lower() != "y":
        print("已取消")
        return
    err = org_store.set_password(name, input("新密码（至少 4 位）: "))
    if err:
        print("失败: " + err)
    else:
        print("「%s」密码已重置，登录锁定同时解除" % name)


if "--unlock-admin" in sys.argv[1:]:
    _unlock_admin()
    sys.exit(0)

# 商用网关模式：命令行入口同样校验激活（纯文本输入卡密；开发直连自动放行）
from core import license as _lic
if _lic.gateway_mode():
    _ok, _info = _lic.check_on_startup()
    while not _ok:
        print(f"⚠ {_info}")
        _card = input("请输入卡密（空行退出软件）: ").strip()
        if not _card:
            sys.exit(0)
        _ok, _res = _lic.activate(_card)
        if _ok:
            print(f"✅ 激活成功，剩余约 {_res.get('days_remaining')} 天")
        else:
            _info = _res

from core.config import ACCOUNTS, DOWNLOAD_DIR, DEFAULT_DURATION, USER_NAME
from core.logger import raw_info, raw_warning
from store import db
from registry.manager import health_monitor_worker
from workers.poll import start_poll_threads
from console.app import interactive_loop

# 当前视频时长（秒）
current_duration = DEFAULT_DURATION


def main():
    raw_info(f"启动 {datetime.now().isoformat()}")
    db.init()
    
    # 汇总启动信息为极简格式
    info_parts = [
        f"姓名={USER_NAME or '未配置'}",
        f"数据库={'data/aigc.db'}",
        f"账号={'/'.join([a['name'] for a in ACCOUNTS])}",
        f"下载={DOWNLOAD_DIR.split('/')[-1]}",
        f"时长={current_duration}秒"
    ]
    raw_info(" | ".join(info_parts))
    
    # 账号健康状态汇总
    health_status = []
    for a in ACCOUNTS:
        try:
            from core.api_client import health
            h = health(a["base"])
            status = "OK" if all(v == 'ready' for v in h.values()) else "FAIL"
            device = h.get('device', 'unknown').split()[0]
            health_status.append(f"{a['name']}={status} [{device}]")
        except Exception as e:
            health_status.append(f"{a['name']}=ERR")
    
    raw_info("服务：" + "; ".join(health_status))

    threading.Thread(target=health_monitor_worker, daemon=True,
                     name="health").start()
    raw_info("健康检查线程已启动")

    start_poll_threads()
    raw_info("轮询线程已启动")

    interactive_loop()


if __name__ == "__main__":
    main()
