"""
tests/test_hotkey_wiring.py —— 快捷键配置层 + 主窗口注册口径 + 安装/启动参数一致性

钉住这次最容易出事的几处：
1. 窗口收进托盘后设键：activeWindow() 返回 None，写进去不报错也永不生效；
2. 系统热键注册成功又挂一份 QShortcut：一次按键弹两次窗口；
3. 改键/清键后旧系统热键没释放：键白白占着，别的软件按不动；
4. 安装器 AppMutex、单实例互斥体名、--minimized 旗标三处必须同源，
   写错一处就是"升级时不问一句直接覆盖正在跑的 exe"或"开机自启弹一屏告警"；
5. ✕ / 最小化 / 双击图标 这三个动作的归宿全靠偏好说话，接错一根就是
   "关一个小窗整个程序没了"或"双击没反应"。
"""
import io
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from core import config, single_instance
from gui import global_hotkey as ghk
from gui import main_window as mw
from gui import tools_registry as reg
from gui.dialogs_launcher import match_score
from gui.window_frame import TitleBar
from store import app_state


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")


@pytest.fixture(autouse=True)
def _keep_log_out_of_dev_file(monkeypatch):
    """注册成没成、按命中都写日志：测试跑出来的那几十行不能落进开发者真的 aigc.log"""
    class _Log:
        def __init__(self):
            self.lines = []

        def info(self, msg, *a):
            self.lines.append(("info", msg))

        def warning(self, msg, *a):
            self.lines.append(("warn", msg))

    fake = _Log()
    monkeypatch.setattr("core.logger.log", fake)
    return fake


class _FakeUser32:
    def __init__(self, ok=True, hook=True):
        self.ok = ok
        self.hook_ok = hook
        self.registered = {}
        self.released = []
        self.hooks = []
        self.unhooks = []

    def RegisterHotKey(self, hwnd, hid, mods, vk):
        if not self.ok:
            return 0
        self.registered[hid] = (mods, vk)
        return 1

    def UnregisterHotKey(self, hwnd, hid):
        self.released.append(hid)
        self.registered.pop(hid, None)
        return 1

    # 兜底通道的三个调用：假句柄要给非 0，否则测试里会多一行“钩子没装上”的告警，
    # 把“最后一行日志”这种断言带沟里
    def SetWindowsHookExW(self, idhook, proc, inst, tid):
        if not self.hook_ok:
            return None
        self.hooks.append(len(self.hooks) + 1)
        return self.hooks[-1]

    def UnhookWindowsHookEx(self, handle):
        self.unhooks.append(handle)
        return 1

    def CallNextHookEx(self, hook, ncode, wparam, lparam):
        return 0

    def GetAsyncKeyState(self, vk):
        return 0


@pytest.fixture
def hk(qapp, monkeypatch):
    """换掉模块单例：主窗口取的是 gui.global_hotkey.manager"""
    m = ghk.GlobalHotkeyManager()
    m._user32 = _FakeUser32()
    monkeypatch.setattr(ghk, "manager", m)
    return m


class _Page:
    def __init__(self):
        self.opened = []
        self.hints = 0

    def open_tool(self, name):
        self.opened.append(name)

    def refresh_sc_hints(self):
        self.hints += 1


class StubWindow(QWidget):
    """只带 apply_* 用到的成员：建真 MainWindow 要把整站页面都拖起来"""

    def __init__(self):
        super().__init__()
        self.page_tools = _Page()
        self.summoned = []
        self.launched = 0
        self.homed = 0

    def summon_tool(self, name):
        self.summoned.append(name)

    def _summon_launcher(self):
        self.launched += 1

    def show_home(self):
        self.homed += 1

    def _log_hotkeys(self, what):
        """走真方法的实现跑一遍："注册结果写进日志"这条链也得有测试兜着"""
        mw.MainWindow._log_hotkeys(self, what)


