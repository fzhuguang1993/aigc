"""
gui/global_hotkey.py —— Windows 系统级全局快捷键（RegisterHotKey）

和 QShortcut 的区别：QShortcut 只在自家窗口聚焦时认键；RegisterHotKey 注册的
是系统级热键，软件最小化到托盘、甚至焦点在微信里，按 Ctrl+Alt+1 照样能把
「爆款拆解」唤出来。装到托盘 + 全局键才叫"随手可用"的小工具。

为什么用 hwnd=None 而不是自建隐藏窗口：官方文档写明传 NULL 时 WM_HOTKEY 投递到
调用线程的消息队列，而 Qt 的事件循环就在 GUI 线程上跑 GetMessage/PeekMessage，
装一个 QAbstractNativeEventFilter 直接截 0x0400 即可——省掉一个原生窗口和它
自带的消息循环，也省掉销毁窗口的时机问题（退出前 UnregisterHotKey(None, id)）。

❗ 但“注册上”不等于“收得到”：本机实测过一桩悬案——RegisterHotKey 返回成功、
按键确实进了输入栈（钩子 10/10 看见、CallNextHookEx 返回 0 说明无人吞键），
而这个进程一条 WM_HOTKEY 都收不到，连不带修饰键的裸 F8 也一样，而 explorer 的
Win+E 与 Snipaste 的 F1 照常——本机的“物理按键→WM_HOTKEY”这条投递路对普通进程
是死的。因此下面额外挂一条本进程自己的低级键盘钩子做兜底通道：它不吃任何按键，
只靠同一个键位表自己认组合键，两条通道只放行先到的那一次（见 _try_fire）。

一个说在前面的限制：本进程非管理员运行时，焦点在“以管理员身份运行”的窗口里
（任务管理器等）钩子收不到按键——这是 Windows 的完整性级别规则，所有非提权
程序一样，不是这里能绕的；正常办公、浏览器、微信里唤出不受影响。

⚠ Windows 专有：非 Windows 平台 available() 返回 False，调用方自动退回
窗口内 QShortcut（功能不打折，只是“焦点在别的软件里按不动”）。
"""
import ctypes
import re
import sys
import time

from PySide6.QtCore import QAbstractNativeEventFilter, Qt
from PySide6.QtGui import QKeySequence

try:                                    # wintypes 只在 Windows 上存在
    from ctypes import wintypes as _wintypes
except (ImportError, ValueError):
    _wintypes = None

#: Windows 修饰键位（RegisterHotKey 的 fsModifiers）
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
#: Win8+：按住不放不连发（否则工具窗口会被反复 raise/activate）
MOD_NOREPEAT = 0x4000

#: 线程消息：热键被按下（wParam = 注册时给的 id）
WM_HOTKEY = 0x0400

#: 兜底投递通道用的低级键盘钩子（只在有键位注册着时才挂，见 _install_hook）
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
_KEY_DOWN_MSGS = (WM_KEYDOWN, WM_SYSKEYDOWN)
_KEY_UP_MSGS = (WM_KEYUP, WM_SYSKEYUP)
#: 修饰键的通用码与左右变体：低级钩子报的是左/右（0xA2/0xA4…），只盯 0x11/0x12 会错过
_MOD_VKS = ((0x11, MOD_CONTROL), (0xA2, MOD_CONTROL), (0xA3, MOD_CONTROL),
            (0x12, MOD_ALT), (0xA4, MOD_ALT), (0xA5, MOD_ALT),
            (0x10, MOD_SHIFT), (0xA0, MOD_SHIFT), (0xA1, MOD_SHIFT),
            (0x5B, MOD_WIN), (0x5C, MOD_WIN))
#: 两条通道都到齐时只认先到的那一下（秒）：重复一次会把刚弹出来的面板又收掉
DOUBLE_FIRE_SECONDS = 0.4

_VK_F1 = 0x70


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    """KBDLLHOOKSTRUCT：只用到 vkCode，其余字段原样声明，保证 from_address 偏移正确"""
    _fields_ = [("vkCode", ctypes.c_ulong), ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


#: WNDPROC：返回值与 LPARAM 都是指针宽度，64 位下用 c_int 会把地址截断
_HOOK_PROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int,
                                ctypes.c_void_p, ctypes.c_void_p)


