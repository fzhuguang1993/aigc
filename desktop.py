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
    app.setApplicationName("AIGC视频助手")
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

    from store import db, risk_store
    db.init()
    risk_store.init_default()    # 风控表为空时种入广告法极限词通用政策

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
