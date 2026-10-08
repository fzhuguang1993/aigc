"""
tests/test_global_hotkey.py —— 全局快捷键：键位解析、注册结果回报、按 token 派发

用假 user32 顶掉 Win32 调用（RegisterHotKey 返回 1/0），这样两条最容易出事的路
都能在离屏环境里钉住：
- 键位解析口径必须和界面上的 QKeySequenceEdit 一致，否则"看着设上了其实没设"；
- 注册失败必须被记进 failures 并能在改键后清掉，否则界面只能瞎报"已生效"。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 早于任何 PySide6 导入

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui import global_hotkey as ghk


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def mgr(qapp):
    """独立管理器 + 假 user32：不碰真系统热键，也不污染模块单例"""
    m = ghk.GlobalHotkeyManager()
    m._user32 = _FakeUser32()
    return m


class _FakeUser32:
    def __init__(self, ok=True, hook=True, down=()):
        self.ok = ok
        self.hook_ok = hook
        self.registered = {}        # id -> (mods, vk)
        self.released = []
        self.hooks = []             # SetWindowsHookExW 发出去的句柄
        self.unhooks = []           # UnhookWindowsHookEx 收回去的句柄
        self.down = set(down)       # 哪些 vk 算“此刻按着”

    def RegisterHotKey(self, hwnd, hid, mods, vk):
        if not self.ok:
            return 0
        assert hwnd is None, "传 NULL 才会把 WM_HOTKEY 投到 GUI 线程消息队列"
        self.registered[hid] = (mods, vk)
        return 1

    def UnregisterHotKey(self, hwnd, hid):
        self.released.append(hid)
        self.registered.pop(hid, None)
        return 1

    # 兜底通道（低级键盘钩子）：不给这几个方法的话，每次注册都会多一行
    # “钩子没装上”的告警，把按 lines[-1] 断言的测试全带沟里
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
        """按 down 集合回答：真钩子报的是左/右变体（0xA2/0xA4），测试就专门用变体喂它"""
        return -32768 if vk in self.down else 0


# ---------------- 键位解析 ----------------

@pytest.mark.parametrize("seq,mods,vk", [
    ("Ctrl+Alt+1", ghk.MOD_CONTROL | ghk.MOD_ALT | ghk.MOD_NOREPEAT, ord("1")),
    ("CTRL+ALT+1", ghk.MOD_CONTROL | ghk.MOD_ALT | ghk.MOD_NOREPEAT, ord("1")),
    ("Meta+Shift+F9", ghk.MOD_WIN | ghk.MOD_SHIFT | ghk.MOD_NOREPEAT, 0x70 + 8),
    # 用户嘴里的"Win+E"必须认：QKeySequence 不认 Win 这个写法
    ("Win+E", ghk.MOD_WIN | ghk.MOD_NOREPEAT, ord("E")),
    # 裸功能键是 Windows 允许的例外（不截用户打字）
    ("F9", ghk.MOD_NOREPEAT, 0x70 + 8),
])
def test_parse_ok(qapp, seq, mods, vk):
    assert ghk.parse_sequence(seq) == (mods, vk)


@pytest.mark.parametrize("bad", [
    "", "   ", "A", "Ctrl+A, Ctrl+B", "@@@", "Ctrl+@@@", "Junk-9",
    "Ctrl+Alt 1",                                # 分隔符必须是 +：QKeySequence 不认空格
])
def test_parse_rejects(qapp, bad):
    """被拒的键位一律退回窗口内 QShortcut：
    Key_unknown / 多段 / 无修饰键的普通键都不能拿去 RegisterHotKey"""
    assert ghk.parse_sequence(bad) is None


def test_parse_never_raises(qapp):
    for s in (None, 0, "Ctrl+", "+++", "F0", "Ctrl+Alt+Shift+Win+Space"):
        ghk.parse_sequence(s)                       # 只求不炸，值由上面两条钉


def test_modifier_flags_survive_enum_api(qapp):
    """PySide6 6.11 起修饰键标志不能直接 int()，_flag 走 .value 兜底"""
    assert ghk._flag(Qt.ControlModifier) == int(ghk._flag(Qt.ControlModifier))
    assert ghk._flag(7) == 7


# ---------------- 注册与派发 ----------------

def test_register_and_dispatch_by_token(mgr):
    assert mgr.register("爆款拆解", "Ctrl+Alt+1") is True
    hit = []
    mgr.set_callback("爆款拆解", lambda t: hit.append(t))
    mgr.dispatch("爆款拆解")
    mgr.dispatch("没登记的键")                        # 静默忽略，不能抛
    assert hit == ["爆款拆解"]
    assert mgr.is_registered("爆款拆解")
    assert mgr.bound_tokens() == {"爆款拆解"}


def test_register_failure_is_reported_not_swallowed(mgr):
    mgr._user32.ok = False                            # 键被微信/QQ 抢了
    assert mgr.register("语音识别", "Ctrl+Alt+2") is False
    assert "占用" in mgr.failures["语音识别"]
    assert mgr.bound_tokens() == set()
    # 换一个没人占的键：失败记录必须跟着清掉，否则界面永远挂着旧报错
    mgr._user32.ok = True
    assert mgr.register("语音识别", "Ctrl+Alt+7") is True
    assert "语音识别" not in mgr.failures


def test_reparse_failure_message_is_actionable(mgr):
    assert mgr.register("封面提取", "A") is False
    assert "修饰键" in mgr.failures["封面提取"]


def test_re_register_releases_old_id(mgr):
    mgr.register("视频水印", "Ctrl+Alt+3")
    first = dict(mgr._user32.registered)
    mgr.register("视频水印", "Ctrl+Alt+4")            # 改键＝先释放再注册
    assert mgr._user32.released and mgr._user32.released[0] in first
    assert len(mgr._user32.registered) == 1


def test_forget_drops_callback_so_no_window_leak(mgr):
    mgr.register("批量改名", "Ctrl+Alt+5")
    mgr.set_callback("批量改名", lambda t: None)
    mgr.forget("批量改名")
    assert not mgr.is_registered("批量改名")
    assert mgr._callbacks == {}
    mgr.dispatch("批量改名")                          # 派发不到人：不能抛


def test_unregister_all(mgr):
    mgr.register("a", "Ctrl+Alt+1")
    mgr.register("b", "Ctrl+Alt+2")
    mgr.unregister_all()
    assert mgr.bound_tokens() == set()
    assert mgr._user32.registered == {}


def test_unsupported_platform_registers_nothing(qapp):
    m = ghk.GlobalHotkeyManager()
    m._user32 = None                                  # 非 Windows：available False
    assert m.available() is False
    assert m.register("a", "Ctrl+Alt+1") is False


def test_singleton_exists_for_app_wiring():
    assert isinstance(ghk.manager, ghk.GlobalHotkeyManager)


# ---------------- 失败原因：说系统给的真话，并留下日志 ----------------

@pytest.fixture(autouse=True)
def _quiet_logger(monkeypatch):
    """注册失败必写日志，但测试不能把那几行写进开发者真实的 aigc.log"""
    class _Log:
        def __init__(self):
            self.lines = []

        def warning(self, msg, *a):
            self.lines.append(("warn", msg))

        def info(self, msg, *a):
            self.lines.append(("info", msg))

    fake = _Log()
    monkeypatch.setattr("core.logger.log", fake)
    return fake


def test_fail_reason_per_error_code():
    """1409 才是“热键已被注册”的铁证（本机拿 FormatMessageW 当面问过系统）。

    以前不分码一律写“已被系统或其它软件占用”，于是“键位被占”“组合非法”
    “句柄无效”三件不同的事在界面上长得一样，排查只能从外面拿探针猜。"""
    assert "换个键" in ghk._fail_reason(1409)
    assert "不接受" in ghk._fail_reason(87)
    assert "句柄无效" in ghk._fail_reason(1401)
    assert "占用" in ghk._fail_reason(0)          # 取不到码：保留老说法，不编新故事


def test_last_error_refuses_untrusted_handle():
    """假 user32 不往 ctypes 的线程槽位里写东西，读到的是上一个调用留下的陈货
    （本机实测串成了单实例 CreateMutexW 的 183）：没标记就不许信。"""
    assert ghk._last_error(_FakeUser32()) == 0
    assert ghk._last_error(None) == 0


def test_conflict_code_reaches_the_user(mgr, monkeypatch, _quiet_logger):
    mgr._user32.ok = False
    monkeypatch.setattr(ghk, "_last_error", lambda handle=None: 1409)
    assert mgr.register("语音识别", "Ctrl+Alt+2") is False
    assert "换个键" in mgr.failures["语音识别"]
    assert "1409" in _quiet_logger.lines[-1][1]        # 日志里带着真码，事后能查


def test_parse_failure_also_logged(mgr, _quiet_logger):
    """解析不过也是一次“没注册上”，同样得在日志里留行"""
    assert mgr.register("封面提取", "A") is False
    assert _quiet_logger.lines and "封面提取" in _quiet_logger.lines[-1][1]


def test_registration_and_hit_are_logged(mgr, _quiet_logger):
    """注册成了什么键、在哪个线程、按下有没有命中——三条都要能在日志里查到。

    RegisterHotKey(NULL) 把 WM_HOTKEY 投给“调用它的线程”：注册线程与跑 Qt
    事件循环的线程不是同一个时，键位注册得上但按下去永远没人响应，
    不报错过错——tid 就是为分辨这一种情况留下的。"""
    if not ghk.is_supported():
        pytest.skip("要看的是 Windows 注册路径写出的内容")
    mgr.register("爆款拆解", "Ctrl+Alt+1")
    mgr.set_callback("爆款拆解", lambda t: None)
    mgr.dispatch("爆款拆解")
    text = "\n".join(m for _tag, m in _quiet_logger.lines)
    assert "已注册 爆款拆解" in text
    assert "命中 爆款拆解" in text
    assert "线程" in text and "vk=0x31" in text
    assert mgr.reg_tid >= 0
    assert mgr._keys["爆款拆解"][1] == ord("1")


def test_hit_without_callback_is_warned(mgr, _quiet_logger):
    """键位还在系统里、动作却没挂上：按下去不会有任何反应，以前日志里一句都不记"""
    if not ghk.is_supported():
        pytest.skip("没注册上就谈不上命中")
    mgr.register("封面提取", "Ctrl+Alt+8")
    mgr.dispatch("封面提取")
    assert _quiet_logger.lines[-1][0] == "warn"
    assert "没挂动作" in _quiet_logger.lines[-1][1]


# ---------------- 兜底投递通道：本进程自己的键盘钩子 ----------------
#
# 本机实测：RegisterHotKey 返回成功、按键确实进了输入栈（钩子 10/10 看见、
# CallNextHookEx 返回 0 说明无人吞键），而这个进程一条 WM_HOTKEY 都收不到；
# 同一时刻 explorer 的 Win+E、Snipaste 的 F1 正常。下面的用例钉的是：
# 既然钩子是这台机器上唯一还看得见按键的通道，那它必须真的能认出组合键。

def test_hook_channel_follows_registration_lifecycle(mgr, _quiet_logger):
    """有键位才挂钩子，键位清空就摘：钩子是全系统的，留着白拖慢别人的键盘"""
    if not ghk.is_supported():
        pytest.skip("钩子只在 Windows 上挂")
    assert mgr.register("爆款拆解", "Ctrl+Alt+1") is True
    assert mgr._hook and mgr._hook_cb is not None    # 回调必须留着强引用，被 GC 就是野指针
    mgr.register("语音识别", "Ctrl+Alt+2")           # 第二个键：不重复挂
    assert mgr._user32.hooks == [1]
    mgr.unregister("爆款拆解")
    assert mgr._hook                                 # 还剩一个键位，钩子得在
    mgr.unregister("语音识别")
    assert mgr._hook is None and mgr._hook_cb is None
    assert mgr._user32.unhooks == [1]


def test_hook_install_failure_does_not_break_registration(mgr, _quiet_logger):
    """钩子装不上只是少一条兜底，不能反过来把主通道标成失败"""
    mgr._user32.hook_ok = False
    assert mgr.register("语音识别", "Ctrl+Alt+3") is True
    assert mgr._hook is None
    assert "钩子没装上" in _quiet_logger.lines[-1][1]


def test_find_hotkey_masks_norepeat(mgr):
    """MOD_NOREPEAT 是 RegisterHotKey 的口径，与“此刻按着什么”无关，不能参与比对"""
    mgr._keys["x"] = (ghk.MOD_CONTROL | ghk.MOD_ALT | ghk.MOD_NOREPEAT, ord("D"))
    assert mgr.find_hotkey(ord("D"), ghk.MOD_CONTROL | ghk.MOD_ALT) == "x"
    assert mgr.find_hotkey(ord("D"), ghk.MOD_CONTROL) is None      # 少按一个 Alt 不算
    assert mgr.find_hotkey(ord("E"), ghk.MOD_CONTROL | ghk.MOD_ALT) is None
    mgr._keys["f"] = (ghk.MOD_NOREPEAT, 0x77)                      # 裸 F8：修饰键全空
    assert mgr.find_hotkey(0x77, 0) == "f"


def test_hook_channel_skips_auto_repeat(mgr):
    """按住不放系统会连发 keydown（本机实测一次按住左 Alt 连发 30 多条）：只认第一下"""
    mgr._keys["x"] = (ghk.MOD_CONTROL | ghk.MOD_ALT | ghk.MOD_NOREPEAT, ord("D"))
    mods = ghk.MOD_CONTROL | ghk.MOD_ALT
    assert mgr.on_key_event(ord("D"), True, mods) == "x"
    assert mgr.on_key_event(ord("D"), True, mods) is None       # 连发
    assert mgr.on_key_event(ord("D"), False, mods) is None      # 松手
    assert mgr.on_key_event(ord("D"), True, mods) == "x"        # 再按下算新的一次


def test_mods_async_asks_system_not_own_bookkeeping(mgr):
    """修饰键状态现问 GetAsyncKeyState：自己记账错过一个 keyup 就永久脏，
    会把用户根本没按的组合键当成按了；而且真钩子报的是左/右变体，
    只盯 0x11/0x12 这两个通用码会永远认不出组合键。"""
    if not ghk.is_supported():
        pytest.skip("GetAsyncKeyState 只在 Windows 路径上有意义")
    mgr._user32.down = {0xA2, 0xA4}            # 左 Ctrl + 左 Alt
    assert mgr._mods_async() == ghk.MOD_CONTROL | ghk.MOD_ALT
    mgr._user32.down = {0x5B}                  # 左 Win
    assert mgr._mods_async() == ghk.MOD_WIN
    mgr._user32.down = set()
    assert mgr._mods_async() == 0


def test_hook_proc_walks_the_real_struct(mgr):
    """整条钩子路走一遍：真 KBDLLHOOKSTRUCT 地址 → 认出组合键 → 一律放行。

    这里用 ctypes 现场分配一个结构体再取地址：以前拿硬凑的整数当地址喂
    from_address，测试进程直接 access violation 崩掉——读非法地址不是异常，
    是硬崩，所以 _hook_proc 对 lparam 必须先判空。"""
    import ctypes
    fired = []
    mgr.set_callback("x", fired.append)
    mgr._keys["x"] = (ghk.MOD_CONTROL | ghk.MOD_ALT | ghk.MOD_NOREPEAT, ord("D"))
    mgr._user32.down = {0xA2, 0xA4}
    buf = ghk._KBDLLHOOKSTRUCT()
    buf.vkCode = ord("D")
    buf.scanCode = 0x20
    addr = ctypes.addressof(buf)
    assert mgr._hook_proc(0, ghk.WM_KEYDOWN, addr) == 0      # CallNextHookEx 的返回值
    assert mgr._held == {ord("D")}                            # 结构体真的被读开了
    mgr._hook_proc(0, ghk.WM_KEYUP, addr)
    assert mgr._held == set()
    # 坏数据一律不许炸：ncode<0、lparam 为空、不认识的消息
    assert mgr._hook_proc(-1, ghk.WM_KEYDOWN, addr) == 0
    assert mgr._hook_proc(0, ghk.WM_KEYDOWN, 0) == 0
    assert mgr._hook_proc(0, 0x0999, addr) == 0


def test_hook_channel_defers_the_action_out_of_the_callback(mgr, qapp):
    """钩子回调里绝不建窗口（Windows 超时会悄悄摘钩，这期间全系统键盘都等我），
    动作要排回事件循环：下一轮 processEvents 才能看到它真的被执行了。"""
    if not ghk.is_supported():
        pytest.skip("钩子通道只在 Windows 上挂")
    import ctypes
    fired = []
    mgr.set_callback("x", fired.append)
    mgr._keys["x"] = (ghk.MOD_CONTROL | ghk.MOD_ALT | ghk.MOD_NOREPEAT, ord("D"))
    mgr._user32.down = {0xA2, 0xA4}
    buf = ghk._KBDLLHOOKSTRUCT()
    buf.vkCode = ord("D")
    mgr._hook_proc(0, ghk.WM_KEYDOWN, ctypes.addressof(buf))
    assert fired == []                       # 回调当场不派发，只把它排进队列
    qapp.processEvents()
    assert fired == ["x"]


def test_two_channels_dedup_to_one_activation(mgr):
    """正常机器上两条通道都会到：只允许先到的派发。否则一次按键弹两次面板，
    第二次正好把刚弹出来的收掉——看起来就是“闪一下没了”。"""
    hits = []
    mgr.set_callback("x", hits.append)
    assert mgr._try_fire("x", "系统投递") is True
    assert mgr._try_fire("x", "键盘钩子") is False
    assert hits == ["x"]
    mgr._fired["x"] -= ghk.DOUBLE_FIRE_SECONDS + 1      # 过了去重窗口：下一次照常
    assert mgr._try_fire("x", "键盘钩子") is True
    assert hits == ["x", "x"]


def test_hook_proc_passes_through_on_unusable_input(mgr):
    """钩子回调里抛异常＝把异常带回原生代码，整个进程崩；坏数据也必须照样放行"""
    fired = []
    mgr.set_callback("x", fired.append)
    mgr._keys["x"] = (ghk.MOD_CONTROL | ghk.MOD_ALT | ghk.MOD_NOREPEAT, ord("D"))
    assert mgr._hook_proc(-1, ghk.WM_KEYDOWN, 0) == 0        # ncode<0：不处理
    assert mgr._hook_proc(0, ghk.WM_KEYDOWN, None) == 0      # 地址为空：不能去 from_address
    assert mgr._hook_proc(0, 0x0999, None) == 0              # 不认识的消息
    assert fired == []