def _flag(x):
    """枚举/标志取整数：PySide6 6.11 起修饰键标志不能直接 int()，要走 .value"""
    try:
        return int(x)
    except TypeError:
        return int(x.value)


# Qt 的键码与 Windows VK 不一致的那几个（方向键/编辑键/小键盘，以及 OEM 键：
# Qt 用 ASCII 值、VK 用另一套）。字母与数字两边编码相同，F1-F24 走偏移算法。
_QT_TO_VK = {
    int(Qt.Key_Backspace): 0x08, int(Qt.Key_Tab): 0x09, int(Qt.Key_Clear): 0x0C,
    int(Qt.Key_Return): 0x0D, int(Qt.Key_Enter): 0x0D, int(Qt.Key_Escape): 0x1B,
    int(Qt.Key_Space): 0x20, int(Qt.Key_PageUp): 0x21, int(Qt.Key_PageDown): 0x22,
    int(Qt.Key_End): 0x23, int(Qt.Key_Home): 0x24, int(Qt.Key_Left): 0x25,
    int(Qt.Key_Up): 0x26, int(Qt.Key_Right): 0x27, int(Qt.Key_Down): 0x28,
    int(Qt.Key_Select): 0x2F, int(Qt.Key_Print): 0x2A, int(Qt.Key_Printer): 0x2C,
    int(Qt.Key_Execute): 0x2B,
    int(Qt.Key_Insert): 0x2D, int(Qt.Key_Delete): 0x2E, int(Qt.Key_Help): 0x2F,
    int(Qt.Key_Period): 0xBE, int(Qt.Key_Comma): 0xBC, int(Qt.Key_Semicolon): 0xBA,
    int(Qt.Key_Plus): 0xBB, int(Qt.Key_Equal): 0xBB, int(Qt.Key_Minus): 0xBD,
    int(Qt.Key_AsciiTilde): 0xC0, int(Qt.Key_BracketLeft): 0xDB,
    int(Qt.Key_Backslash): 0xDC, int(Qt.Key_BracketRight): 0xDD,
    int(Qt.Key_Apostrophe): 0xDE, int(Qt.Key_Slash): 0xBF,
}

#: Qt 修饰键 -> Win 修饰键（只认这四个；Keypad/GroupSwitch 不参与全局热键）
_MODS = ((_flag(Qt.ControlModifier), MOD_CONTROL),
         (_flag(Qt.AltModifier), MOD_ALT),
         (_flag(Qt.ShiftModifier), MOD_SHIFT),
         (_flag(Qt.MetaModifier), MOD_WIN))

_KEY_UNKNOWN = _flag(Qt.Key_unknown)


def is_supported():
    return sys.platform.startswith("win") and _wintypes is not None


def _vk_for(code):
    """Qt 键码 -> Windows 虚拟键码；认不出返回 None（调用方退回窗口内快捷键）"""
    if int(Qt.Key_A) <= code <= int(Qt.Key_Z) or int(Qt.Key_0) <= code <= int(Qt.Key_9):
        return code                                   # 字母数字：两边编码相同（都是 ASCII）
    if int(Qt.Key_F1) <= code <= int(Qt.Key_F24):
        return _VK_F1 + (code - int(Qt.Key_F1))
    return _QT_TO_VK.get(code)


def _combo_parts(combo):
    """QKeySequence[0] -> (键码, Qt 修饰键位图)。

    PySide6 6.5+ 这里返回 QKeyCombination 对象（.key()/.keyboardModifiers()），
    更早的版本直接返回合成整数；两边都兼容，避免换 Qt 版本就"快捷键全体失灵"。"""
    key_of = getattr(combo, "key", None)
    mods_of = getattr(combo, "keyboardModifiers", None)
    if key_of is None or mods_of is None:
        combined = int(combo)
        return combined & ~_flag(Qt.KeyboardModifierMask), combined
    return int(key_of()), _flag(mods_of())


def _normalize_seq(seq):
    """把“Win+”这类写法归一成 Qt 认的“Meta+”。

    QKeySequenceEdit 回写的串是 Meta，但用户在文档/口口相传里写的是 Win：
    不转就会“设上了、存下了、就是不生效”，而且界面上一点提示都没有。"""
    s = str(seq or "")
    return re.sub(r"(?i)\bwin(keys?)?\b", "Meta", s)