@pytest.fixture
def stub_window(qapp):
    """带上 apply_tool_shortcuts 标记的 stub（main_window() 靠这个鸭子类型找窗）。

    用完必须把标记删掉：顶层 widget 会一直挂在 topLevelWidgets 里，
    不清就会让下一个用例拿到错的对象（隔用例串扰的常见成因）。"""
    w = StubWindow()
    w.apply_tool_shortcuts = lambda: None
    yield w
    del w.apply_tool_shortcuts


# ---------------- 配置层 ----------------

def test_main_window_found_even_when_hidden_in_tray(qapp, stub_window):
    """这就是原来用 activeWindow() 的翻车点：窗口一藏，设键静默失败"""
    sentinel = lambda: "hit"
    stub_window.apply_tool_shortcuts = sentinel
    stub_window.hide()
    assert reg.main_window().apply_tool_shortcuts is sentinel
    assert QApplication.activeWindow() is None      # 旧写法在这种情况下拿不到窗


def test_launcher_default_then_cleared(qapp, hk):
    assert reg.launcher_shortcut() == reg.LAUNCHER_DEFAULT
    reg.set_launcher_shortcut("Ctrl+Alt+J")
    assert reg.launcher_shortcut() == "Ctrl+Alt+J"
    assert hk.is_registered(reg.LAUNCHER_TOKEN) is False   # 没主窗口就不该动系统键
    reg.set_launcher_shortcut("")
    assert reg.launcher_shortcut() == ""            # 清空要粘住，不能被默认值顶回来


def test_tool_shortcut_is_exclusive_per_key(qapp):
    reg.set_tool_shortcut("爆款拆解", "Ctrl+Alt+9")
    reg.set_tool_shortcut("语音识别", "Ctrl+Alt+9")
    seqs = reg.tool_shortcuts()
    assert seqs.get("爆款拆解") is None             # 一键一工具：前一个让位
    assert seqs["语音识别"] == "Ctrl+Alt+9"


def test_global_switch_is_persisted_and_reregisters(qapp, stub_window):
    calls = []
    stub_window.apply_tool_shortcuts = lambda: calls.append(1)
    assert reg.global_hotkey_enabled() is True      # 装托盘的形态默认开全局
    reg.set_global_hotkey_enabled(False)
    assert reg.global_hotkey_enabled() is False
    assert calls == [1]                             # 拨开关立即重注册，不等重启


def test_pinned_tools_drop_unknown_names():
    app_state.set_value(reg.PINNED_KEY, ["爆款拆解", "已下架的工具"])
    assert reg.load_pinned() == ["爆款拆解"]


# ---------------- 主窗口注册口径 ----------------

def test_global_key_wins_and_no_duplicate_shortcut(qapp, hk):
    app_state.set_value("tool_shortcuts", {"爆款拆解": "Ctrl+Alt+1"})
    w = StubWindow()
    mw.MainWindow.apply_tool_shortcuts(w)
    assert w._tool_scuts == []                      # 注册成功就不再挂窗口内快捷键
    assert hk.bound_tokens() == {"爆款拆解"}
    hk.dispatch("爆款拆解")                          # 系统热键命中真的走到唤出
    assert w.summoned == ["爆款拆解"]


def test_failed_global_registration_falls_back_to_qshortcut(qapp, hk):
    hk._user32.ok = False                           # 键被微信/QQ 抢了
    app_state.set_value("tool_shortcuts", {"语音识别": "Ctrl+Alt+2"})
    w = StubWindow()
    mw.MainWindow.apply_tool_shortcuts(w)
    assert len(w._tool_scuts) == 1                  # 退回窗口内：软件开着还能用
    assert hk.bound_tokens() == set()
    assert "语音识别" in hk.failures                 # 失败原因留给设置页如实显示


