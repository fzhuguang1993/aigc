"""
gui/tray.py —— Windows 系统托盘：关闭收进托盘 + 开机自启 + 快捷菜单

三个设计取舍：
1. 点 ✕ 默认不退程序、收进托盘（可在设置里改回"直接退出"）：这类工具软件
   常年开着，误关一次就要重新摆好窗口和上下文；但**第一次**收进托盘必须
   气泡说明去哪找回——否则用户以为软件"闪退了"，其实还在后台跑。
2. 托盘菜单一定要有「退出」：窗口收进托盘后任务栏没入口，托盘是唯一出口；
   而 Windows 会把不常用的托盘图标折进溢出区（小箭头里），气泡文案得带上这句。
3. 开机自启写 HKCU\\...\\Run（当前用户级），不写 HKLM：装到 Program Files 是
   机器级的，但自启只该给当前用户，HKLM 还要管理员权限、卸载易残留。

隐藏到托盘时 `setQuitOnLastWindowClosed(False)`：否则关掉最后一个工具小窗，
Qt 认为"没窗口了"直接把整个程序带走了（主窗口是 hide 不是 close，不算窗口）。
"""
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from store import app_state

#: 界面偏好键（ui_state.json）
CLOSE_TO_TRAY_KEY = "close_to_tray"          # 点 ✕ → 收进托盘（默认开）
MINIMIZE_TO_TRAY_KEY = "minimize_to_tray"    # 点最小化 → 也收进托盘（默认关）
AUTOSTART_KEY = "autostart_enabled"          # 开机自启开关的本地记账
TIP_SHOWN_KEY = "tray_tip_shown"             # 首次收进托盘只提示一次

#: 注册表自启值名（HKCU\Software\Microsoft\Windows\CurrentVersion\Run）
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "AIGC视频助手"


def tray_supported():
    return sys.platform.startswith("win")