def parse_sequence(seq):
    """组合键串 -> (fsModifiers, vk)，解析不了返回 None。

    走 QKeySequence 归一化再拆修饰键，是为了跟界面里的 QKeySequenceEdit 完全
    同一口径："Ctrl+Alt+1"、"CTRL+ALT+1"、"Meta+Shift+F9" 都能吃（大小写不敏感，
    修饰键顺序随意）。只认 "+" 作分隔符：写成"Ctrl+Alt 1"这里给 None，
    界面里的 QKeySequenceEdit 也同样给不出值，两边不会"看着设上了其实没设"。

    两条硬性拒绝：
    - 认不出的串（如 "@@@"）：QKeySequence 不报错，而是给出 Key_unknown，
      不挡掉就会拿着 0x1FFFFFF 去注册，永远不触发还不报错；
    - 无修饰键的普通键：裸一个字母会把用户所有软件里的打字全截走。
      裸功能键（F1-F24）是 Windows 允许的例外，保留可用。"""
    seq = _normalize_seq(seq).strip()
    if not seq:
        return None
    try:
        ks = QKeySequence(seq)
        if ks.count() != 1:               # 空串 0 段；"Ctrl+C, Ctrl+V" 多段都不支持
            return None
        code, qt_mods = _combo_parts(ks[0])
    except (TypeError, ValueError, IndexError):
        return None
    if code in (0, _KEY_UNKNOWN):
        return None
    vk = _vk_for(code)
    if vk is None:
        return None
    mods = 0
    for flag, win_mod in _MODS:
        if qt_mods & flag:
            mods |= win_mod
    if not mods and not (int(Qt.Key_F1) <= code <= int(Qt.Key_F24)):
        return None
    return mods | MOD_NOREPEAT, vk


def _load_user32():
    """取 user32 并钉好签名：不显式声明 argtypes/restype，64 位下句柄会被
    ctypes 当 int 截断，RegisterHotKey 看起来成功、其实一次都不触发。"""
    if not is_supported():
        return None
    # use_last_error=True 不是可有可无的：RegisterHotKey 失败后我们要反问错误码，
    # 而 ctypes 只在带这个标志加载的句柄上才会把 GetLastError 记进本线程的槽位。
    # 直接用 ctypes.windll.user32 的话 get_last_error() 读回来的是上一个 ctypes
    # 调用留下的陈货（本机实测串成了单实例互斥体 CreateMutexW 的 183）。
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32._records_last_error = True   # 供 _last_error() 辨认“这是真句柄”，见那边注释
    user32.RegisterHotKey.argtypes = [_wintypes.HWND, _wintypes.UINT,
                                      _wintypes.UINT, _wintypes.UINT]
    user32.RegisterHotKey.restype = _wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [_wintypes.HWND, _wintypes.UINT]
    user32.UnregisterHotKey.restype = _wintypes.BOOL
    # 兜底通道那几个调用同样要钉签名：钩子句柄与 LPARAM 都是指针宽度，
    # 少一句声明就是“钩子装上了、回调永远不进来”，或者地址被截成低 32 位后读到脏数据
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                        _wintypes.HINSTANCE, _wintypes.DWORD]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = _wintypes.BOOL
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_void_p, ctypes.c_void_p]
    user32.CallNextHookEx.restype = ctypes.c_int
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    return user32


#: RegisterHotKey 返回 FALSE 时系统给的真错误码 → 用户能照做的说法。
#: 这几个码是拿 FormatMessageW 当面问系统问出来的，不是抄文档：1409 才是
#: “热键已被注册”的铁证。以前不分错误码一律写“已被系统或其它软件占用”，
#: 于是“键位被占”“组合非法”“句柄无效”三件不同的事在界面上长得一模一样。
_ERROR_HINTS = {
    1409: "键位已被系统或其它软件注册（换个键，或先退出占用的软件）",
    87: "这个组合系统不接受（修饰键与按键的搭配无效）",
    1401: "热键功能未就绪（窗口句柄无效）",
    1408: "热键功能未就绪（窗口句柄无效）",
}

