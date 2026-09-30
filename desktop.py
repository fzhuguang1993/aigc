"""
desktop.py —— Windows 桌面版入口（GUI 模式，与命令行模式共用数据）
"""
import importlib
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from PySide6.QtGui import QIcon

import core.config as config


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
    app = QApplication(sys.argv)
    app.setApplicationName("AIGC工厂")
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
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