def test_switch_off_global_releases_system_keys(qapp, hk):
    app_state.set_value("tool_shortcuts", {"爆款拆解": "Ctrl+Alt+3"})
    w = StubWindow()
    mw.MainWindow.apply_tool_shortcuts(w)
    assert hk.bound_tokens() == {"爆款拆解"}
    app_state.set_value(reg.GLOBAL_KEY, False)
    mw.MainWindow.apply_tool_shortcuts(w)
    assert hk.bound_tokens() == set()               # 关掉全局就把系统键交还给别的软件
    assert len(w._tool_scuts) == 1


def test_cleared_key_releases_stale_token(qapp, hk):
    app_state.set_value("tool_shortcuts", {"批量改名": "Ctrl+Alt+4"})
    w = StubWindow()
    mw.MainWindow.apply_tool_shortcuts(w)
    app_state.set_value("tool_shortcuts", {})       # 用户在设置页清了键
    mw.MainWindow.apply_tool_shortcuts(w)
    assert hk.bound_tokens() == set()
    assert hk._user32.released                      # 确实调了 UnregisterHotKey


def test_launcher_key_binds_and_clears(qapp, hk):
    w = StubWindow()
    reg.set_launcher_shortcut("Ctrl+Alt+Space")
    mw.MainWindow.apply_launcher_shortcut(w)
    assert hk.bound_tokens() == {reg.LAUNCHER_TOKEN}
    assert w._launcher_scuts == []
    hk.dispatch(reg.LAUNCHER_TOKEN)
    assert w.launched == 1
    reg.set_launcher_shortcut("")
    mw.MainWindow.apply_launcher_shortcut(w)
    assert hk.bound_tokens() == set()
    assert w._launcher_scuts == []


# ---------------- 呼出主界面的键（与总唤出面板各自一条） ----------------

def test_home_key_defaults_empty_and_persists(qapp):
    """产品口径（用户定的）：不预置默认键，空串就是没设——别拿默认值顶回来"""
    assert reg.home_shortcut() == ""
    reg.set_home_shortcut("Ctrl+Alt+H")
    assert reg.home_shortcut() == "Ctrl+Alt+H"
    reg.set_home_shortcut("")
    assert reg.home_shortcut() == ""


def test_home_key_binds_dispatches_and_clears(qapp, hk):
    w = StubWindow()
    reg.set_home_shortcut("Ctrl+Alt+H")
    mw.MainWindow.apply_home_shortcut(w)
    assert hk.bound_tokens() == {reg.HOME_TOKEN}
    assert w._home_scuts == []                      # 注册上了就不再挂窗口内快捷键
    hk.dispatch(reg.HOME_TOKEN)
    assert w.homed == 1
    reg.set_home_shortcut("")
    mw.MainWindow.apply_home_shortcut(w)
    assert hk.bound_tokens() == set()               # 清键要把系统键交还出去
    assert w._home_scuts == []


def test_home_key_falls_back_to_window_shortcut(qapp, hk):
    """键被别的软件抢了也得能用（软件开着时），而且失败原因要留给设置页"""
    hk._user32.ok = False
    w = StubWindow()
    reg.set_home_shortcut("Ctrl+Alt+H")
    mw.MainWindow.apply_home_shortcut(w)
    assert len(w._home_scuts) == 1
    assert hk.bound_tokens() == set()
    assert reg.HOME_TOKEN in hk.failures


def test_home_key_reapplies_all_three_slots(qapp, stub_window):
    """改一个键可能把另一个槽位的同键清掉：只重注自己那一侧，对面旧键会卡在系统里
    占着不走（用户看到的就是“主界面键设了，弹出来的却是唤出面板”）"""
    calls = []
    stub_window.apply_tool_shortcuts = lambda: calls.append("tool")
    stub_window.apply_launcher_shortcut = lambda: calls.append("launch")
    stub_window.apply_home_shortcut = lambda: calls.append("home")
    reg.set_home_shortcut("Ctrl+Alt+H")
    assert calls == ["tool", "launch", "home"]