_IGNORE_INSERTS = 0x00000200
_FROM_SYSTEM = 0x00001000


def _last_error(handle=None):
    """紧接着失败的 RegisterHotKey 取错误码；取不到就当 0（未知）。

    只信 _load_user32() 造出来的真句柄：测试注入的假 user32 不会写槽位，此时
    get_last_error() 里躺的是别人留下的值——宁报“未知”也不能把假线索当真话说。"""
    if not is_supported() or not getattr(handle, "_records_last_error", False):
        return 0
    try:
        return int(ctypes.get_last_error())
    except (OSError, ValueError):
        return 0


def _win_error_text(code):
    """错误码 → 系统自己的文案（跟着系统界面语言）；问不到就交空串"""
    if not is_supported():
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        n = ctypes.windll.kernel32.FormatMessageW(
            _IGNORE_INSERTS | _FROM_SYSTEM, None, code, 0, buf, 256, None)
        return buf.value.strip() if n else ""
    except Exception:
        return ""


def _fail_reason(code):
    """错误码 → 给人看的一句话；code=0（假句柄/系统没给码）保留老文案"""
    hint = _ERROR_HINTS.get(code)
    if hint:
        return hint
    text = _win_error_text(code) if code else ""
    if text:
        return "键位注册失败：%s" % text
    return "键位已被系统或其它软件占用"


def _current_tid():
    """注册发生在哪个线程。

    RegisterHotKey(NULL) 把 WM_HOTKEY 投给“调用它的那个线程”的消息队列，而不是
    窗口所在线程：一旦注册线程与跑 Qt 事件循环的线程不是同一个，键位会“注册得上、
    按下去永远没人响应”（不报错、不崩溃，外观等同于功能没做）。在日志里留一手，
    遇到这种情况一眼就能分辨，不必再从外面拿探针猜。"""
    if not is_supported():
        return 0
    try:
        return int(ctypes.windll.kernel32.GetCurrentThreadId())
    except Exception:
        return 0


