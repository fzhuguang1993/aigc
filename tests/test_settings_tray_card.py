"""
tests/test_settings_tray_card.py —— 「托盘与全局快捷键」设置卡接线自检

这张卡上的控件全是"即时生效"：勾一下就要落盘、重注册、同步托盘菜单，
没有一个「保存」按钮兜底。所以最容易出的事故是 handler 名字写错/漏写——
界面照常画出来，用户一点就 AttributeError。这里把每个入口都点一遍。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

import pytest
from PySide6.QtWidgets import QApplication

from gui import global_hotkey as ghk
from gui import tray
from gui.pages_settings import SettingsPage
from core import fileindex
from store import app_state


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """偏好落临时文件；开机自启读注册表这条腿整个顶掉（测试不碰真机）

    索引库路径也得顶掉：设置页现在有一张「本地文件搜索」卡会读它的状态，
    不顶就变成每条用例都去开一次本机那份真索引（实测 700 多 MB）。"""
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")
    monkeypatch.setattr(fileindex, "DB_PATH", tmp_path / "idx" / "file_index.db")
    monkeypatch.setattr(tray, "read_autostart", lambda: False)
    monkeypatch.setattr(tray, "write_autostart", lambda on: (True, ""))


class _Win:
    def __init__(self):
        self.refreshed = 0

    def refresh_tray_prefs(self):
        self.refreshed += 1


class _Box:
    """替掉 QMessageBox：离屏环境里一个模态框就能把整个测试挂住"""
    warned = []

    @staticmethod
    def warning(*args, **kwargs):
        _Box.warned.append(args[2] if len(args) > 2 else "")


@pytest.fixture
def _win():
    return _Win()


@pytest.fixture
def page(qapp, _win, _isolate):
    p = SettingsPage()
    p.window = lambda: _win                                 # 让 handler 找得到"主窗口"
    yield p


# ---------------- 卡片本身 ----------------

def test_card_widgets_exist(page):
    for name in ("ck_close_tray", "ck_min_tray", "ck_global", "ck_auto",
                 "launch_key", "home_key", "lbl_hotkey"):
        assert hasattr(page, name), f"设置卡缺控件：{name}"


def test_defaults_match_product_decision(page):
    """产品口径：点关闭默认收进托盘、点最小化默认不、全局键默认开"""
    assert page.ck_close_tray.isChecked() is True
    assert page.ck_min_tray.isChecked() is False
    assert page.ck_global.isChecked() is True


def test_status_line_is_filled_on_build(page):
    assert page.lbl_hotkey.text().strip()          # 一进来就该告诉他现在是什么状态


# ---------------- 每个开关点一遍：handler 必须在 ----------------

def test_tray_toggles_persist_and_notify_main_window(page, _win):
    page.ck_close_tray.setChecked(False)
    assert app_state.get(tray.CLOSE_TO_TRAY_KEY) is False
    page.ck_min_tray.setChecked(True)
    assert app_state.get(tray.MINIMIZE_TO_TRAY_KEY) is True
    assert _win.refreshed == 2                     # 两次都让主窗口按新规矩接管 ✕/最小化


def test_autostart_toggle_writes_registry_abstraction(page):
    page.ck_auto.setChecked(True)                  # 不抛就是接通了（假 write 返成功）
    assert app_state.get(tray.AUTOSTART_KEY) is None   # write_autostart 被顶掉，不落账


def test_autostart_failure_rolls_the_checkbox_back(page, monkeypatch):
    """写不进去必须把勾退回：留着勾＝告诉用户"开机会有软件"，而并不会"""
    monkeypatch.setattr(tray, "write_autostart", lambda on: (False, "写入注册表失败：被拦"))
    monkeypatch.setattr("gui.pages_settings.QMessageBox", _Box)
    _Box.warned.clear()
    page.ck_auto.blockSignals(True)                 # 只走 handler，不重复发信号
    page.ck_auto.setChecked(True)
    page.ck_auto.blockSignals(False)
    page._toggle_autostart(True)
    assert page.ck_auto.isChecked() is False
    assert _Box.warned and "注册表" in _Box.warned[0]


def test_launcher_key_roundtrip(page, monkeypatch):
    hits = []
    m = ghk.GlobalHotkeyManager()
    m._user32 = None                               # 顶掉系统能力：注册失败但不抛
    monkeypatch.setattr(ghk, "manager", m)
    monkeypatch.setattr("gui.pages_settings.set_launcher_shortcut",
                        lambda s: hits.append(s) or s)
    page.launch_key.clear()
    page._save_launcher_key()
    assert hits == [""]
    page._clear_launcher_key()                     # 清空走同一条路，不能再抛
    assert hits == ["", ""]


def test_home_key_starts_empty_by_design(page):
    """呼出主界面默认不给键（用户定的）：输进去不能预先占住一个系统热键"""
    from PySide6.QtGui import QKeySequence
    assert page.home_key.keySequence().toString() == ""
    assert QKeySequence("").toString() == ""        # 空串不会被解成一个真键位


def test_home_key_roundtrip(page, monkeypatch):
    """与总唤出键同一条链路：落盘交给 set_home_shortcut，随后刷状态行"""
    hits = []
    monkeypatch.setattr("gui.pages_settings.set_home_shortcut",
                        lambda s: hits.append(s) or s)
    refreshed = []
    monkeypatch.setattr(page, "_refresh_hotkey_status", lambda: refreshed.append(1))
    monkeypatch.setattr(page, "home_key", _KeyEdit("Ctrl+Alt+H"))
    page._save_home_key()
    assert hits == ["Ctrl+Alt+H"] and refreshed == [1]
    monkeypatch.setattr(page, "home_key", _KeyEdit(""))
    page._clear_home_key()                          # 清空按钮走同一条路，不能再抛
    assert hits == ["Ctrl+Alt+H", ""]


def test_refresh_hotkey_status_survives_every_branch(page, monkeypatch):
    """四种状态都要有话说得出：不可用 / 没开全局 / 有失败 / 一切正常"""
    m = ghk.GlobalHotkeyManager()
    m._user32 = None
    monkeypatch.setattr(ghk, "manager", m)
    page._refresh_hotkey_status()
    assert "Windows" in page.lbl_hotkey.text()

    m._user32 = object()                           # 假装能注册
    app_state.set_value("tool_hotkey_global", False)
    page._refresh_hotkey_status()
    assert "窗口内" in page.lbl_hotkey.text()

    app_state.set_value("tool_hotkey_global", True)
    m.failures["爆款拆解"] = "键位已被系统或其它软件占用"
    page._refresh_hotkey_status()
    assert "爆款拆解" in page.lbl_hotkey.text()

    m.failures.clear()
    m._ids["语音识别"] = 1                         # 有一个键确实注册上了
    page._refresh_hotkey_status()
    assert "1 个" in page.lbl_hotkey.text()


def test_failure_line_translates_reserved_tokens(page, monkeypatch):
    """热键表里躺的是 __launcher__/__home__ 这种内部标识，直接拿它当提示用户看不懂"""
    from gui.tools_registry import HOME_TOKEN, LAUNCHER_TOKEN
    m = ghk.GlobalHotkeyManager()
    m._user32 = object()
    monkeypatch.setattr(ghk, "manager", m)
    page._refresh_hotkey_status()
    assert "__" not in page.lbl_hotkey.text()

    m.failures[LAUNCHER_TOKEN] = "键位已被系统或其它软件占用"
    m.failures[HOME_TOKEN] = "键位已被系统或其它软件占用"
    page._refresh_hotkey_status()
    text = page.lbl_hotkey.text()
    assert "总唤出面板" in text and "呼出主界面" in text


def test_home_key_counts_into_status(page, monkeypatch):
    """只配了主界面键时，状态行不能说“还没配快捷键”：明明配了"""
    from gui import tools_registry as reg
    m = ghk.GlobalHotkeyManager()
    m._user32 = object()
    monkeypatch.setattr(ghk, "manager", m)
    reg.set_home_shortcut("Ctrl+Alt+H")
    page._refresh_hotkey_status()
    assert "还没配" not in page.lbl_hotkey.text()


def test_tool_shortcut_save_also_refreshes_status(page, monkeypatch):
    """改工具键之后，状态行不能停留在旧结论"""
    called = []
    monkeypatch.setattr(page, "_refresh_hotkey_status", lambda: called.append(1))
    monkeypatch.setattr("gui.pages_settings.set_tool_shortcut", lambda n, s: s)
    page.tool_combo.addItem("🔥 测试工具", "测试工具")
    page._save_tool_shortcut()
    assert called == [1]


# ---------------- 半截组合键不许落盘（本机日志里真的丢过键位） ----------------

class _Seq:
    def __init__(self, text):
        self._text = text

    def toString(self):
        return self._text


class _KeyEdit:
    """冒充 QKeySequenceEdit：手里只按住 Ctrl+Alt 时它也会把 "Ctrl+Alt+" 发出来"""

    def __init__(self, text):
        self._text = text

    def keySequence(self):
        return _Seq(self._text)

    def clear(self):                        # 「清空」按钮会先调这一句
        self._text = ""


@pytest.mark.parametrize("text,expect", [
    ("Ctrl+Alt+K", "Ctrl+Alt+K"),        # 按完了：照旧交给注册层
    ("", ""),                            # 主动清空：不能拦「清除键位」按钮
    ("Ctrl+Alt+", None),                 # 半截：手里只剩修饰键
    ("D", None),                         # 裸字母：注册层同样解析不出来
])
def test_key_seq_or_hint(page, text, expect):
    assert page._key_seq_or_hint(_KeyEdit(text)) == expect
    if expect is None:
        assert "没按完" in page.lbl_hotkey.text()


def test_half_typed_launcher_combo_keeps_the_working_key(page, monkeypatch):
    """注册层是先 forget 再注册：拿半截串落盘＝旧键位被释放、新键位又注不上"""
    saved = []
    monkeypatch.setattr("gui.pages_settings.set_launcher_shortcut",
                        lambda s: saved.append(s))
    monkeypatch.setattr(page, "launch_key", _KeyEdit("Ctrl+Alt+"))
    page._save_launcher_key()
    assert saved == []
    monkeypatch.setattr(page, "launch_key", _KeyEdit("Ctrl+Alt+K"))
    page._save_launcher_key()
    assert saved == ["Ctrl+Alt+K"]


def test_half_typed_home_combo_keeps_the_working_key(page, monkeypatch):
    """两条键同一个闸门：不能因为“新加的那行”少个校验就丢键"""
    saved = []
    monkeypatch.setattr("gui.pages_settings.set_home_shortcut",
                        lambda s: saved.append(s))
    monkeypatch.setattr(page, "home_key", _KeyEdit("Ctrl+Alt+"))
    page._save_home_key()
    assert saved == []
    assert "没按完" in page.lbl_hotkey.text()
    monkeypatch.setattr(page, "home_key", _KeyEdit("Ctrl+Alt+K"))
    page._save_home_key()
    assert saved == ["Ctrl+Alt+K"]


def test_half_typed_tool_key_does_not_wipe_old_binding(page, monkeypatch):
    saved = []
    monkeypatch.setattr("gui.pages_settings.set_tool_shortcut",
                        lambda n, s: saved.append((n, s)))
    page.tool_combo.addItem("🔥 测试工具", "测试工具")
    page.tool_combo.setCurrentIndex(page.tool_combo.count() - 1)   # 新项不一定是当前项
    monkeypatch.setattr(page, "tool_key", _KeyEdit("Ctrl+Alt+"))
    page._save_tool_shortcut()
    assert saved == []
    monkeypatch.setattr(page, "tool_key", _KeyEdit("Ctrl+Alt+J"))
    page._save_tool_shortcut()
    assert saved == [("测试工具", "Ctrl+Alt+J")]
