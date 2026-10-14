"""
gui/window_foreground.py —— 把窗口真正顶到最前并拿到键盘焦点

为什么需要这一层（这是个真 bug，不是保险丝）：
全局热键触发时，前台一定属于"用户刚才还在用的那个软件"。而 Windows 有条
前台锁：后台进程调 SetForegroundWindow 会被直接拒掉（Qt 的 activateWindow()
走的就是它）。现象就是"按了快捷键什么也没发生"——窗口其实已经 show() 了，
只是既没焦点也没盖在别人上面，正好藏在用户正看着的窗口底下。
（本机实测：投一条 WM_HOTKEY 进运行中的程序，面板 visible=True、
 在前台=False、WS_EX_TOPMOST=False。）

系统允许的正规后门是 AttachThreadInput：把本线程临时挂到前台线程的输入队列上，
SetForegroundWindow 就被认作"用户刚刚在我们这儿点过"。相比网上常见的
"发一个假的 ALT 按键" 的野路子，它不会触发别人程序里的菜单助记符，也不污染输入。

⚠ 只在 Windows 上有意义；任何异常都吞掉并返回 False——顶不到最前只是不好看，
把热键回调炸掉才是事故。调用方应同时给窗口加 WindowStaysOnTopHint 之类
的兜底（唤出面板就加了），让"拿不到焦点"时至少还看得见。
"""
import ctypes
import sys

try:                                    # wintypes 只在 Windows 上存在
    from ctypes import wintypes as _wintypes
except (ImportError, ValueError):
    _wintypes = None

_SW_SHOW = 5


def is_supported():
    return sys.platform.startswith("win") and _wintypes is not None


