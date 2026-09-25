"""
desktop.py —— Windows 桌面版入口（GUI 模式，与命令行模式共用数据）
"""
import importlib
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog
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

    from store import db
    db.init()

    # 组织结构启用后必须先登录（org 表要等 db.init() 建好才能查）；
    # 关闭/取消登录框＝退出应用，登录态不落盘——每次启动都要登录
    from gui.dialogs_login import require_login
    if not require_login():
        sys.exit(0)

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
