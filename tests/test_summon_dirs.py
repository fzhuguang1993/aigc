"""
tests/test_summon_dirs.py —— Alt+W 唤出「接口管理」的会话级验证回归

用户诉求：Alt+W 必须**先弹口令输入框（指令输入台）**，验证通过后才放出「🔌 接口管理」页。
关键陷阱：旧版用「按天解锁」标记（app_has_unlocked，跨会话存 ui_state）决定是否弹框，
导致同一天重开程序后 Alt+W 直接跳页/或干脆没反应，口令框再也不弹。

修法：改用**本会话**标记 _api_verified——新会话首次 Alt+W 一律 force=True 强制弹框验证，
验证通过才放出；同会话再次 Alt+W 才直接跳页（不重复弹框）。

用鸭子类型 stub 直调未绑定方法，不建真实 SettingsPage（省得起一堆 Qt 依赖）。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _Box:
    def __init__(self):
        self.visible = None

    def setVisible(self, v):
        self.visible = v


class _Win:
    def __init__(self):
        self.revealed = False

    def reveal_api_page(self):
        self.revealed = True


class _Gate:
    def __init__(self, result):
        self.result = result
        self.asked = False
        self.last_force = None

    def ask(self, parent, force=False):
        self.asked = True
        self.last_force = force
        return self.result


class _Stub:
    """只带 _summon_dirs 用到的成员。api_verified 模拟本会话是否已验证。"""

    def __init__(self, api_verified, gate, dirs_unlocked=False):
        self._win = _Win()
        self.dirs_box = _Box()
        self._gate_dirs = gate
        self._dirs_unlocked = dirs_unlocked
        self._api_verified = api_verified

    def window(self):
        return self._win


def test_new_session_first_press_forces_passcode_box(monkeypatch):
    """核心回归：本会话未验证（哪怕当天别处已解锁过）→ 必弹口令框，force=True。"""
    import gui.pages_settings as ps
    monkeypatch.setattr(ps, "GATEWAY_MODE", False)
    gate = _Gate(True)
    stub = _Stub(api_verified=False, gate=gate, dirs_unlocked=True)  # 当天已解锁但新会话
    ps.SettingsPage._summon_dirs(stub)
    assert gate.asked is True and gate.last_force is True   # 弹框且绕过按天缓存
    assert stub._win.revealed is True                       # 验证通过才放出
    assert stub._api_verified is True


def test_already_verified_this_session_jumps_without_box(monkeypatch):
    """同会话再次 Alt+W：已验证过 → 直接跳页，不再弹框。"""
    import gui.pages_settings as ps
    monkeypatch.setattr(ps, "GATEWAY_MODE", False)
    gate = _Gate(True)
    stub = _Stub(api_verified=True, gate=gate)
    ps.SettingsPage._summon_dirs(stub)
    assert gate.asked is False                              # 不重复要口令
    assert stub._win.revealed is True


def test_passcode_fail_does_not_reveal(monkeypatch):
    """口令错/取消：不放页面，会话标记也不置位。"""
    import gui.pages_settings as ps
    monkeypatch.setattr(ps, "GATEWAY_MODE", False)
    stub = _Stub(api_verified=False, gate=_Gate(False))
    ps.SettingsPage._summon_dirs(stub)
    assert stub._win.revealed is False
    assert stub._api_verified is False


def test_gateway_mode_noop(monkeypatch):
    import gui.pages_settings as ps
    monkeypatch.setattr(ps, "GATEWAY_MODE", True)
    stub = _Stub(api_verified=False, gate=_Gate(True))
    ps.SettingsPage._summon_dirs(stub)
    assert stub._win.revealed is False                      # 买家模式不放出维护人页
