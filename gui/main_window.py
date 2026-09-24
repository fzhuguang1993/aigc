"""
gui/main_window.py —— 主窗口：左侧导航 + 页面栈 + 状态栏，2 秒自动刷新
"""
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QCursor
from PySide6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
                               QListWidget, QStackedWidget, QLabel, QPushButton,
                               QMessageBox)

from utils.desktop_utils import open_path
from gui.pages_tasks import TasksPage
from gui.pages_records import RecordsPage
from gui.pages_settings import SettingsPage
from gui.pages_dashboard import DashboardPage
from gui.pages_console import ConsolePage
from gui.pages_products import ProductsPage
from gui.pages_risk import RiskPage
from gui.pages_tools import ToolsPage
from gui.pages_guide import GuidePage
from gui.pages_api import ApiManagerPage


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
        self.nav.addItems(["📋  任务中心", "🧩  产品中心", "📊  数据看板", "🕘  执行记录",
                           "📡  线路负载", "🛡  风控中心", "🧰  工具中心",
                           "📖  新手入门", "⚙️  设置"])
        slay.addWidget(self.nav, 1)
        self.lbl_health = QLabel()
        self.lbl_health.setObjectName("SideStatus")
        self.lbl_health.setWordWrap(True)
        # 灯不是只读装饰：点一下立刻逐条真实探活（后台线程，不卡界面）
        self.lbl_health.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.lbl_health.setToolTip(
            "点击重新检测全部线路（后台线程，几秒后刷新）\n"
            "🟢 探活接口已应答　🟡 服务在线但探活路径未实现（照样能提交）\n"
            "🔴 连不上/5xx（选线跳过它）　⚪ 还没测过")
        self.lbl_health.mousePressEvent = lambda e: self._recheck_health()
        self._checking = False
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
        # 接口管理：默认不进导航（页面在栈里备着），设置页口令验证通过后
        # reveal_api_page() 才把导航项补出来并直接跳入。导航行号与页面栈索引
        # 始终一一对应（currentRowChanged→setCurrentIndex 是直连的），所以
        # 它必须排在最后：前面 9 项不动，它固定落在行号/索引 9。
        self.page_api = ApiManagerPage()
        self._nav_api_row = None
        for p in (self.page_tasks, self.page_products, self.page_dashboard,
                  self.page_records, self.page_console, self.page_risk,
                  self.page_tools, self.page_guide, self.page_settings,
                  self.page_api):
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
        if self._checking:                # 检测中不被 2 秒定时刷打断
            return
        from registry.manager import ACCOUNTS, first_check_done
        from core.config import USER_NAME
        if not first_check_done():
            # 首轮探活还没回来：此刻的 healthy 只是默认值，不能当结果画成绿灯
            dots = "检测中…" if ACCOUNTS else "无线路"
        else:
            # 🟢 探活接口正常应答；🟡 服务有话回但探活路径未实现（能提交，但不谎称
            # “已测正常”）；🔴 连不上/5xx —— 三档共用 gui.header.line_light
            from gui.header import line_light
            dots = "  ".join(line_light(a, True)[0] for a in ACCOUNTS)
        self.lbl_health.setText(f"服务：{dots}\n当前用户：{USER_NAME or '未配置'}")

    def _recheck_health(self):
        """手动触发一轮真实检测：绿灯必须是测出来的，不是默认值"""
        if self._checking:
            return
        self._checking = True
        self.lbl_health.setText("服务：检测中…")

        def run():
            from registry.manager import check_all_accounts
            try:
                check_all_accounts()
            finally:
                # 回主线程复位（QTimer.singleShot 线程安全）
                QTimer.singleShot(0, self._recheck_done)
        threading.Thread(target=run, daemon=True, name="recheck").start()

    def _recheck_done(self):
        self._checking = False
        self._update_side_status()

    def reveal_api_page(self):
        """口令验证通过后由设置页调用：把「🔌 接口管理」补进导航并跳过去

        本次运行期内保持可见（再锁回去没意义：口令都给你了）；
        重启后恢复默认隐藏。"""
        if self._nav_api_row is None:
            self.nav.addItem("🔌  接口管理")
            self._nav_api_row = self.nav.count() - 1
        self.nav.setCurrentRow(self._nav_api_row)

    def open_output_dir(self):
        """打开（并自动创建）输出文件夹"""
        from core.config import DOWNLOAD_DIR
        try:
            d = Path(DOWNLOAD_DIR)
            d.mkdir(parents=True, exist_ok=True)
            open_path(d)
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
            kwargs = {}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            else:
                kwargs["start_new_session"] = True   # 脱离 GUI 终端，独立运行
            subprocess.Popen(cmd, cwd=str(RUNTIME_DIR), **kwargs)
        except Exception as e:
            QMessageBox.critical(self, "启动失败", str(e))
