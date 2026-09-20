"""
gui/main_window.py —— 主窗口：左侧导航 + 页面栈 + 状态栏，2 秒自动刷新
"""
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
                               QListWidget, QStackedWidget, QLabel, QPushButton,
                               QMessageBox)

from gui.pages_tasks import TasksPage
from gui.pages_records import RecordsPage
from gui.pages_settings import SettingsPage
from gui.pages_dashboard import DashboardPage
from gui.pages_console import ConsolePage
from gui.pages_products import ProductsPage
from gui.pages_risk import RiskPage
from gui.pages_tools import ToolsPage
from gui.pages_guide import GuidePage


def resource_path(rel):
    """资源文件路径：兼容开发模式与 PyInstaller 打包（sys._MEIPASS）"""
    base = getattr(sys, "_MEIPASS", None)
    candidates = []
    if base:
        candidates.append(Path(base) / rel)
    candidates.append(Path(__file__).resolve().parent.parent / rel)
    candidates.append(Path.cwd() / rel)
    for p in candidates:
        if p.exists():
            return str(p)
    return ""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AIGC 视频助手")
        self.resize(1280, 760)
        icon = resource_path("assets/app.ico")
        if icon:
            self.setWindowIcon(QIcon(icon))

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        body = QHBoxLayout()
        root.addLayout(body)

        # ----- 左侧导航 -----
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        slay = QVBoxLayout(sidebar)
        slay.setContentsMargins(6, 18, 6, 12)
        logo = QLabel("🎬 AIGC\n视频助手")
        logo.setObjectName("Logo")
        slay.addWidget(logo)
        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.addItems(["📋  任务中心", "🧩  产品中心", "📊  看板", "🕘  执行记录",
                           "📡  线路负载", "🛡  风控中心", "🧰  工具中心",
                           "📖  新手入门", "⚙️  设置"])
        slay.addWidget(self.nav, 1)
        self.lbl_health = QLabel()
        self.lbl_health.setObjectName("SideStatus")
        self.lbl_health.setWordWrap(True)
        slay.addWidget(self.lbl_health)
        body.addWidget(sidebar)

        # ----- 右侧页面 -----
        self.pages = QStackedWidget()
        self.page_tasks = TasksPage()
        self.page_products = ProductsPage()
        self.page_dashboard = DashboardPage()
        self.page_records = RecordsPage()
        self.page_console = ConsolePage()
        self.page_risk = RiskPage()
        self.page_tools = ToolsPage()
        self.page_guide = GuidePage()
        self.page_settings = SettingsPage()
        for p in (self.page_tasks, self.page_products, self.page_dashboard,
                  self.page_records, self.page_console, self.page_risk,
                  self.page_tools, self.page_guide, self.page_settings):
            self.pages.addWidget(p)
        body.addWidget(self.pages, 1)

        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

        # ----- 侧栏底部：输出文件夹 + 命令行模式入口 -----
        b_out = QPushButton("📂 输出文件夹")
        b_out.setObjectName("SideBtn")
        b_out.setToolTip("打开生成视频的存放目录（运行目录下 outputs/）")
        b_out.clicked.connect(self.open_output_dir)
        slay.addWidget(b_out)

        b_console = QPushButton("🖥 打开命令行窗口")
        b_console.setObjectName("SideBtn")
        b_console.setToolTip("在新窗口打开黑窗口模式（与界面共用同一数据库）")
        b_console.clicked.connect(self.open_console_mode)
        slay.addWidget(b_console)

        self.statusBar().showMessage("就绪 — 完成的视频会自动保存到运行目录 outputs/ 下")

        # ----- 定时刷新（当前页 + 侧栏健康状态）-----
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(2000)
        self._refresh()

    def _refresh(self):
        page = self.pages.currentWidget()
        if hasattr(page, "refresh"):
            page.refresh()
        self._update_side_status()

    def _update_side_status(self):
        from registry.manager import ACCOUNTS
        from core.config import USER_NAME
        dots = "  ".join("🟢" if a.healthy else "🔴" for a in ACCOUNTS)
        self.lbl_health.setText(f"服务：{dots or '无'}\n当前用户：{USER_NAME or '未配置'}")

    def open_output_dir(self):
        """打开（并自动创建）输出文件夹"""
        from core.config import DOWNLOAD_DIR
        try:
            d = Path(DOWNLOAD_DIR)
            d.mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))
        except Exception as e:
            QMessageBox.critical(self, "打开失败", str(e))

    def open_console_mode(self):
        """启动命令行（黑窗口）模式，与 GUI 共用 data/aigc.db"""
        from core.config import RUNTIME_DIR
        try:
            if getattr(sys, "frozen", False):
                exe = Path(sys.executable).with_name("AIGC视频助手-命令行.exe")
                if not exe.exists():
                    QMessageBox.information(
                        self, "提示",
                        "未找到「AIGC视频助手-命令行.exe」，\n"
                        "请把它与本程序放在同一目录（运行 build.bat 可同时打包两个版本）")
                    return
                cmd = [str(exe)]
            else:
                py = Path(sys.executable)
                python = py.with_name("python.exe")
                cmd = [str(python if python.exists() else py), "main.py"]
            subprocess.Popen(cmd, cwd=str(RUNTIME_DIR),
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception as e:
            QMessageBox.critical(self, "启动失败", str(e))