def _pin_signatures(u):
    """钉好句柄签名：不声明 restype，64 位下 GetForegroundWindow 返回的指针会被
    截成 32 位整数，于是 AttachThreadInput 拿到一个假线程 id——每一步都"调用成功"，
    合起来一次也不生效。测试注入的替身没有这些属性，跳过就行。"""
    try:
        u.GetForegroundWindow.restype = _wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [_wintypes.HWND,
                                               ctypes.POINTER(_wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = _wintypes.DWORD
        u.AttachThreadInput.argtypes = [_wintypes.DWORD, _wintypes.DWORD,
                                        _wintypes.BOOL]
        u.AttachThreadInput.restype = _wintypes.BOOL
        for name in ("BringWindowToTop", "SetForegroundWindow"):
            getattr(u, name).argtypes = [_wintypes.HWND]
            getattr(u, name).restype = _wintypes.BOOL
        u.ShowWindow.argtypes = [_wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = _wintypes.BOOL
    except (AttributeError, TypeError):
        pass                             # 替身/老版本：签名不是功能，跳过不影响


def _is_hung(u, hwnd):
    """前台那个窗口是不是已经不应答了（挂死）。

    为什么得先问这一句：AttachThreadInput 是把对方输入队列**并进我们这条线程**，
    随后的 SetForegroundWindow 会向它同步发消息——对方一挂死，我们这条线程就跟着死。
    而本程序的全局低级键盘钩子恰好就挂在同一条 Qt 主线程上：主线程一被卡住，
    全系统的键盘都得排队等它返回，观感就是"整机卡死"。宁可这次不顶到最前
    （窗口照样 show，只是压在别人底下），也不能拿一条钩子线程去赌。"""
    if _wintypes is None:
        return False
    try:
        u.IsHungAppWindow.argtypes = [_wintypes.HWND]
        u.IsHungAppWindow.restype = _wintypes.BOOL
    except Exception:
        pass                        # 测试替身没函数指针可设：签名不是功能，跳过
    try:
        return bool(u.IsHungAppWindow(hwnd))
    except Exception:
        return False              # 探不到就当没挂死：不能因为探针自坏把功能关掉


def _foreground(u, k, hwnd):
    """AttachThreadInput 成对地把本线程挂到前台线程上，顶完立刻解绑"""
    cur = int(k.GetCurrentThreadId())
    fg = int(u.GetForegroundWindow() or 0)
    tgt = int(u.GetWindowThreadProcessId(fg, None)) if fg else 0
    if tgt and _is_hung(u, fg):
        return False                  # 前台挂了：不把自己的输入队列跟它绑在一起
    # 前台本来就挂在同一个线程上（就是我们自己）：没必要挂，挂了反而白折腾
    attached = bool(tgt and tgt != cur and u.AttachThreadInput(tgt, cur, True))
    try:
        u.ShowWindow(hwnd, _SW_SHOW)     # 藏在托盘/最小化时，先让它存在于可见层
        u.BringWindowToTop(hwnd)
        return bool(u.SetForegroundWindow(hwnd))
    finally:
        # 必须成对解除：长期挂着别人的输入队列，两边输入会一起卡住（鼠标点不动）
        if attached:
            u.AttachThreadInput(tgt, cur, False)


def raise_to_front(widget, user32=None, kernel32=None):
    """把 widget 顶到最前并抢焦点；返回是否真拿到了前台。

    非 Windows / 拿不到原生句柄 / Win 调用异常 → False，绝不抛：这一步只做"看得见"
    的加固，主流程（show/raise）在调用方已经做过了。
    user32/kernel32 供测试注入替身——真句柄要两个进程互相抢前台才撞得出来，
    而这里要钉住的是"挂上就必须解绑"这条纪律，替身足够。"""
    if widget is None:
        return False
    if user32 is None and not is_supported():
        return False
    try:
        hwnd = int(widget.winId())
    except (AttributeError, TypeError, ValueError):
        return False
    if not hwnd:
        return False
    u = user32 if user32 is not None else ctypes.windll.user32
    k = kernel32 if kernel32 is not None else ctypes.windll.kernel32
    if user32 is None:
        _pin_signatures(u)
    try:
        return bool(_foreground(u, k, hwnd))
    except Exception:
        return False


# ====================================================================
# 快贴面板用：把焦点交回“刚才那个窗口”，再模拟一次 Ctrl+V
# ====================================================================
#: 虚拟键码与 keybd_event 标志（只这里用，就近定义免得满屏常量）
_VK_CONTROL = 0x11
_VK_V = 0x56
_KEYEVENTF_KEYUP = 0x0002


def get_foreground_hwnd():
    """当前前台窗口的原生句柄（整数）；拿不到给 0。

    快贴面板在弹出前抓这一句，选中条目后把焦点还回这个窗——用户才能像
    Win+V 那样“一键贴回刚才正在写的地方”。非 Windows / 异常一律 0，不抛。"""
    if not is_supported():
        return 0
    try:
        u = ctypes.windll.user32
        _pin_signatures(u)
        return int(u.GetForegroundWindow() or 0)
    except Exception:
        return 0


def focus_hwnd(hwnd):
    """把某个原生句柄顶回前台。与 raise_to_front 同一套 AttachThreadInput 纪律：
    本线程临时挂到目标线程输入队列上，SetForegroundWindow 才被认作“用户刚在那儿操过盘”。

    与 raise_to_front 的区分：它吃 Qt widget，这里吃裸句柄（目标根本是我们的窗口时
    widget 不存在）。同样不抛：顶不回前台只是贴不上，不能把快贴流程炸掉。"""
    if not hwnd or not is_supported():
        return False
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        _pin_signatures(u)
        return bool(_foreground(u, k, int(hwnd)))
    except Exception:
        return False


def send_paste():
    """模拟一次 Ctrl+V（keybd_event）：焦点已在目标窗口后才能调。

    非 Windows 直接 False（无 Win32 键盘注入）；任何异常吞掉返回 False。
    不声明 argtypes 也能跑（都是小整型与 0），但为 64 位下 ULONG_PTR 参数安全还是钉一下。"""
    if not is_supported():
        return False
    try:
        u = ctypes.windll.user32
        u.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte,
                                  _wintypes.DWORD, ctypes.c_void_p]
        u.keybd_event.restype = None
        u.keybd_event(_VK_CONTROL, 0, 0, None)
        u.keybd_event(_VK_V, 0, 0, None)
        u.keybd_event(_VK_V, 0, _KEYEVENTF_KEYUP, None)
        u.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, None)
        return True
    except Exception:
        return False
