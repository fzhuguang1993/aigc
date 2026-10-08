"""
tests/test_window_foreground.py —— 抢前台：挂上就必须解绑，任何异常都不许外抛

raise_to_front() 是这次"快捷键呼不出面板"的修法：Qt 的 activateWindow() 底层就是
SetForegroundWindow，而 Windows 有条前台锁——后台进程调它会被直接拒掉，于是窗口
确实 show 了，却顶不到用户正看着的窗口上面（本机实测 visible=True/在前台=False）。

真句柄要两个进程互抢才撞得出来，这里用替身钉住两条纪律：
- AttachThreadInput 成对（挂 True / 解 False）：长期挂着别人的输入队列，
  两边鼠标键盘会一起卡死，那是比"看不见面板"严重得多的事故；
- 失败只返回 False，绝不把异常抛回热键回调。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

from gui import window_foreground as wf


class _FakeU:
    """user32 替身：把调用记成流水，好断言"挂/解"成对；raise_on 指定哪个 API 炸"""

    def __init__(self, fg_tid=999, set_ok=True, raise_on=(), fg=1234, hung=False):
        self.fg_tid = fg_tid
        self.set_ok = set_ok
        self.raise_on = set(raise_on)
        self.fg = fg
        self.hung = hung
        self.calls = []

    def _hit(self, name):
        self.calls.append(name)
        if name in self.raise_on:
            raise OSError(5, "拒绝访问")

    def GetForegroundWindow(self):
        self.calls.append("fg")
        return self.fg

    def GetWindowThreadProcessId(self, hwnd, out):
        self.calls.append("tid")
        return self.fg_tid

    def IsHungAppWindow(self, hwnd):
        self.calls.append("hung")
        return int(self.hung)

    def AttachThreadInput(self, a, b, on):
        self.calls.append(("attach", a, b, on))
        return 1

    def ShowWindow(self, hwnd, cmd):
        self._hit("show")
        return 1

    def BringWindowToTop(self, hwnd):
        self._hit("top")
        return 1

    def SetForegroundWindow(self, hwnd):
        self.calls.append("setfg")
        if "setfg" in self.raise_on:
            raise OSError(5, "拒绝访问")
        return int(self.set_ok)


class _FakeK:
    def __init__(self, tid=111):
        self.tid = tid

    def GetCurrentThreadId(self):
        return self.tid


class _W:
    """只回答 winId() 的假 widget"""

    def __init__(self, hwnd=0x1000, error=False):
        self._hwnd = hwnd
        self._error = error

    def winId(self):
        if self._error:
            raise AttributeError("没有原生窗口")
        return self._hwnd


def _attaches(u):
    return [c for c in u.calls if isinstance(c, tuple) and c[0] == "attach"]


def test_attach_is_paired_with_current_thread():
    """前台属于别人：挂上去顶一把，顶完必须原样解绑"""
    u = _FakeU(fg_tid=999)
    assert wf.raise_to_front(_W(), user32=u, kernel32=_FakeK(111)) is True
    assert _attaches(u) == [("attach", 999, 111, True), ("attach", 999, 111, False)]
    # 顺序也钉住：先解绑再返回——中途抛异常时 finally 也得赶上解绑
    assert u.calls.index("setfg") < len(u.calls) - 1


def test_same_thread_does_not_attach():
    """前台就是我们自己（主窗口开着时按键）：没必要挂，挂了反而白折腾"""
    u = _FakeU(fg_tid=111)
    assert wf.raise_to_front(_W(), user32=u, kernel32=_FakeK(111)) is True
    assert _attaches(u) == []
    assert "show" in u.calls and "setfg" in u.calls


def test_no_foreground_window_skips_attach():
    """桌面/锁屏时 GetForegroundWindow 给 0：不能拿着 0 线程 id 去挂"""
    u = _FakeU(fg=0, fg_tid=999)
    assert wf.raise_to_front(_W(), user32=u, kernel32=_FakeK(111)) is True
    assert _attaches(u) == []
    assert "tid" not in u.calls                    # 前台都没有，问线程 id 也是多余


def test_detach_runs_even_when_setforeground_throws():
    """抢前台那步炸了也要解绑——泄漏挂载是会把用户机器输入卡住的"""
    u = _FakeU(fg_tid=999, raise_on=("setfg",))
    assert wf.raise_to_front(_W(), user32=u, kernel32=_FakeK(111)) is False
    assert _attaches(u) == [("attach", 999, 111, True), ("attach", 999, 111, False)]


def test_refused_foreground_returns_false_not_exception():
    """系统就是不给前台：如实返回 False，让调用方的置顶标志兜底"""
    assert wf.raise_to_front(_W(), user32=_FakeU(set_ok=False),
                             kernel32=_FakeK()) is False


def test_bad_widget_is_ignored():
    assert wf.raise_to_front(None, user32=_FakeU(), kernel32=_FakeK()) is False
    assert wf.raise_to_front(_W(hwnd=0), user32=_FakeU(),
                             kernel32=_FakeK()) is False
    assert wf.raise_to_front(_W(error=True), user32=_FakeU(),
                             kernel32=_FakeK()) is False


def test_unsupported_platform_does_not_touch_win32(monkeypatch):
    """非 Windows：直接 False，绝不能去碰 ctypes.windll（那才是 AttributeError）"""
    monkeypatch.setattr(wf, "is_supported", lambda: False)
    assert wf.raise_to_front(_W()) is False


def test_hung_foreground_is_never_attached():
    """前台已经不应答：一步都不许挂。AttachThreadInput 是把对方输入队列并进我们
    这条线程，而全局键盘钩子就挂在同一条 Qt 主线程上——对方挂死时我们跟着卡，
    表现为整机键盘一起卡住不动。宁可顶不到最前（窗口照样 show），也不能去赌这一把。"""
    u = _FakeU(fg_tid=999, hung=True)
    assert wf.raise_to_front(_W(), user32=u, kernel32=_FakeK(111)) is False
    assert "hung" in u.calls                       # 先探了挂死，不是直接放弃
    assert _attaches(u) == []                       # 一个挂都不许发
    assert "show" not in u.calls and "setfg" not in u.calls   # 也不再往里走
    # 对照：前台活着才走那套“挂上/解绑”
    u2 = _FakeU(fg_tid=999, hung=False)
    assert wf.raise_to_front(_W(), user32=u2, kernel32=_FakeK(111)) is True
    assert len(_attaches(u2)) == 2


def test_pin_signatures_tolerate_fakes():
    """替身没有 restype/argtypes 可设：签名不是功能，跳过就行，不能因此炸掉热键"""
    wf._pin_signatures(_FakeU())          # 属性都设在实例上，取不到函数指针签名