def test_home_key_frees_launcher_on_collision(qapp):
    """一键只管一个动作：RegisterHotKey 只会给后注册的那个报 1409，
    而界面上一堆 ✓ 看着全都对——静默失败最难查的就是这种"""
    reg.set_launcher_shortcut("Ctrl+Alt+G")
    reg.set_home_shortcut("Ctrl+Alt+G")
    assert reg.launcher_shortcut() == ""            # 旧那个已让位
    assert reg.home_shortcut() == "Ctrl+Alt+G"


def test_launcher_key_frees_home_on_collision(qapp):
    """反过来也一样：后设的那个拿键，先设的那个不能默默占着旧键"""
    reg.set_home_shortcut("Ctrl+Alt+G")
    reg.set_launcher_shortcut("Ctrl+Alt+G")
    assert reg.home_shortcut() == ""
    assert reg.launcher_shortcut() == "Ctrl+Alt+G"


def test_reserved_key_frees_tool_binding_and_vice_versa(qapp):
    """工具键与两个保留位同口径：双向都得让位，不能只防一边"""
    reg.set_tool_shortcut("爆款拆解", "Ctrl+Alt+Y")
    reg.set_home_shortcut("Ctrl+Alt+Y")
    assert reg.tool_shortcuts().get("爆款拆解") is None
    assert reg.home_shortcut() == "Ctrl+Alt+Y"

    reg.set_home_shortcut("Ctrl+Alt+U")
    reg.set_tool_shortcut("语音识别", "Ctrl+Alt+U")
    assert reg.home_shortcut() == ""                # 工具抢走了主界面键
    assert reg.tool_shortcuts()["语音识别"] == "Ctrl+Alt+U"


def test_apply_tool_shortcuts_keeps_reserved_tokens(qapp, hk):
    """工具重注册时会把“配置里已经没有的键位”放开，但两个保留位不在工具表里：
    一并当成陈旧键位放掉的话，面板键与主界面键就会被自己人摘掉"""
    reg.set_launcher_shortcut("Ctrl+Alt+Space")
    reg.set_home_shortcut("Ctrl+Alt+H")
    w = StubWindow()
    mw.MainWindow.apply_launcher_shortcut(w)
    mw.MainWindow.apply_home_shortcut(w)
    assert hk.bound_tokens() == {reg.LAUNCHER_TOKEN, reg.HOME_TOKEN}
    mw.MainWindow.apply_tool_shortcuts(w)           # 只改工具，不碰保留位
    assert hk.bound_tokens() == {reg.LAUNCHER_TOKEN, reg.HOME_TOKEN}


class _HomeWin(QWidget):
    def __init__(self):
        super().__init__()
        self.refreshed = 0
        self.moves = []

    def _refresh(self):
        self.refreshed += 1


def test_show_home_wakes_minimized_window_and_refreshes(qapp, monkeypatch):
    """藏在托盘/最小化/压在其它窗口下面都要露出来，不补抢前台就会被前台锁拒掉"""
    seen = []
    monkeypatch.setattr(mw, "raise_to_front", lambda w: seen.append(w))
    w = _HomeWin()
    monkeypatch.setattr(w, "showNormal", lambda: w.moves.append("normal"))
    monkeypatch.setattr(w, "show", lambda: w.moves.append("show"))
    monkeypatch.setattr(w, "raise_", lambda: None)
    monkeypatch.setattr(w, "activateWindow", lambda: None)
    mw.MainWindow.show_home(w)
    assert w.moves == ["show"]                      # 没最小化：走 show，不能当成最小化去恢复
    assert seen == [w] and w.refreshed == 1

    monkeypatch.setattr(w, "isMinimized", lambda: True)
    w.moves.clear()
    mw.MainWindow.show_home(w)
    assert w.moves == ["normal"]                    # 最小化过：用 showNormal 而不是 show


