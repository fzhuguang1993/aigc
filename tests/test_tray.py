"""
tests/test_tray.py —— 托盘行为与开机自启（装进 Program Files 后的两条命门）

winreg 用假对象顶掉（注入 sys.modules）：单测绝不写真机注册表，但
「写失败要把原因带回去、勾选得退回」这类契约必须钉住——开机自启是最容易
静默失败的一项，安全软件拦一下用户完全看不出来。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication          # 只要 QApplication 在场

from gui import tray
from store import app_state


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """偏好文件指到临时目录：用例之间不串，也不碰真实 %APPDATA%"""
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")


# ---------------- 假注册表 ----------------

class _Key:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeWinreg:
    """只实现 tray 用得到的四个调用，够钉住"写 / 读 / 删 / 无权限"四种行为"""
    HKEY_CURRENT_USER = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, deny=False):
        self.hive = {}                  # 键路径 -> {值名: 数据}
        self.deny = deny

    def OpenKey(self, root, key, reserved=0, access=0):
        if self.deny:
            raise OSError(5, "Access is denied.")
        if access:                                  # 写模式：键不存在就地建
            return _Key(self.hive.setdefault(key, {}))
        if key not in self.hive:
            raise FileNotFoundError(2, "系统找不到指定的键。")
        return _Key(self.hive[key])

    def QueryValueEx(self, key, name):
        if name not in key.data:
            raise FileNotFoundError(2, "系统找不到指定的值。")
        return key.data[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, typ, value):
        key.data[name] = value

    def DeleteValue(self, key, name):
        if name not in key.data:
            raise FileNotFoundError(2, "系统找不到指定的值。")
        key.data.pop(name)


@pytest.fixture
def reg(monkeypatch):
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(tray, "tray_supported", lambda: True)
    return fake


# ---------------- 开关默认值：装完就该是"关闭进托盘" ----------------

class _Win(QObject):
    """给 TrayController 当窗口用的替身（必须是 QObject：控制器拿它当 parent）。
    偏好判定不碰任何真控件，离屏环境里也就没有“有没有任务栏”这个变量。"""

    def __init__(self):
        super().__init__()
        self.hidden = False

    def hide(self):
        self.hidden = True


def _controller(monkeypatch, available=True):
    """只测偏好判定，不装真图标（available 直接顶掉）：离屏环境里
    根本没有任务栏，能钉住的只有"该不该进托盘"这一个口径"""
    monkeypatch.setattr(tray.TrayController, "available", lambda self: available)
    return tray.TrayController(_Win())


def test_close_to_tray_defaults_on(monkeypatch):
    assert _controller(monkeypatch).close_to_tray() is True


def test_close_to_tray_respects_pref_and_tray_availability(monkeypatch):
    c = _controller(monkeypatch)
    app_state.set_value(tray.CLOSE_TO_TRAY_KEY, False)
    assert c.close_to_tray() is False              # 用户要"点关闭直接退出"
    app_state.set_value(tray.CLOSE_TO_TRAY_KEY, True)
    c2 = _controller(monkeypatch, available=False)
    # 托盘不可用（被三方任务栏顶掉）时必须退回直接退出：
    # 否则窗口一关，程序进后台而用户连图标都找不到
    assert c2.close_to_tray() is False


def test_minimize_to_tray_defaults_off(monkeypatch):
    c = _controller(monkeypatch)
    assert c.minimize_to_tray() is False
    app_state.set_value(tray.MINIMIZE_TO_TRAY_KEY, True)
    assert c.minimize_to_tray() is True


def test_first_hide_tip_shows_only_once(monkeypatch):
    c = _controller(monkeypatch)
    assert app_state.get(tray.TIP_SHOWN_KEY) is None
    c.notify_first_hide()                    # 没装图标也不能抛（内部直接跳过）
    assert bool(app_state.get(tray.TIP_SHOWN_KEY)) is True
    c.notify_first_hide()                    # 第二次只记账，不再打扰


def test_hide_window_hides_the_window():
    w = _Win()
    tray.TrayController(w).hide_window()
    assert w.hidden is True


def test_remove_and_set_visible_are_safe_before_install(monkeypatch):
    c = _controller(monkeypatch)
    c.remove()                               # 没装过也要能安静调用（退出路径共用）
    c.set_visible(False)
    c.notify("标题", "内容")


# ---------------- 开机自启 ----------------

def test_autostart_command_always_starts_minimized(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd = tray.autostart_command()
    assert cmd.endswith("--minimized") and cmd.startswith('"')
    assert sys.executable.strip('"') in cmd


def test_autostart_command_in_dev_points_at_desktop_py(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = tray.autostart_command()
    assert "desktop.py" in cmd and "--minimized" in cmd


def test_write_then_read_autostart(reg):
    assert tray.read_autostart() is False            # 键不存在＝没开
    ok, msg = tray.write_autostart(True)
    assert (ok, msg) == (True, "")
    assert reg.hive[tray.RUN_KEY][tray.RUN_VALUE].endswith("--minimized")
    assert tray.read_autostart() is True


def test_turning_off_is_idempotent_when_absent(reg):
    """本来就没有自启项：关掉＝已经是目标状态，不能报"删除失败" """
    ok, msg = tray.write_autostart(False)
    assert (ok, msg) == (True, "")


def test_toggle_off_deletes_the_value(reg):
    tray.write_autostart(True)
    ok, _ = tray.write_autostart(False)
    assert ok is True
    assert tray.read_autostart() is False


def test_denied_write_returns_the_real_reason(reg, monkeypatch):
    reg.deny = True
    ok, msg = tray.write_autostart(True)
    assert ok is False
    assert "注册表" in msg and "Access is denied" in msg
    assert app_state.get(tray.AUTOSTART_KEY) is None   # 失败不落本地记账


def test_non_windows_has_no_autostart(monkeypatch):
    monkeypatch.setattr(tray, "tray_supported", lambda: False)
    ok, msg = tray.write_autostart(True)
    assert ok is False and "Windows" in msg
    assert tray.read_autostart() is False