class GlobalHotkeyManager(QAbstractNativeEventFilter):
    """全局热键注册表 + 派发器。

    token（一般是工具名，或 "launcher" 总唤出）与自增 id 双向映射：id 给
    Windows 用，token 给业务用，命中后同步回调（已在主线程的事件循环里）。

    注册失败（键位被微信/QQ 抢了，RegisterHotKey 返回 FALSE）不抛：收进
    failures 让设置页明说"这个键被别的软件占了，已退回窗口内快捷键"——
    静默失败最坑，用户会以为功能根本没做。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._user32 = _load_user32()
        self._ids = {}                    # token -> 热键 id
        self._tokens = {}                 # id -> token
        self._keys = {}                   # token -> (mods, vk)，只给日志用
        self._callbacks = {}              # token -> 触发动作
        self._next = 1
        self._installed = False
        self.reg_tid = 0                  # 最后一次注册所在的线程 id
        self.failures = {}                # token -> 失败原因（界面提示用）
        # 兜底通道（本进程自己的键盘钩子）：句柄、回调强引用、当前按住的键
        self._hook = None
        self._hook_cb = None
        self._held = set()
        self._fired = {}                  # token -> 最近一次派发时刻（双通道去重）

    # ---------- 能力 ----------
    def available(self):
        return self._user32 is not None

    # ---------- 注册 ----------
    def register(self, token, seq):
        """注册一个全局热键；返回 True＝成功（同 token 再注册＝改键）"""
        self.unregister(token)
        if not self.available():
            return False
        parsed = parse_sequence(seq)
        if parsed is None:
            return self._note_failure(token, "键位不可用（需带修饰键，且只支持一段组合）")
        mods, vk = parsed
        self._ensure_filter()
        hid = self._next
        self._next += 1
        try:
            ok = bool(self._user32.RegisterHotKey(None, hid, mods, vk))
            code = 0 if ok else _last_error(self._user32)
        except OSError as e:
            return self._note_failure(token, "注册异常：%s" % e)
        if not ok:
            return self._note_failure(token, _fail_reason(code), code)
        self._ids[token] = hid
        self._tokens[hid] = token
        self._keys[token] = (mods, vk)
        self.failures.pop(token, None)
        self.reg_tid = _current_tid()
        self._log("已注册 %s → %s（mods=0x%04X vk=0x%02X，线程 %d）"
                  % (token, seq, mods, vk, self.reg_tid))
        self._install_hook()              # 有键位挂着才需要钩子；重复调用幂等
        return True

    def _log(self, msg, warn=False):
        """热键链路上的事都写成日志（不能影响功能，所以全包在 try 里）"""
        try:
            from core.logger import log
            (log.warning if warn else log.info)("全局热键 %s" % msg)
        except Exception:
            pass

    def _note_failure(self, token, reason, code=0):
        """记下失败原因并返回 False（调用方直接 return 这个就行）。

        界面那行小字只有正在设键的人看得见；日志才是“用户说按了没反应”之后
        唯一查得到的线索——本轮排查就是因为一条热键日志都没有，只能从外面拿探针猜。"""
        self.failures[token] = reason
        self._log("%s 没注册上：%s%s" % (token, reason,
                                        "（系统码 %d）" % code if code else ""),
                  warn=True)
        return False

    def unregister(self, token):
        """反注册一个键位（改键/清键/退出都要走这里）；返回是否真的注册过"""
        hid = self._ids.pop(token, None)
        self._keys.pop(token, None)
        if hid is None:
            self.failures.pop(token, None)
            return False
        self._tokens.pop(hid, None)
        try:
            self._user32.UnregisterHotKey(None, hid)
        except OSError:
            pass
        if not self._keys:
            self._remove_hook()           # 一个键位都不剩了，没理由再拦着全系统的键盘
        return True

    def unregister_all(self):
        for token in list(self._ids):
            self.unregister(token)

    def bound_tokens(self):
        return set(self._ids)

    def is_registered(self, token):
        return token in self._ids

    def set_callback(self, token, callback):
        """登记 token 被按下时执行的动作（回调在主线程同步执行）"""
        self._callbacks[token] = callback

    def forget(self, token):
        """解绑键位并丢掉动作（工具下架/清键时调用，免得回调攥着窗口不放）"""
        self.unregister(token)
        self._callbacks.pop(token, None)

    # ---------- 派发 ----------
    def _ensure_filter(self):
        if self._installed:
            return
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self)
            self._installed = True

    def nativeEventFilter(self, eventType, message):
        """截 WM_HOTKEY。必须在这里同步派发：Qt 允许在其它线程访问该 MSG 指针，
        排队回主线程反而可能读到已失效的地址。"""
        if not is_supported() or message is None:
            return False, 0
        if bytes(eventType) != b"windows_generic_MSG":
            return False, 0
        try:
            msg = _wintypes.MSG.from_address(int(message))
        except (OSError, ValueError):
            return False, 0
        if msg.message != WM_HOTKEY:
            return False, 0
        token = self._tokens.get(int(msg.wParam))
        if token is None:
            return False, 0
        self._try_fire(token, "系统投递")
        return True, 0

    def dispatch(self, token, via="系统投递"):
        """执行 token 的动作；单独成方法，测试可直接调它验路由而不碰 Win API"""
        cb = self._callbacks.get(token)
        if cb is None:
            # 键位还在系统里、动作却没挂上：按下去不会有任何反应，不记一笔就是隐形坑
            self._log("命中 %s，但没挂动作（被 forget 掉了？）" % token, warn=True)
            return
        self._log("命中 %s（%s）" % (token, via))
        cb(token)

    # ---------- 兜底投递通道：本进程自己的低级键盘钩子 ----------
    def _try_fire(self, token, via):
        """两条通道都汇聚到这里，只放行先到的那一次。

        并存是有意为之：正常机器上系统投递就够用（而且 Windows 会把这组按键从其它
        软件那里吃掉），本机则反过来——注册得上、一条 WM_HOTKEY 都收不到。不去重的话
        一次按键会弹两次面板，第二次正好把刚弹出来的收掉，看起来就是“闪一下没了”。"""
        now = time.monotonic()
        if now - self._fired.get(token, 0.0) < DOUBLE_FIRE_SECONDS:
            return False
        self._fired[token] = now
        self.dispatch(token, via=via)
        return True

    def _mods_async(self):
        """现问一次修饰键状态（GetAsyncKeyState 高位＝此刻按着）。

        比自己在钩子里记账可靠：错过一个 keyup（比如钩子装上去时用户已经按着 Alt）
        状态就永久脏了，之后会把用户根本没按的组合键当成按了。"""
        flags = 0
        if self._user32 is None or not is_supported():
            return 0
        for vk, mod in _MOD_VKS:
            try:
                if self._user32.GetAsyncKeyState(vk) & 0x8000:
                    flags |= mod
            except (AttributeError, OSError, TypeError):
                continue                  # 假句柄没这个方法／调不了：当没按下
        return flags

    def find_hotkey(self, vk, mods):
        """(修饰键, 键) -> token，没对上给 None。

        表里的 mods 带着 MOD_NOREPEAT，那是 RegisterHotKey 的口径，与“此刻按着什么”
        无关，比对前先抹掉。"""
        want = mods & (MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN)
        for token, (m, v) in self._keys.items():
            if v == vk and (m & ~MOD_NOREPEAT) == want:
                return token
        return None

    def on_key_event(self, vk, down, mods=None):
        """这次按键该派发哪个 token（没有则 None）；单独成方法是为了不碰 Win32 也能测。

        只认“刚按下的那一下”：按住不放时系统每秒连发几十个 keydown（本机实测按住一次
        左 Alt 连发 30 多条），不去重就会一路把窗口弹到前台。"""
        if not down:
            self._held.discard(vk)
            return None
        if vk in self._held:
            return None
        self._held.add(vk)
        return self.find_hotkey(vk, self._mods_async() if mods is None else mods)

    def _hook_proc(self, ncode, wparam, lparam):
        """钩子回调：只取键码 + 把动作甩回事件循环，这里绝不建窗口。

        钩子慢一点，Windows 会超时把钩子悄悄摘掉（不报任何错），而且这期间整个系统的
        键盘都要等我这条钩子返回——所以真活儿交给 QTimer 下一步做。
        一律放行（返回值交给 CallNextHookEx）：我们不吞用户的任何一个按键。
        异常绝不能抛出去：抛进原生代码等于把整个进程一起带崩。"""
        token = None
        try:
            # lparam 先判空：from_address 读到非法地址不会抛异常，而是直接 access violation 把进程崩掉
            if ncode >= 0 and lparam and wparam in _KEY_DOWN_MSGS + _KEY_UP_MSGS:
                vk = int(_KBDLLHOOKSTRUCT.from_address(int(lparam)).vkCode)
                token = self.on_key_event(vk, wparam in _KEY_DOWN_MSGS)
        except Exception:
            token = None
        if token:
            self._fire_soon(token)
        try:
            return self._user32.CallNextHookEx(self._hook, ncode, wparam, lparam)
        except Exception:
            return 0

    def _fire_soon(self, token):
        """把动作排回事件循环（见 _hook_proc：钩子回调必须快进快出）"""
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._try_fire(token, "键盘钩子"))
        except Exception:
            self._try_fire(token, "键盘钩子")

    def _install_hook(self):
        """挂上兜底的低级键盘钩子（只有键位注册着时才挂，装不上不影响主通道）"""
        if self._hook or not is_supported() or self._user32 is None:
            return bool(self._hook)
        try:
            self._hook_cb = _HOOK_PROC(self._hook_proc)
            self._hook = self._user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_cb,
                                                       None, 0)
        except Exception as e:
            self._hook = None
            self._hook_cb = None
            self._log("兜底键盘钩子没装上（异常：%s）：全局快捷键只能依赖系统投递" % e,
                      warn=True)
            return False
        if not self._hook:
            self._hook_cb = None
            self._log("兜底键盘钩子没装上（码 %d）：全局快捷键只能依赖系统投递"
                      % _last_error(self._user32), warn=True)
            return False
        self._log("兜底通道已挂上：本进程键盘钩子（与系统投递去重，不吞任何按键）")
        return True

    def _remove_hook(self):
        if not self._hook:
            return
        try:
            self._user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        self._hook = None
        # 回调本体必须一起松掉：钩子还挂着而 Python 把回调 GC 掉了，
        # 原生代码回调进来就是野指针，直接崩进程
        self._hook_cb = None
        self._held.clear()


#: 进程内单例：主窗口注册、设置页读失败原因、退出时统一反注册
manager = GlobalHotkeyManager()