def test_show_home_has_no_toggle_hide(qapp, monkeypatch):
    """主界面不做“再按一次收起”：误按一下就把工作台收走的代价远大于多按一次
    （唤出面板那种小窗可以随手收，这条不行）"""
    monkeypatch.setattr(mw, "raise_to_front", lambda w: None)
    w = _HomeWin()
    for m in ("show", "showNormal", "raise_", "activateWindow", "isMinimized"):
        monkeypatch.setattr(w, m, lambda *a: None)
    hides = []
    monkeypatch.setattr(w, "hide", lambda: hides.append(1))
    mw.MainWindow.show_home(w)
    mw.MainWindow.show_home(w)
    assert hides == []


# ---------------- 托盘偏好接线：✕ / 最小化 的归宿 ----------------

class _Tray:
    """TrayController 替身：只回答两个偏好，并记下菜单有没有同步"""

    def __init__(self, close_to_tray=True, minimize_to_tray=False):
        self._close = close_to_tray
        self._min = minimize_to_tray
        self.menus_refreshed = 0

    def close_to_tray(self):
        return self._close

    def minimize_to_tray(self):
        return self._min

    def refresh_menu(self):
        self.menus_refreshed += 1


class _TrayWindow(QWidget):
    def __init__(self, tray):
        super().__init__()
        self.tray = tray
        self.hidden_to_tray = 0

    def hide_to_tray(self):
        self.hidden_to_tray += 1


@pytest.fixture
def tray_window(qapp):
    built = []

    def build(tray):
        w = _TrayWindow(tray)
        built.append(w)
        return w

    yield build
    for w in built:
        w.deleteLater()                # 顶层 widget 会一直挂着，不清干净就会影响后续用例


def test_tray_pref_takes_over_last_window_quit(qapp, tray_window):
    """关闭进托盘开着时主窗口只是 hide，任务栏没有入口。这时假设“最后一个
    窗口关了退出”必须关掉：否则用户关掉一个工具小窗就把整个程序带走了。"""
    app = QApplication.instance()
    keep = app.quitOnLastWindowClosed()
    try:
        t = _Tray(close_to_tray=True)
        w = tray_window(t)
        mw.MainWindow.refresh_tray_prefs(w)
        assert app.quitOnLastWindowClosed() is False
        assert t.menus_refreshed == 1              # 自启勾选态跟着同步进托盘菜单
        t._close = False                           # 用户改回"点关闭直接退出"
        mw.MainWindow.refresh_tray_prefs(w)
        assert app.quitOnLastWindowClosed() is True
    finally:
        app.setQuitOnLastWindowClosed(keep)        # 不改全局状态，别的用例接着跑


def test_minimize_button_follows_pref(qapp, tray_window):
    w = tray_window(_Tray(minimize_to_tray=True))
    assert mw.MainWindow.try_minimize_to_tray(w) is True
    assert w.hidden_to_tray == 1

    w2 = tray_window(_Tray(minimize_to_tray=False))
    assert mw.MainWindow.try_minimize_to_tray(w2) is False
    assert w2.hidden_to_tray == 0                  # 没开就照常最小化，不能暗藏窗口


def test_titlebar_lets_the_window_reroute_minimize(qapp, tray_window):
    """标题栏不去 import gui.tray，而是鸭子类型问窗口一句要不要改道：
    工具小窗没这个钩子，必须照常走 showMinimized。"""
    w = tray_window(_Tray(minimize_to_tray=True))
    w.try_minimize_to_tray = lambda: mw.MainWindow.try_minimize_to_tray(w)
    TitleBar(w, "AIGC 工厂").btn_min.click()
    assert w.hidden_to_tray == 1

    plain = tray_window(_Tray(minimize_to_tray=False))
    calls = []
    plain.showMinimized = lambda: calls.append(1)   # 离屏下别真去改窗口状态
    TitleBar(plain, "工具窗").btn_min.click()
    assert calls == [1] and plain.hidden_to_tray == 0


# ---------------- 唤出面板的匹配口径 ----------------

