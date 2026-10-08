"""
desktop.py —— Windows 桌面版入口（GUI 模式，与命令行模式共用数据）

启动参数：
  --minimized / --tray   启动后直接待在托盘里（开机自启用的就是它）；
                         没配好/没激活时该弹的向导照旧弹，不会“静默启动了一个不能用”的程序。
"""
import importlib
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from PySide6.QtGui import QIcon

import core.config as config

#: 启动后直接进托盘的参数（开机自启注册表写的就是它）
TRAY_FLAGS = ("--minimized", "--tray")


def pop_tray_flag(argv=None):
    """从 argv 里取走托盘旗标，返回是否带了。

    必须取走再交给 QApplication：Qt 会把不认识的参数当自己的选项解析，
    轻则刷一屏 QCoreApplication::unknownArgument 告警，重则弹一个错误框。"""
    argv = sys.argv if argv is None else argv
    hit = any(a in TRAY_FLAGS for a in argv)
    for f in TRAY_FLAGS:
        while f in argv:
            argv.remove(f)
    return hit


def _icon_path():
    base = getattr(sys, "_MEIPASS", None)
    candidates = [Path(base) / "assets" / "app.ico"] if base else []
    candidates += [Path(__file__).resolve().parent / "assets" / "app.ico",
                   Path.cwd() / "assets" / "app.ico"]
    for p in candidates:
        if p.exists():
            return str(p)
    return ""


def main():
    minimized = pop_tray_flag()
    # 崩溃留痕：必须赶在 QApplication 与任何线程之前装。这次"反复崩溃"就是因为
    # native 层一死什么都不留（只有 Qt 的 qFatal → abort），只能去 %LOCALAPPDATA%
    # \CrashDumps 用 cdb 解 dump 才看到真凶。装上后 logs/crash.log + aigc.log
    # 里会分别留下 Python 栈与 Qt 原话。详见 core/crashdump.py。
    from core import crashdump
    crashdump.enable()

    app = QApplication(sys.argv)
    app.setApplicationName("AIGC工厂")

    # 单实例守护：托盘常驻 + 系统热键的形态下，第二个实例抢不到键位还会多跑
    # 一份轮询线程，所以“已经开着”就是错：把已开的那个叫到前台，自己退出。
    # 赶在任何对话框之前判：不能先弹完协议/登录框才发现“其实已经有一个在跑”。
    from core import single_instance
    if not single_instance.acquire():
        single_instance.activate_existing()
        sys.exit(0)

    from gui.theme import apply_theme
    apply_theme(app)
    icon = _icon_path()
    if icon:
        app.setWindowIcon(QIcon(icon))   # 任务栏/对话框统一图标

    # 用户协议：首次运行（或条款大版本变更后）10 秒倒计时读完才能同意，拒绝即退出
    from gui.dialogs_eula import ensure_accepted
    if not ensure_accepted():
        sys.exit(0)

    # 无配置 → 先弹首配窗口；registry 在 import 时读取 ACCOUNTS，因此必须延后导入
    if not config.CONFIG_JSON.exists():
        from gui.first_run import FirstRunDialog
        dlg = FirstRunDialog()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)
        importlib.reload(config)

    # 商用网关模式：激活校验通过才进主窗口（开发直连模式自动放行）
    from gui.dialogs_license import ensure_valid
    if not ensure_valid():
        sys.exit(0)

    # 试用版限时锁定：仅当打包时写了 TRIAL_HOURS 才生效（正式版=0 整体放行，零影响）。
    # 口径是「首次运行起 N 小时」的自然日历，到期弹窗退出；trial.json + 注册表双锚点
    # 防手改 / 防删，见过最大时间戳单调不回退防改系统时间续命。详见 core/trial.py。
    from core import trial
    if trial.trial_enabled():
        ok, msg, _remain = trial.check_and_persist()
        if not ok:
            QMessageBox.warning(None, "试用已结束", msg)
            sys.exit(0)

    from store import db
    db.init()

    # 默认不再强制登录框：直接进主窗口。顶栏右侧显示当前用户，没登录就显示
    # 「未登录」，需要登录/换人时点「🔄 切换用户」现场登录（登录态仍不落盘）。
    # 未登录时数据按「不限」处理（等同 admin 可见范围），登录后再按角色圈定；
    # 单机模式（未启用组织）本就没有登录概念。

    from registry.manager import health_monitor_worker
    from workers.poll import start_poll_threads
    threading.Thread(target=health_monitor_worker, daemon=True, name="health").start()
    start_poll_threads()

    from gui import log_sink
    log_sink.install()

    from gui.main_window import MainWindow
    win = MainWindow(start_minimized=minimized)
    if not minimized:
        win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