def autostart_command():
    """自启命令行：带 --minimized，开机后直接待在托盘，不糊用户一屏窗口"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    desktop = Path(__file__).resolve().parents[1] / "desktop.py"
    return f'"{sys.executable}" "{desktop}" --minimized'


def read_autostart():
    """读注册表里的自启项是否指向本程序（被别的版本占用也算开着）"""
    if not tray_supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, RUN_VALUE)
        return bool(val)
    except OSError:
        return False


def write_autostart(on):
    """写/删自启项；返回 (是否成功, 说明文案)，失败由界面如实显示。

    注册表这一层出错通常是无权限或键被安全软件拦下，不能只回一个 False
    让用户猜——把 winreg 的原始错误带回去。"""
    if not tray_supported():
        return False, "仅 Windows 支持开机自启"
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                           winreg.KEY_SET_VALUE)
        with k:
            if on:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ,
                                  autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass        # 本来就没有：关掉＝已是目标状态，不报错
        app_state.set_value(AUTOSTART_KEY, bool(on))
        return True, ""
    except OSError as e:
        return False, f"写入注册表失败：{e}"


class TrayController(QObject):
    """托盘图标与其菜单/动作；只负责"显示/隐藏/退出/开工具"四类请求，
    具体做什么由主窗口接信号决定（保持本模块可单测）。"""

    show_requested = Signal()
    quit_requested = Signal()
    tool_requested = Signal(str)
    launcher_requested = Signal()

    def __init__(self, window, icon_path="", parent=None):
        super().__init__(parent or window)
        self._win = window
        self._icon_path = icon_path or ""
        self._tray = None
        self._menu = None
        self._tool_actions = []

    # ---------- 能力与开关 ----------
    def available(self):
        """系统托盘可用（图标可显示）。用户关掉"关闭进托盘"仍然要有托盘图标
        用来收回窗口，所以可用性只看平台，不看偏好开关。"""
        if not tray_supported():
            return False
        return QSystemTrayIcon.isSystemTrayAvailable()

    def close_to_tray(self):
        """点 ✕ 是否收进托盘：偏好默认开；托盘不可用（资源管理器被换掉等）时
        自动按"直接退出"处理，否则窗口一关程序就再也找不回了。"""
        if not self.available():
            return False
        return bool(app_state.get(CLOSE_TO_TRAY_KEY, True))

    def minimize_to_tray(self):
        return bool(self.available() and app_state.get(MINIMIZE_TO_TRAY_KEY, False))

    # ---------- 装配 ----------
    def install(self):
        """建托盘图标并接好信号；托盘不可用时返回 False（调用方按原样退出）"""
        if self._tray is not None:
            self._rebuild_menu()
            return True
        if not self.available():
            return False
        app = QApplication.instance()
        icon = QIcon(self._icon_path) if self._icon_path else \
            (app.windowIcon() if app is not None else QIcon())
        if icon.isNull():
            return False
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip(self._tooltip())
        self._menu = QMenu()
        self._rebuild_menu()
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.show()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)    # 见模块头第 3 段
        return True

    def _tooltip(self):
        from core.config import APP_VERSION
        return f"🎬 AIGC 工厂 v{APP_VERSION}｜双击打开主界面，右键看工具菜单"

    def _rebuild_menu(self):
        """菜单项：主界面 / 唤出工具搜索 / 固定工具 / 开机自启 / 退出。

        「固定工具」直接列在托盘菜单里，是为了不打开主界面就能一键到位；
        数据源与左侧导航固定项同一份（tools_registry.load_pinned）。"""
        if self._menu is None:
            return
        self._menu.clear()
        self._tool_actions = []
        self._menu.addAction("🖥  打开主界面", lambda: self.show_requested.emit())
        self._menu.addAction("🔍  唤出工具搜索", lambda: self.launcher_requested.emit())
        self._menu.addSeparator()
        from gui.tools_registry import load_pinned
        pinned = load_pinned()
        if pinned:
            sub = self._menu.addMenu("🧰  固定工具")
            for name in pinned:
                self._tool_actions.append(
                    sub.addAction(name, lambda n=name: self.tool_requested.emit(n)))
        else:
            act = self._menu.addAction("🧰  固定工具（还没有）")
            act.setEnabled(False)
        self._menu.addSeparator()
        auto = self._menu.addAction("🚀  开机自启")
        auto.setCheckable(True)
        auto.setChecked(read_autostart())
        auto.triggered.connect(self._toggle_autostart)
        self._menu.addAction("❌  退出程序", lambda: self.quit_requested.emit())

    def _toggle_autostart(self, checked):
        ok, msg = write_autostart(checked)
        if not ok:
            self.notify("开机自启设置失败", msg)
        self._rebuild_menu()

    def refresh_menu(self):
        """菜单只在装配时建一次：设置页改了自启/固定工具后叫一声同步过来"""
        self._rebuild_menu()

    # ---------- 图标动作 ----------
    def _on_activated(self, reason):
        R = QSystemTrayIcon.ActivationReason
        if reason in (R.Trigger, R.DoubleClick):
            # 单击/双击都当作"要用了"：窗口在眼前就收起，不在就拉出来
            if self._win.isVisible() and not self._win.isMinimized() \
                    and self._win.isActiveWindow():
                self.hide_window()
            else:
                self.show_window()

    def show_window(self):
        self._win.showNormal()
        self._win.raise_()
        self._win.activateWindow()

    def hide_window(self):
        self._win.hide()

    def notify(self, title, msg, ms=6000):
        """气泡提示；托盘不可用（或用户关了通知）就安静跳过，不能改用
        QMessageBox 拦路——提示一次而已，把工具流程挡住就成骚扰了。"""
        if self._tray is None or not QSystemTrayIcon.supportsMessages():
            return
        self._tray.showMessage(title, msg, self._tray.icon(), ms)

    def notify_first_hide(self):
        """首次收进托盘的指路气泡（只发一次，之后用户已知去向）"""
        if app_state.get(TIP_SHOWN_KEY):
            return
        app_state.set_value(TIP_SHOWN_KEY, True)
        self.notify("已最小化到托盘",
                    "程序继续在后台运行。想再打开：双击右下角图标；"
                    "找不到图标就点任务栏右下角的 ▲（被折叠进小箭头里了）。\n"
                    "想改回「点关闭直接退出」：设置 → 托盘与全局快捷键。")

    def set_visible(self, on):
        if self._tray is None:
            return
        self._tray.setVisible(bool(on))

    def remove(self):
        """退出前摘掉图标：否则进程结束而图标留在托盘上，点一下报错（Windows 通病）"""
        if self._tray is not None:
            self._tray.setVisible(False)
            self._tray = None