@pytest.mark.parametrize("q,name,score", [
    ("爆", "爆款拆解", 0), ("爆款", "爆款拆解", 0),
    ("拆解", "爆款拆解", 1), ("爆拆", "爆款拆解", 2),
])
def test_match_scores(qapp, q, name, score):
    assert match_score(q, name, "粘贴爆款链接：拆分镜/口播") == score


def test_match_desc_and_miss(qapp):
    assert match_score("录制", "屏幕录制", "全屏录制") == 1        # 名称包含优先于简介
    assert match_score("压缩", "素材瘦身", "把参考图批量压缩到接口要求的大小") == 3
    assert match_score("zzz", "素材瘦身", "把参考图批量压缩") is None
    assert match_score("", "任意", "") == 0          # 空串＝全量列表，不是不匹配


# ---------------- 启动参数与单实例 ----------------

def test_pop_tray_flag_is_removed_before_qt_sees_it():
    """旗标必须从 argv 取走：Qt 把不认识的参数当自己的选项解析，会刷一屏告警"""
    import desktop
    argv = ["app.exe", "--minimized", "--foo"]
    assert desktop.pop_tray_flag(argv) is True
    assert argv == ["app.exe", "--foo"]
    argv2 = ["app.exe"]
    assert desktop.pop_tray_flag(argv2) is False and argv2 == ["app.exe"]
    assert "--tray" in desktop.TRAY_FLAGS


def test_mutex_name_matches_installer_appmutex():
    """安装器靠 AppMutex 认出"程序正开着"：名字与 single_instance 不同源就是空写"""
    assert single_instance._mutex_name().startswith("Local\\")
    assert single_instance.WINDOW_TITLE in single_instance._mutex_name()
    iss = io.open(_iss_path(), encoding="utf-8-sig").read()
    assert "AppMutex=Local\\%s" % single_instance.WINDOW_TITLE in iss
    # 开机自启/后台启动的旗标必须是入口认的那一个：写成别的串就成"开机 nothing"
    import desktop
    assert "--minimized" in desktop.TRAY_FLAGS
    assert "--minimized" in iss            # .iss 里的"后台启动"快捷方式用的就是它


def _iss_path():
    from pathlib import Path
    return Path(__file__).resolve().parents[1] / "installer" / "aigc.iss"


def test_installer_version_source_is_config():
    from installer.read_version import read_version
    assert read_version() == config.APP_VERSION


def test_iss_keeps_utf8_bom_and_crlf():
    """脚本编码写死在测试里，不靠口头约定。

    Inno Setup 靠 BOM 认 UTF-8：抹掉以后，英文系统按 ANSI 解脚本，
    中文 AppName / 快捷方式名直接编成乱码（要到装完才看得见）。而常用
    编辑/换行工具很容易顺手把 BOM 弄丢、把 CRLF 拉成 LF，所以这两件
    必须有人拦住。"""
    raw = _iss_path().read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "installer/aigc.iss 丢了 UTF-8 BOM"
    lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
    assert lone_lf == 0, f"installer/aigc.iss 须全 CRLF，查出 {lone_lf} 个单独 LF"


class _FakeWin:
    """只实现 activate_existing 要问的三句：状态用构造参数控"""

    def __init__(self, iconic=False, visible=True):
        self.iconic = iconic
        self.visible = visible
        self.shown = []

    def IsIconic(self, hwnd):
        return int(self.iconic)

    def IsWindowVisible(self, hwnd):
        return int(self.visible)

    def ShowWindow(self, hwnd, cmd):
        self.shown.append(cmd)
        return 1


def test_second_instance_wakes_a_tray_hidden_window():
    """收进托盘走的是 hide()，不是最小化：IsIconic 给 False。只盯着 iconic 就
    会跳过了 ShowWindow，剩下一个 SetForegroundWindow 打在看不见的窗口上——
    用户观感就是"双击图标没反应"（任务栏里也没入口，唯一的路被托盘占了）。"""
    hidden = _FakeWin(iconic=False, visible=False)
    assert single_instance._restore_if_needed(hidden, 1) is True
    assert hidden.shown == [single_instance._SW_RESTORE]

    minimized = _FakeWin(iconic=True, visible=False)
    assert single_instance._restore_if_needed(minimized, 1) is True

    front = _FakeWin(iconic=False, visible=True)
    assert single_instance._restore_if_needed(front, 1) is False
    assert front.shown == []                       # 已经露着：别再动它一下


def test_restore_helper_swallows_api_errors():
    """取不到窗口状态（句柄已失效）不能抛：第二个实例该安静退出而不是报错框"""
    class _Boom:
        def IsIconic(self, hwnd):
            raise OSError(6, "Invalid handle")

    assert single_instance._restore_if_needed(_Boom(), 0) is False


# ---------------- 呼出来了还得看得见（本轮"快捷键没生效"的真根因） ----------------

def test_launcher_panel_is_topmost(qapp):
    """不置顶就是本次的 bug：面板 show 出来了，却弹在用户正看着的窗口底下。

    本机实测（往运行中的程序投一条 WM_HOTKEY）：visible=True、在前台=False、
    WS_EX_TOPMOST=False——回调与显示都走到了，用户看到的却是"按了没反应"。"""
    from PySide6.QtCore import Qt
    from gui import dialogs_launcher as dl
    d = dl.LauncherDialog(lambda name: None, None)
    flags = d.windowFlags()
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.Tool
    d.close()


def test_summon_panel_grabs_foreground(qapp, monkeypatch):
    """activateWindow() 会被 Windows 前台锁拒掉，summon() 必须补一次抢前台"""
    from gui import dialogs_launcher as dl
    seen = []
    monkeypatch.setattr(dl, "raise_to_front", lambda w: seen.append(w))
    d = dl.LauncherDialog(lambda name: None, None)
    monkeypatch.setattr(d, "_load_tools", lambda: [])
    monkeypatch.setattr(d, "_center_on_screen", lambda: None)
    d.summon()
    assert seen == [d]
    d.close()


def test_summon_tool_also_raises_window(qapp, monkeypatch):
    """工具直连键走的是 summon_tool：只 raise_()/activateWindow() 同样顶不到最前"""
    from gui import main_window as _mw
    seen = []
    monkeypatch.setattr(_mw, "raise_to_front", lambda w: seen.append(w))

    class _Dlg(QWidget):
        def showNormal(self):
            pass

    page = _Page()
    page.opened_tool = lambda name: _Dlg()
    w = StubWindow()
    w.page_tools = page
    mw.MainWindow.summon_tool(w, "爆款拆解")
    assert len(seen) == 1


def test_log_hotkeys_reports_both_outcomes(qapp, hk, monkeypatch):
    """注册成败都要在日志里留一行：界面那行小字只有设键的人看得见，
    事后排查只能靠日志（本轮就是日志里 0 条热键记录）。"""
    from core import logger
    lines = []

    class _Log:
        def info(self, msg, *a):
            lines.append(("info", msg))

        def warning(self, msg, *a):
            lines.append(("warn", msg))

    monkeypatch.setattr(logger, "log", _Log())
    StubWindow._log_hotkeys(StubWindow(), "工具快捷键")
    assert lines[-1][0] == "info" and "0 个" in lines[-1][1]   # 没配键也打个账，确认跑过
    hk.register("爆款拆解", "Ctrl+Alt+1")
    StubWindow._log_hotkeys(StubWindow(), "工具快捷键")
    assert lines[-1][0] == "info" and "爆款拆解" in lines[-1][1]
    hk._user32.ok = False
    hk.register("语音识别", "Ctrl+Alt+2")
    StubWindow._log_hotkeys(StubWindow(), "工具快捷键")
    assert lines[-1][0] == "warn" and "语音识别" in lines[-1][1]
