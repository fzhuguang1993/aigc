"""
gui/quicklook.py —— QuickLook 式「空格预览」：选中文件按空格即弹纯预览浮层

对标商业 QuickLook：在资源管理器 / 桌面上选中一个文件，按空格弹出一个只有预览、
没有其它 UI 的浮层；再按空格（或 Esc）关掉，↑↓ 在相邻文件间切换。软件在跑
（哪怕收进托盘）时全局生效。

三条实现主线，每条都对应一个"不这么做就会出事"的坑：

1) 触发不能用 RegisterHotKey。裸空格是打字键，gui/global_hotkey.py 的
   parse_sequence 明确拒绝无修饰键的普通键（否则会把用户所有软件里的空格全吞了）。
   QuickLook 能商用靠的是「低级键盘钩子 WH_KEYBOARD_LL + 前台判定」：只在
   前台确实是资源管理器/桌面、且焦点在文件列表（不在地址栏/搜索框那种能打字的
   控件里）时才把空格截下来弹预览，其余场景一律放行（CallNextHookEx），不打扰打字。

2) "选中了哪个文件"走 Shell.Application COM（comtypes 晚绑定，不需要 typelib 代码
   生成，对 PyInstaller 友好）。本机实测：Document.SelectedItems 逐个 .Path 能拿到
   资源管理器/桌面选中项的绝对路径。这比裸 ctypes 写 UIA 的 COM vtable 稳得多。

3) 预览内容复用 gui/launcher_preview.PreviewPane（image/video/pdf/docx/xlsx/pptx/
   txt/md/html 全支持，含它自己那套后台线程 + 序号守卫 + 线程纪律）。本模块绝不
   另起一套解码逻辑——预览引擎的铁律（后台线程不碰 QPainter/QFont）由它统一守着。

⚠ 钩子回调必须快进快出：钩子慢一点，Windows 会超时把钩子悄悄摘掉，这期间全系统
  键盘都排队等我们返回。所以钩子里只做「前台类名 / 焦点控件」这种纯 user32 的快判断，
  真正的选文件（COM）和建窗口都甩给 QTimer.singleShot 回事件循环再做。
⚠ 本进程非管理员时，焦点在"以管理员身份运行"的窗口里钩子收不到按键（Windows 完整性
  级别规则，非提权程序都这样）；正常办公 / 浏览器 / 资源管理器不受影响。
"""
import ctypes
import os
import sys

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from gui import launcher_preview as pv
from gui.window_foreground import raise_to_front

try:                                    # wintypes 只在 Windows 上存在
    from ctypes import wintypes as _wintypes
except (ImportError, ValueError):
    _wintypes = None


def _win():
    return sys.platform.startswith("win") and _wintypes is not None


# ---------- 虚拟键码 / 钩子常量（与 global_hotkey 同一套编码，这里只取用到的） ----------
VK_SPACE, VK_ESCAPE, VK_UP, VK_DOWN = 0x20, 0x1B, 0x26, 0x28
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
_DOWN_MSGS = (WM_KEYDOWN, WM_SYSKEYDOWN)

# 前台是这些窗口类时，空格才可能被当作 QuickLook 触发。
# CabinetWClass/ExploreWClass = 资源管理器窗口；Progman/WorkerW = 桌面。
_EXPLORER_CLASSES = {"CabinetWClass", "ExploreWClass"}
_DESKTOP_CLASSES = {"Progman", "WorkerW"}
# 焦点控件类名：DirectUIHWND 是资源管理器的文件列表 / 地址栏（靠祖先链区分，见下）；
# SysListView32 是桌面的图标列表。
_LIST_CLASS = "DirectUIHWND"
_DESKTOP_LIST_CLASS = "SysListView32"
# 地址栏 / 搜索框所在的带（band）类名：焦点控件若挂在这下面，说明用户在打字，
# 绝不能截空格。命中其一就把这次空格当普通按键放行。
_BAND_BLOCK = {"Address Band Root", "Address Bar", "Search Band",
               "Search EditBox", "TraySearchBox"}

_OVERLAY_W_RATIO = 0.6          # 无确定宽高比时（文本/文档）浮层宽占可用屏比例
_OVERLAY_H_RATIO = 0.72
_OVERLAY_MAX_RATIO = 0.86       # 图片/视频按内容宽高比适配时，长边最多占可用屏这么多
_OVERLAY_MIN_W = 360            # 适配后的最小宽（别让一张小图标缩成指甲盖）
_OVERLAY_CHROME = 44           # 顶部标题条预留高度（px），算内容尺寸时扣掉
_WATCH_MS = 260                 # 浮层开着时轮询前台是否已切走的间隔


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.c_ulong), ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


class _GUITHREADINFO(ctypes.Structure):
    """GetGUIThreadInfo 用：cbSize 必须先填 sizeof，否则函数直接返回 FALSE。"""
    _fields_ = [("cbSize", _wintypes.DWORD if _wintypes else ctypes.c_ulong),
                ("flags", ctypes.c_ulong),
                ("hwndActive", ctypes.c_void_p), ("hwndFocus", ctypes.c_void_p),
                ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p),
                ("rcCaret", ctypes.c_long * 4)]


class _LVITEMW(ctypes.Structure):
    """跨进程读桌面图标名用的 LVITEM（Unicode）。桌面图标宿主是标准 SysListView32
    （FolderView），能响应 LVM_* ；结构体内存布局须与目标进程一致，ctypes 默认对齐即 C 布局。"""
    _fields_ = [("mask", ctypes.c_uint), ("iItem", ctypes.c_int),
                ("iSubItem", ctypes.c_int), ("state", ctypes.c_uint),
                ("stateMask", ctypes.c_uint), ("pszText", ctypes.c_void_p),
                ("cchTextMax", ctypes.c_int), ("iImage", ctypes.c_int),
                ("lParam", ctypes.c_void_p), ("iIndent", ctypes.c_int),
                ("iGroupId", ctypes.c_int)]


class _POINT(ctypes.Structure):
    """POINT：x64 下仍是两个 32 位 LONG（不是指针宽），共 8 字节。"""
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _LVHITTESTINFO(ctypes.Structure):
    """LVHITTESTINFO：{ POINT pt; UINT flags; int iItem; int iSubItem; int iGroup; }，
    x64 下紧排 24 字节。pt 用目标列表的客户区坐标，命中后 iItem 回填。"""
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long),
                ("flags", ctypes.c_uint), ("iItem", ctypes.c_int),
                ("iSubItem", ctypes.c_int), ("iGroup", ctypes.c_int)]


# 桌面列表跨进程读取用到的消息 / 标志（commctrl.h）
LVM_FIRST = 0x1000
LVM_GETNEXTITEM = LVM_FIRST + 12          # 0x100C
LVM_GETITEMTEXTW = LVM_FIRST + 115        # 0x1073
LVM_SUBITEMHITTEST = LVM_FIRST + 129      # 0x1081（按客户区坐标命中项，不看选中态）
LVNI_SELECTED = 0x0002
LVIF_TEXT = 0x0001
SMTO_ABORTIFHUNG = 0x0002
_PROCESS_VM = 0x0008 | 0x0010 | 0x0020    # OPERATION|READ|WRITE
_PROCESS_QUERY_LIMITED = 0x1000           # 只能查进程镜像名（受保护进程也能开，供封面识别）
_MEM_COMMIT_RESERVE = 0x1000 | 0x2000
_PAGE_READWRITE = 0x04
_MEM_RELEASE = 0x8000


#: WINFUNCTYPE 是 Windows 专有；非 Win 置 None，模块仍能被 import（设置页等依赖本模块）。
if sys.platform.startswith("win"):
    _HOOK_PROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_void_p)
else:
    _HOOK_PROC = None


def _load_user32():
    """取 user32 并钉好签名：64 位下不声明 restype，HWND 会被截成低 32 位——
    每一个调用都"成功"，合起来一次都不生效（同 window_foreground 的那桩坑）。"""
    if not _win():
        return None
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = _wintypes.HWND
    u.GetClassNameW.argtypes = [_wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    u.GetWindowThreadProcessId.argtypes = [_wintypes.HWND, ctypes.POINTER(_wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = _wintypes.DWORD
    u.GetAncestor.argtypes = [_wintypes.HWND, ctypes.c_uint]
    u.GetAncestor.restype = _wintypes.HWND
    u.GetParent.argtypes = [_wintypes.HWND]
    u.GetParent.restype = _wintypes.HWND
    u.GetGUIThreadInfo.argtypes = [_wintypes.UINT, ctypes.POINTER(_GUITHREADINFO)]
    u.GetGUIThreadInfo.restype = _wintypes.BOOL
    u.IsWindow.argtypes = [_wintypes.HWND]
    u.IsWindow.restype = _wintypes.BOOL
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                    _wintypes.HINSTANCE, _wintypes.DWORD]
    u.SetWindowsHookExW.restype = ctypes.c_void_p
    u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    u.UnhookWindowsHookEx.restype = _wintypes.BOOL
    u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                 ctypes.c_void_p, ctypes.c_void_p]
    u.CallNextHookEx.restype = ctypes.c_int
    u.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u.GetAsyncKeyState.restype = ctypes.c_short
    # 跨进程读桌面 SysListView32 用：SendMessageTimeoutW 防 explorer 卡住时把主线程也拖死
    u.SendMessageTimeoutW.restype = ctypes.c_size_t
    u.SendMessageTimeoutW.argtypes = [_wintypes.HWND, ctypes.c_uint,
                                      _wintypes.WPARAM, _wintypes.LPARAM,
                                      ctypes.c_uint, ctypes.c_uint,
                                      ctypes.POINTER(ctypes.c_ulong)]
    u.IsWindow.argtypes = [_wintypes.HWND]
    u.IsWindow.restype = _wintypes.BOOL
    # 桌面图标列表定位用：递归子窗 + 顶层枚举
    u.GetWindow.restype = _wintypes.HWND
    u.GetWindow.argtypes = [_wintypes.HWND, ctypes.c_uint]
    u.FindWindowExW.restype = _wintypes.HWND
    u.FindWindowExW.argtypes = [_wintypes.HWND, _wintypes.HWND,
                                _wintypes.LPCWSTR, _wintypes.LPCWSTR]
    u.EnumWindows.restype = _wintypes.BOOL
    u.EnumWindows.argtypes = [ctypes.c_void_p, _wintypes.LPARAM]
    # 桌面命中测试用：鼠标屏幕坐标 → 目标列表客户区坐标（配合 LVM_SUBITEMHITTEST）
    u.GetCursorPos.restype = _wintypes.BOOL
    u.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    u.ScreenToClient.restype = _wintypes.BOOL
    u.ScreenToClient.argtypes = [_wintypes.HWND, ctypes.POINTER(_POINT)]
    return u


#: kernel32 只加载一次；64 位下 VirtualAllocEx 返回的是指针，restype 必须钉成
#: c_void_p，否则被截成低 32 位——写进 explorer 的远程内存就成了野地址（同 HWND 那桩坑）。
_KERNEL32 = None


def _load_kernel32():
    global _KERNEL32
    if not _win():
        return None
    if _KERNEL32 is not None:
        return _KERNEL32
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = _wintypes.HANDLE
    k.OpenProcess.argtypes = [_wintypes.DWORD, _wintypes.BOOL, _wintypes.DWORD]
    k.CloseHandle.argtypes = [_wintypes.HANDLE]
    k.VirtualAllocEx.restype = ctypes.c_void_p
    k.VirtualAllocEx.argtypes = [_wintypes.HANDLE, ctypes.c_void_p,
                                 ctypes.c_size_t, _wintypes.DWORD, _wintypes.DWORD]
    k.VirtualFreeEx.argtypes = [_wintypes.HANDLE, ctypes.c_void_p,
                                ctypes.c_size_t, _wintypes.DWORD]
    k.WriteProcessMemory.restype = _wintypes.BOOL
    k.WriteProcessMemory.argtypes = [_wintypes.HANDLE, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.POINTER(ctypes.c_size_t)]
    k.ReadProcessMemory.restype = _wintypes.BOOL
    k.ReadProcessMemory.argtypes = [_wintypes.HANDLE, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.POINTER(ctypes.c_size_t)]
    # 查前台窗口所属进程镜像名（识别元气壁纸/桌面整理等桌面覆盖程序用）
    k.QueryFullProcessImageNameW.restype = _wintypes.BOOL
    k.QueryFullProcessImageNameW.argtypes = [_wintypes.HANDLE, _wintypes.DWORD,
                                             ctypes.c_wchar_p,
                                             ctypes.POINTER(_wintypes.DWORD)]
    _KERNEL32 = k
    return k


def _class_of(u, hwnd):
    buf = ctypes.create_unicode_buffer(256)
    try:
        n = u.GetClassNameW(hwnd, buf, 256)
    except Exception:
        return ""
    return buf.value if n else ""


#: 修饰键通用码 + 左右变体（与 global_hotkey 同一套）：低级钩子不直接报修饰键，现问最可靠
_MOD_VKS = (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)


def _mods_down(u):
    """此刻是否按着任修饰键（Ctrl/Alt/Shift/Win）。高位＝按着。

    QuickLook 只认“裸空格”：带任修饰键的空格（比如总唤出面板默认键 Ctrl+Alt+Space）
    必须放行给系统热键，不能当成预览触发——否则两个功能撞在同一颗键上。"""
    for vk in _MOD_VKS:
        try:
            if u.GetAsyncKeyState(vk) & 0x8000:
                return True
        except (AttributeError, OSError, TypeError):
            continue
    return False


# ---------------------------------------------------------------------------
# 前台判定：只在"文件列表拿到焦点"时才考虑截空格（不这么做就会吞掉打字）
# ---------------------------------------------------------------------------
def _focus_hwnd(u, fg):
    tid = u.GetWindowThreadProcessId(fg, None)
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    try:
        if not u.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return 0
        return int(info.hwndFocus or 0)
    except Exception:
        return 0


def _under_blocked_band(u, hwnd):
    """沿父链往上找，命中地址栏/搜索框所在的带就返回 True。"""
    cur = hwnd
    hops = 0
    while cur and hops < 24:
        if _class_of(u, cur) in _BAND_BLOCK:
            return True
        try:
            cur = int(u.GetParent(cur) or 0)
        except Exception:
            break
        hops += 1
    return False


def _is_file_list_focus(u, fg):
    """焦点是否真的落在"文件列表"上（而不是资源管理器里的地址栏/搜索框、或别的软件）。

    判不准就返回 False——宁可这次不弹（用户能立刻再按一次），也绝不能把用户在
    打字框里的空格截走，那才是 QuickLook 式功能最容易招骂的地方。

    桌面特殊：前台是 Progman/WorkerW 时，唯一能拿焦点的就是图标列表（桌面上没
    有可打字控件），所以直接放行——不再强依赖 GetGUIThreadInfo 能跨线程拿到那个
    SysListView32（图标宿主常在 Progman 的兄弟 WorkerW 下，焦点线程判定不稳）。"""
    fg_cls = _class_of(u, fg)
    if fg_cls in _DESKTOP_CLASSES:
        return True                       # 桌面图标列表
    f = _focus_hwnd(u, fg)
    if not f:
        return False
    cls = _class_of(u, f)
    if cls == _LIST_CLASS and fg_cls in _EXPLORER_CLASSES:
        # DirectUIHWND 既是文件列表也是地址栏；地址栏那一支挂在 "Address Band Root" 下
        return not _under_blocked_band(u, f)
    return False


#: 第三方“壁纸/桌面整理”会盖在桌面上抢走前台焦点（元气壁纸 kwallpaper、
#: 猎豹/金山桌面整理 kdesk、Wallpaper Engine 等），此时前台类不是 Progman/WorkerW，
#: 但桌面图标仍挂在原生 SysListView32 上——认出这些进程，就把空格仍按“桌面”处理。
#: 壁纸/整理程序对空格本来就无动作，抢一下无害。
_DESKTOP_COVER_TOKENS = ("kwallpaper", "kdesk", "wallpaperengine", "wallpaper32", "wallpaper64")
_cover_cache = {}                        # pid -> (是否覆盖类, 进程短名)


def _fg_pid(u, fg):
    pid = _wintypes.DWORD(0)
    try:
        u.GetWindowThreadProcessId(int(fg) & 0xFFFFFFFF, ctypes.byref(pid))
    except Exception:
        return 0
    return int(pid.value or 0)


def _resolve_cover(u, p):
    """解析 pid 的进程镜像名并按 token 判定是否桌面覆盖类；结果缓存 pid->(bool, 短名)。
    首次会做 OpenProcess+QueryFullProcessImageName（亚毫秒）后走缓存，钩子里也便宜。"""
    if p in _cover_cache:
        return _cover_cache[p]
    name = ""
    k = _load_kernel32()
    if k:
        h = None
        try:
            h = k.OpenProcess(_PROCESS_QUERY_LIMITED, False, p)
            if h:
                buf = ctypes.create_unicode_buffer(1024)
                size = _wintypes.DWORD(1024)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = (buf.value or "").lower()
        except Exception:
            name = ""
        finally:
            try:
                if h:
                    k.CloseHandle(h)
            except Exception:
                pass
    base = name.split("\\")[-1] if name else ""
    hit = any(tok in name for tok in _DESKTOP_COVER_TOKENS)
    if len(_cover_cache) > 256:          # pid 会随进程重启换，缓存兜个底别再无限增长
        _cover_cache.clear()
    _cover_cache[p] = (hit, base)
    return _cover_cache[p]


def _is_desktop_cover(u, fg):
    """前台窗口所属进程是否为已知的桌面覆盖/壁纸/整理程序（按 pid 缓存，钩子里便宜）。"""
    if not u or not fg:
        return False
    p = _fg_pid(u, fg)
    if not p:
        return False
    return _resolve_cover(u, p)[0]


def foreground_process_name(fg):
    """给诊断/上层用：前台窗口所属进程短名（拿不到返回 ""）。不在钩子里调。"""
    if not _win() or not fg:
        return ""
    u = _load_user32()
    p = _fg_pid(u, fg)
    if not p:
        return ""
    return _resolve_cover(u, p)[1]


def _find_child_by_class(u, parent, want, max_depth=6):
    """在 parent 的子孙窗里广度找类名为 want 的第一个，返回 hwnd（无则 0）。"""
    if not parent:
        return 0
    stack = [(int(parent), 0)]
    seen = 0
    while stack and seen < 4000:
        h, d = stack.pop()
        c = u.GetWindow(h, 5)                     # GW_CHILD
        while c:
            ci = int(c) & 0xFFFFFFFF
            seen += 1
            if _class_of(u, ci) == want:
                return ci
            if d < max_depth:
                stack.append((ci, d + 1))
            c = u.GetWindow(c, 2)                 # GW_HWNDNEXT
    return 0


def _find_desktop_listview(u):
    """定位桌面图标 SysListView32：遍历顶层 Progman/WorkerW，在其下找
    SHELLDLL_DefView → SysListView32。找不到返回 0。"""
    if not u:
        return 0
    out = {"h": 0}

    def _enum_cb(hwnd, _lparam):
        hi = int(hwnd) & 0xFFFFFFFF
        if _class_of(u, hi) in _DESKTOP_CLASSES:
            dv = _find_child_by_class(u, hi, "SHELLDLL_DefView")
            if dv:
                lv = _find_child_by_class(u, dv, _DESKTOP_LIST_CLASS, max_depth=3)
                if lv:
                    out["h"] = lv
                    return False                  # 停止枚举
        return True

    try:
        cb = ctypes.WINFUNCTYPE(ctypes.c_bool, _wintypes.HWND, _wintypes.LPARAM)(_enum_cb)
        u.EnumWindows(cb, 0)
    except Exception:
        return 0
    return out["h"]


def is_file_browser_foreground(fg):
    """给测试/上层用：前台是否资源管理器/桌面（不含焦点细节的粗判）。"""
    if not _win() or not fg:
        return False
    u = _load_user32()
    return _class_of(u, int(fg)) in (_EXPLORER_CLASSES | _DESKTOP_CLASSES)


# ---------------------------------------------------------------------------
# 选中项读取：Shell.Application COM（comtypes 晚绑定，PyInstaller 友好）
# ---------------------------------------------------------------------------
def _val(obj, name):
    """comtypes 晚绑定把 IFolder/FolderItem 的属性返回成可调用对象，取值要调一下；
    不同版本也可能直接给值。两种形态都兜住。"""
    a = getattr(obj, name)
    return a() if callable(a) else a


def selected_paths(fg):
    """资源管理器/桌面里当前选中项的绝对路径列表；出错/无匹配返回 []。

    在主线程（钩子甩出来的 deferred 槽）调用——COM 往返不是钩子该干的慢活。
    按窗口 HWND 匹配到 fg 那一个，避免把后台另开的资源管理器窗口的选区读出来。"""
    try:
        import comtypes.client as cc
    except Exception:
        return []
    fg = int(fg or 0) & 0xFFFFFFFF     # 归一：GetForegroundWindow 高位地址会返成负 int，
    if not fg:                          # 而下面按 Shell 窗口的 HWND（同样取无符号）比对，不抹
        return []                       # 符号就永远配不上（本机实测那个窗口静默返 []）
    try:
        app = cc.CreateObject("Shell.Application", dynamic=True)
        wins = app.Windows()
    except Exception:
        return []
    try:
        count = int(_val(wins, "Count"))
    except Exception:
        count = 0
    for i in range(count):
        try:
            w = wins.Item(i)
            hw = int(_val(w, "HWND")) & 0xFFFFFFFF
        except Exception:
            continue
        if hw != fg:
            continue
        try:
            doc = w.Document
            sel = _val(doc, "SelectedItems")
            n = int(_val(sel, "Count"))
            out = []
            for k in range(n):
                it = sel.Item(k)
                p = _val(it, "Path")
                if p:
                    out.append(str(p))
            return out
        except Exception:
            return []
    return []


# ---------------------------------------------------------------------------
# 桌面选中项：真桌面（Progman/WorkerW 下的 SysListView32）不进 Shell.Application.Windows()，
# 只能跨进程发 LVM_* 读选中图标名，再回桌面目录里核成真实路径。
# ---------------------------------------------------------------------------
def _lv_text(k, u, hproc, listview, idx):
    """读桌面列表第 idx 项的显示名（跨进程：结构体与文本缓冲各自 VirtualAllocEx）。"""
    textbuf = k.VirtualAllocEx(hproc, None, ctypes.c_size_t(2048),
                               _MEM_COMMIT_RESERVE, _PAGE_READWRITE)
    if not textbuf:
        return ""
    sbuf = None
    try:
        it = _LVITEMW()
        it.mask = LVIF_TEXT
        it.iItem = idx
        it.iSubItem = 0
        it.pszText = textbuf
        it.cchTextMax = 1024
        sbuf = k.VirtualAllocEx(hproc, None, ctypes.c_size_t(ctypes.sizeof(it)),
                                _MEM_COMMIT_RESERVE, _PAGE_READWRITE)
        if not sbuf:
            return ""
        wrote = ctypes.c_size_t(0)
        if not k.WriteProcessMemory(hproc, sbuf, ctypes.byref(it),
                                    ctypes.c_size_t(ctypes.sizeof(it)),
                                    ctypes.byref(wrote)):
            return ""
        res = ctypes.c_ulong(0)
        u.SendMessageTimeoutW(listview, LVM_GETITEMTEXTW, idx, sbuf,
                              SMTO_ABORTIFHUNG, 200, ctypes.byref(res))
        out = ctypes.create_unicode_buffer(1024)
        rd = ctypes.c_size_t(0)
        if not k.ReadProcessMemory(hproc, textbuf, out, ctypes.c_size_t(2048),
                                   ctypes.byref(rd)):
            return ""
        return out.value or ""
    finally:
        for p in (sbuf, textbuf):
            if p:
                try:
                    k.VirtualFreeEx(hproc, p, 0, _MEM_RELEASE)
                except Exception:
                    pass


def _lv_selected_texts(listview):
    """桌面 SysListView32 里当前选中项的显示名列表（读不到/非桌面返回 []）。"""
    if not _win() or not listview:
        return []
    u = _load_user32()
    k = _load_kernel32()
    if u is None or k is None:
        return []
    try:
        listview = int(listview) & 0xFFFFFFFF
    except Exception:
        return []
    if not u.IsWindow(listview):
        return []
    pid = _wintypes.DWORD(0)
    u.GetWindowThreadProcessId(listview, ctypes.byref(pid))
    if not pid.value:
        return []
    hproc = k.OpenProcess(_PROCESS_VM, False, pid.value)
    if not hproc:
        return []
    try:
        names, i, guard = [], -1, 0
        while guard < 64:
            res = ctypes.c_ulong(0)
            # SendMessageTimeoutW 的返回值是“成功与否”的 BOOL，真正的 LRESULT（下一
            # 个选中项下标）在出参 res 里。拿返回值当索引会永远停在同一项（实测重复 63 次）。
            ret = u.SendMessageTimeoutW(listview, LVM_GETNEXTITEM, i,
                                        LVNI_SELECTED, SMTO_ABORTIFHUNG,
                                        200, ctypes.byref(res))
            if not ret:
                break
            idx = res.value & 0xFFFFFFFF
            if idx >= 0x80000000:                 # -1（c_ulong 里是 0xFFFFFFFF）：没有更多选中项
                break
            t = _lv_text(k, u, hproc, listview, idx)
            if t and t.strip():
                names.append(t.strip())
            i = idx
            guard += 1
        return names
    except Exception:
        return []
    finally:
        try:
            k.CloseHandle(hproc)
        except Exception:
            pass


def _lv_hit_texts_at_cursor(listview):
    """拿不到选中态时的兑底：按鼠标当前位置对桌面列表做几何命中（LVM_SUBITEMHITTEST），
    返回命中的那一项显示名。壁纸/桌面整理抢走前台后，原生桌面列表会丢掉 LVIS_SELECTED，
    但图标几何位置还在——命中测试不依赖焦点/选中，所以能读出来。

    坐标换算：GetCursorPos 拿屏幕坐标 → ScreenToClient 化到目标列表客户区 → 写进远程
    LVHITTESTINFO 发 LVM_SUBITEMHITTEST → 回读 iItem。命不中（-1）或异常返回 []。"""
    if not _win() or not listview:
        return []
    u = _load_user32()
    k = _load_kernel32()
    if u is None or k is None:
        return []
    try:
        listview = int(listview) & 0xFFFFFFFF
    except Exception:
        return []
    if not u.IsWindow(listview):
        return []
    pt = _POINT()
    try:
        if not u.GetCursorPos(ctypes.byref(pt)):
            return []
        if not u.ScreenToClient(listview, ctypes.byref(pt)):
            return []
    except Exception:
        return []
    pid = _wintypes.DWORD(0)
    u.GetWindowThreadProcessId(listview, ctypes.byref(pid))
    if not pid.value:
        return []
    hproc = k.OpenProcess(_PROCESS_VM, False, pid.value)
    if not hproc:
        return []
    try:
        hi = _LVHITTESTINFO()
        hi.x, hi.y = int(pt.x), int(pt.y)
        remote = k.VirtualAllocEx(hproc, None, ctypes.c_size_t(ctypes.sizeof(hi)),
                                  _MEM_COMMIT_RESERVE, _PAGE_READWRITE)
        if not remote:
            return []
        try:
            wrote = ctypes.c_size_t(0)
            if not k.WriteProcessMemory(hproc, remote, ctypes.byref(hi),
                                        ctypes.c_size_t(ctypes.sizeof(hi)),
                                        ctypes.byref(wrote)):
                return []
            res = ctypes.c_ulong(0)
            u.SendMessageTimeoutW(listview, LVM_SUBITEMHITTEST, 0, remote,
                                  SMTO_ABORTIFHUNG, 200, ctypes.byref(res))
            back = _LVHITTESTINFO()
            rd = ctypes.c_size_t(0)
            if not k.ReadProcessMemory(hproc, remote, ctypes.byref(back),
                                       ctypes.c_size_t(ctypes.sizeof(back)),
                                       ctypes.byref(rd)):
                return []
            idx = int(back.iItem)
            if idx < 0 or idx >= 0x80000000:
                return []
            t = _lv_text(k, u, hproc, listview, idx)
            return [t.strip()] if t and t.strip() else []
        finally:
            try:
                k.VirtualFreeEx(hproc, remote, 0, _MEM_RELEASE)
            except Exception:
                pass
    except Exception:
        return []
    finally:
        try:
            k.CloseHandle(hproc)
        except Exception:
            pass


def _desktop_dirs():
    """桌面真实目录：用户桌面 + 公共桌面（图标是两者合并视图）。"""
    dirs = []
    try:
        buf = ctypes.create_unicode_buffer(_wintypes.MAX_PATH)
        # CSIDL_DESKTOP = 0
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, buf) == 0 and buf.value:
            dirs.append(buf.value)
    except Exception:
        pass
    try:
        pub = os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop")
        if os.path.isdir(pub):
            dirs.append(pub)
    except Exception:
        pass
    return dirs


def _lnk_target(path):
    """解析 .lnk 快捷方式的目标路径（走 WScript.Shell，系统自带无需额外库）。
    不是快捷方式/解不出返回 ""。在主线程（act）里调，不在钩子里。"""
    try:
        import comtypes.client as cc
        shell = cc.CreateObject("WScript.Shell", dynamic=True)
        sc = shell.CreateShortcut(str(path))
        tp = sc.TargetPath
        tp = tp() if callable(tp) else tp
        return str(tp) if tp else ""
    except Exception:
        return ""


def _resolve_desktop_paths(names):
    """把图标显示名核成真实路径：桌面目录建索引（含名/去扩展名两键），优先取可预览项。

    桌面列表返回的名字有时带扩展名（AI拆解.docx）、有时是文件夹/应用名（SakuraCat），
    两种键都备上；.lnk 快捷方式会解出目标再判可否预览（桌面上绝大多数图标都是快捷方式，
    不跟一下就“读到名字却不弹”）；目标也不存预览就自然跳过。"""
    index = {}
    for d in _desktop_dirs():
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for n in entries:
            full = os.path.join(d, n)
            low = n.lower()
            base = os.path.splitext(n)[0].lower()
            index.setdefault(low, []).append(full)
            if base != low:
                index.setdefault(base, []).append(full)
    out = []
    for name in names:
        cands = index.get(name.strip().lower()) or []
        # 1) 直接就是可预览文件
        chosen = next((c for c in cands if is_previewable(c)), None)
        # 2) 命中快捷方式 .lnk → 解目标，目标可预览就用目标
        if chosen is None:
            lnk = next((c for c in cands if c.lower().endswith(".lnk")), None)
            if lnk:
                tgt = _lnk_target(lnk)
                if tgt and is_previewable(tgt):
                    chosen = tgt
        # 3) 都不行：退而取第一个候选（可能是文件夹/非预览项，交给上层判）
        if chosen is None and cands:
            chosen = cands[0]
        if chosen:
            out.append(chosen)
    return out


def desktop_selected_paths(listview):
    """桌面当前选中项的真实路径列表。

    先读 LVIS_SELECTED（前台就是桌面时正常）；读不到再按鼠标位置做几何命中
    兑底（被壁纸/桌面整理盖住时选中态会丢，但光标就在图标上）。"""
    texts = _lv_selected_texts(listview)
    if not texts:
        texts = _lv_hit_texts_at_cursor(listview)
    return _resolve_desktop_paths(texts)


# ---------------------------------------------------------------------------
# 预览浮层：无边框、置顶、只装一个 PreviewPane
# ---------------------------------------------------------------------------
def is_previewable(path):
    """是否是预览引擎认识、值得弹层的类型（其它类型按空格不该弹出空浮层）。"""
    try:
        return pv.category(path) != "other"
    except Exception:
        return False


def siblings_for(path):
    """同目录下可预览的文件（按名字排序），供 ↑↓ 切换。目录读不到就只返回自己。"""
    d = os.path.dirname(str(path or ""))
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return [str(path)]
    out = []
    for n in names:
        fp = os.path.join(d, n)
        if os.path.isfile(fp) and is_previewable(fp):
            out.append(fp)
    if str(path) in out:
        return out
    return [str(path)]


def _item_for(path):
    """把磁盘路径拼成 PreviewPane.show_item 需要的 dict（name/dir/size/mtime）。"""
    path = str(path)
    name = os.path.basename(path)
    d = os.path.dirname(path)
    size, mtime = None, None
    try:
        st = os.stat(path)
        size = st.st_size
        mtime = st.st_mtime
    except OSError:
        pass
    return {"path": path, "name": name, "dir": d, "size": size, "mtime": mtime}


class _VideoPage(QWidget):
    """浮层里的视频播放页：QMediaPlayer + QVideoWidget，自动播放 + 单循环。

    只在 QuickLook 浮层用（唤出面板仍走 PreviewPane 的首帧缩略图）。QtMultimedia 延迟导入：
    本模块在非 Windows / 无多媒体后端的环境也要能被 import（设置页依赖），且没装后端时
    上层会自动退回首帧图。拿到分辨率后经 _on_res 回调请浮层重新按宽高比适配尺寸。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
        from PySide6.QtMultimediaWidgets import QVideoWidget
        from PySide6.QtWidgets import QSlider
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.video = QVideoWidget(self)
        self.video.setStyleSheet("background:#000;")
        lay.addWidget(self.video, 1)

        # 控制条：播放/暂停 + 进度条（可拖动定位）+ 时间。以前只有画面没进度条，
        # 播到哪儿、能不能拖回去都看不出来。
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 4, 10, 6)
        bar.setSpacing(8)
        self.btn_play = QPushButton("⏸", self)
        self.btn_play.setFixedSize(34, 24)
        self.btn_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_play.setStyleSheet(
            "QPushButton { color:#E8EAED; background:rgba(255,255,255,0.10);"
            " border:1px solid rgba(255,255,255,0.16); border-radius:12px;"
            " font-size:13px; }"
            " QPushButton:hover { background:rgba(255,255,255,0.18); }")
        self.btn_play.clicked.connect(self._toggle)
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 0)
        self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider.setStyleSheet(
            "QSlider::groove:horizontal { height:4px; background:#3A3D44;"
            " border-radius:2px; }"
            " QSlider::sub-page:horizontal { background:#3370FF;"
            " border-radius:2px; }"
            " QSlider::handle:horizontal { width:12px; margin:-5px 0;"
            " background:#E8EAED; border-radius:6px; }")
        self.slider.sliderMoved.connect(self._seek)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.slider.sliderReleased.connect(self._on_release)
        self.lbl_time = QLabel("00:00 / 00:00", self)
        self.lbl_time.setStyleSheet(
            "color:#C9CED6; font-size:11px; background:transparent;")
        bar.addWidget(self.btn_play)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.lbl_time)
        barwrap = QWidget(self)
        barwrap.setLayout(bar)
        barwrap.setStyleSheet("background:#0F1013;")
        lay.addWidget(barwrap)

        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_dur)
        self.player.playbackStateChanged.connect(self._on_state)
        self._on_res = None                 # 上层注入：拿到 (w,h) 后重新适配浮层
        self._dragging = False

    def play(self, path):
        from PySide6.QtCore import QUrl
        try:
            self.player.stop()
            self.slider.setRange(0, 0)
            self.slider.setValue(0)
            self.lbl_time.setText("00:00 / 00:00")
            self.player.setSource(QUrl.fromLocalFile(os.path.abspath(str(path))))
            self.player.play()
        except Exception:
            pass

    def stop(self):
        try:
            from PySide6.QtCore import QUrl
            self.player.stop()
            self.player.setSource(QUrl())
            self.slider.setRange(0, 0)
            self.lbl_time.setText("00:00 / 00:00")
        except Exception:
            pass

    @staticmethod
    def _fmt(ms):
        s = max(int(ms or 0), 0) // 1000
        return "%02d:%02d" % (s // 60, s % 60)

    def _on_pos(self, pos):
        if not self._dragging:                 # 拖动中不让后台回报把滑块抢回去
            self.slider.setValue(pos)
        self.lbl_time.setText("%s / %s" % (self._fmt(pos),
                                           self._fmt(self.player.duration())))

    def _on_dur(self, dur):
        self.slider.setRange(0, max(int(dur or 0), 0))
        self.lbl_time.setText("00:00 / %s" % self._fmt(dur))

    def _seek(self, v):
        try:
            self.player.setPosition(v)
        except Exception:
            pass

    def _on_release(self):
        self._dragging = False
        self._seek(self.slider.value())

    def _toggle(self):
        from PySide6.QtMultimedia import QMediaPlayer
        try:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()
        except Exception:
            pass

    def _on_state(self, st):
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            self.btn_play.setText(
                "⏸" if st == QMediaPlayer.PlaybackState.PlayingState else "▶")
        except Exception:
            pass

    def _on_status(self, status):
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QMediaMetaData
            # 枚举正规名是 LoadedMedia（写成 Loaded 会抛 AttributeError 被下面吞掉，
            # 导致拿到分辨率后永不重新适配尺寸）；LoadedMedia 时元数据才就绪。
            if status != QMediaPlayer.MediaStatus.LoadedMedia:
                return
            md = self.player.metaData()
            res = md.value(QMediaMetaData.Key.VideoResolution)
            if res and getattr(res, "width", None) and res.width() > 0 and res.height() > 0:
                if callable(self._on_res):
                    self._on_res(res.width(), res.height())
        except Exception:
            pass


class QuickLookOverlay(QWidget):
    """只有预览、没有搜索列表的浮层。键盘控制交给上层 GlobalQuickLook（钩子在
    全局态会先把键截走，本窗口的 keyPressEvent 只在"面板内 peek、钩子没接管"时兜底）。

    两种内容页：普通文件走 PreviewPane（图/文档/文本…），视频走 _VideoPage 直接播放。
    浮层尺寸按内容宽高比自适应（图片读 QImageReader、视频等 metadata），贴合物体而不是
    撑一个固定大窗——否则小图/首帧在超高窗口里垂直居中，头顶会空出一大条。"""

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet(
            "QuickLookOverlay { background:#17181C; border:1px solid #2A2C33; }")
        self.resize(900, 640)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)

        # 顶部细条：左文件名，右「空格/Esc 关闭」+ ✕。只这一条 chrome，不再叠第二层标题。
        bar = QHBoxLayout()
        bar.setContentsMargins(12, 6, 10, 4)
        bar.setSpacing(10)
        self.lbl_name = QLabel("")
        self.lbl_name.setStyleSheet(
            "color:#E8EAED; font-size:13px; font-weight:600; background:transparent;")
        bar.addWidget(self.lbl_name, 1)
        hint = QLabel("空格/Esc 关闭")
        hint.setStyleSheet("color:#7A8089; font-size:12px; background:transparent;")
        bar.addWidget(hint)
        btn = QPushButton("✕")
        btn.setFixedSize(24, 24)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton { color:#C9CED6; background:transparent;"
            " border:1px solid rgba(255,255,255,0.16); border-radius:12px;"
            " font-size:13px; }"
            " QPushButton:hover { background:rgba(255,255,255,0.10); }")
        btn.clicked.connect(lambda: self._on_close_cb() if self._on_close_cb else None)
        bar.addWidget(btn)
        barwrap = QWidget(self)
        barwrap.setLayout(bar)
        barwrap.setStyleSheet("background:#17181C;")
        lay.addWidget(barwrap)

        # 内容堆栈：pane 常驻；video 页懒建（首次预览视频才造，缺后端就退回 pane）
        self.pane = pv.PreviewPane(self)
        # 顶部 bar 已经显了文件名，关掉 pane 自带标题，免得上下两处重名
        self.pane.set_header_title(False)
        self._pages = QStackedWidget(self)
        self._pages.setStyleSheet("background:#17181C;")
        self._pages.addWidget(self.pane)
        self._video = None
        lay.addWidget(self._pages, 1)

        self._on_close_cb = None        # 关闭动作由上层 GlobalQuickLook 注入
        self._content_ar = None         # 当前内容宽高比（w/h），None=按默认大窗

    def set_on_close(self, cb):
        self._on_close_cb = cb

    # ---------- 展示 ----------
    def peek(self, path):
        path = str(path)
        self.lbl_name.setText(os.path.basename(path))
        kind = pv.category(path)
        if kind == "video" and self._ensure_video():
            self.pane.show_item({})            # 停掉 pane 的后台取帧线程
            self._pages.setCurrentWidget(self._video)
            self._content_ar = None            # 等 metadata 回来再定比例
            self._video.play(path)
        else:
            self._stop_video()
            self._pages.setCurrentWidget(self.pane)
            self.pane.show_item(_item_for(path))
            self._content_ar = self._image_ar(path) if kind == "image" else None
        self._apply_size()
        self.show()
        self.raise_()
        raise_to_front(self)

    def _ensure_video(self):
        """懒建视频页；QtMultimedia 不可用时返回 False（调用方退回首帧图）。"""
        if self._video is not None:
            return True
        try:
            self._video = _VideoPage(self)
            self._video._on_res = self._refit_video
            self._pages.addWidget(self._video)
            return True
        except Exception:
            self._video = None
            return False

    def _stop_video(self):
        if self._video is not None:
            self._video.stop()

    def _refit_video(self, w, h):
        if w and h:
            self._content_ar = float(w) / float(h)
            if self.isVisible():
                self._apply_size()

    @staticmethod
    def _image_ar(path):
        """图片宽高比：用 QImageReader 只读尺寸不解码，主线程也几乎零成本。"""
        try:
            from PySide6.QtGui import QImageReader
            sz = QImageReader(str(path)).size()
            if sz.isValid() and sz.height() > 0:
                return float(sz.width()) / float(sz.height())
        except Exception:
            pass
        return None

    def _apply_size(self):
        from PySide6.QtWidgets import QApplication
        scr = QApplication.primaryScreen()
        geo = scr.availableGeometry() if scr is not None else None
        if geo is None:
            return
        if self._content_ar and self._content_ar > 0:
            ar = self._content_ar
            avail_w = geo.width() * _OVERLAY_MAX_RATIO
            avail_h = max(geo.height() * _OVERLAY_MAX_RATIO - _OVERLAY_CHROME, 120)
            w = min(avail_w, avail_h * ar)
            h = w / ar
            if h > avail_h:
                h = avail_h
                w = h * ar
            w = max(w, _OVERLAY_MIN_W)
            w = min(w, geo.width())
            h = min(h + _OVERLAY_CHROME, geo.height())
        else:
            w = max(int(geo.width() * _OVERLAY_W_RATIO), 520)
            h = max(int(geo.height() * _OVERLAY_H_RATIO), 360)
            w = min(w, geo.width())
            h = min(h, geo.height())
        w, h = int(w), int(h)
        self.resize(w, h)
        self.move(geo.x() + (geo.width() - w) // 2,
                  geo.y() + (geo.height() - h) // 2)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key.Key_Space, Qt.Key.Key_Escape):
            if self._on_close_cb is not None:
                self._on_close_cb()
            return
        super().keyPressEvent(e)


# ---------------------------------------------------------------------------
# 全局控制器：低级键盘钩子 + 浮层生命周期
# ---------------------------------------------------------------------------
class GlobalQuickLook(QObject):
    """把"选中文件按空格弹预览"接到系统输入栈上的单例控制器。

    钩子只在装好、且开关打开、且焦点确实在文件列表时才动手；否则一律放行。
    浮层一旦开着，空格/Esc 关掉、↑↓ 切换相邻文件、其它任何按键或前台切走都自动收起——
    宁可多关一次，也不能让一个置顶浮层赖在别人脸上。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._user32 = _load_user32()
        self._hook = None
        self._hook_cb = None
        self._overlay = None
        self._anchor = 0                    # peek 时来源窗口（资源管理器/桌面）hwnd
        self._siblings = []
        self._idx = -1
        self._watch = None                  # 看门狗定时器懒建：import 期可能还没 QApplication

    def _watchdog(self):
        """看门狗定时器懒建（import 期可能还没有 QApplication，别在那时候造 QTimer）。"""
        if self._watch is None:
            self._watch = QTimer(self)
            self._watch.setInterval(_WATCH_MS)
            self._watch.timeout.connect(self._check_foreground)
        return self._watch

    # ---------- 生命周期 ----------
    def available(self):
        return _win() and self._user32 is not None and _HOOK_PROC is not None

    def start(self):
        if not self.available() or self._hook:
            return bool(self._hook)
        try:
            self._hook_cb = _HOOK_PROC(self._hook_proc)
            self._hook = self._user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_cb,
                                                        None, 0)
        except Exception as e:
            self._hook = self._hook_cb = None
            self._log("QuickLook 键盘钩子没装上（异常：%s）" % e, warn=True)
            return False
        if not self._hook:
            self._hook_cb = None
            self._log("QuickLook 键盘钩子没装上（SetWindowsHookExW 返回 0）", warn=True)
            return False
        self._log("QuickLook 已接管全局空格（只在资源管理器/桌面的文件列表里生效）")
        return True

    def stop(self):
        if self._hook:
            try:
                self._user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
        self._hook = None
        self._hook_cb = None               # 回调本体必须一起松掉，否则野指针崩进程
        if self._watch is not None:
            self._watch.stop()
        self._close_overlay()

    # ---------- 状态 ----------
    def is_open(self):
        return self._overlay is not None and self._overlay.isVisible()

    def peek(self, path, siblings=None, watch=True):
        """弹出浮层预览 path。面板内 peek（钩子没接管时）与钩子触发都走这里。

        watch=False：不起“前台切走就收”的看门狗。面板里触发时，弹层会抢焦点、
        面板又因失焦收起，前台归属很乱——这时候让看门狗一跑就会把刚弹的预览误关，
        所以面板内预览只靠空格/Esc/✕（都由钩子或弹层自己接）来关。"""
        if not path or not os.path.exists(path):
            return False
        self._ensure_overlay()
        self._siblings = list(siblings) if siblings else siblings_for(path)
        try:
            self._idx = self._siblings.index(str(path))
        except ValueError:
            self._siblings = [str(path)]
            self._idx = 0
        self._overlay.peek(str(path))
        self._anchor = int(self._user32.GetForegroundWindow() or 0) if _win() else 0
        if watch:
            self._watchdog().start()
        return True

    def close(self):
        self._close_overlay()

    def _ensure_overlay(self):
        if self._overlay is None:
            self._overlay = QuickLookOverlay()
            self._overlay.set_on_close(self._close_overlay)
        return self._overlay

    def _close_overlay(self):
        if self._watch is not None:
            self._watch.stop()
        if self._overlay is not None:
            self._overlay._stop_video()      # 关层就停播放，释放文件句柄（否则视频删不掉）
            self._overlay.hide()
        self._anchor = 0
        self._siblings = []
        self._idx = -1

    def _cycle(self, delta):
        if not self.is_open() or len(self._siblings) < 2:
            return
        self._idx = (self._idx + delta) % len(self._siblings)
        path = self._siblings[self._idx]
        self._overlay.peek(path)

    def _check_foreground(self):
        """浮层开着时，前台一旦切到别处（用户去别的软件干活了）就自动收起。

        只认「还是浮层自己」或「还是那个来源窗口」两种情况；其余一律关。这样置顶
        浮层不会赖在用户新打开的窗口上。"""
        if not self.is_open() or not _win():
            return
        fg = int(self._user32.GetForegroundWindow() or 0)
        if fg == 0:
            return
        try:
            ov_hwnd = int(self._overlay.winId() or 0)
        except Exception:
            ov_hwnd = 0
        if fg == ov_hwnd or fg == self._anchor:
            return
        self._close_overlay()

    # ---------- 钩子回调（快进快出，真活甩给事件循环） ----------
    def _hook_proc(self, ncode, wparam, lparam):
        swallow = False
        try:
            if ncode >= 0 and lparam and wparam in _DOWN_MSGS:
                vk = int(_KBDLLHOOKSTRUCT.from_address(int(lparam)).vkCode)
                swallow = self._decide(vk)
        except Exception:
            swallow = False
        if swallow:
            return 1                        # 非 0＝把这一下吞掉，不再往下传
        try:
            return self._user32.CallNextHookEx(self._hook, ncode, wparam, lparam)
        except Exception:
            return 0

    def _decide(self, vk):
        """返回 True＝这次按键归 QuickLook（吞掉 + 已排动作）；False＝放行。"""
        # 浮层开着：空格/Esc 关，↑↓ 切换；其它键放行让用户正常操作（前台切走会被看门狗关）
        if self.is_open():
            if vk in (VK_SPACE, VK_ESCAPE):
                self._fire(self._close_overlay)
                return True
            if vk == VK_UP:
                self._fire(lambda: self._cycle(-1))
                return True
            if vk == VK_DOWN:
                self._fire(lambda: self._cycle(1))
                return True
            return False
        # 浮层没开：只有裸空格（无任何修饰键）+ 前台是文件列表才考虑
        if vk != VK_SPACE:
            return False
        if not _win():
            return False
        u = self._user32
        if _mods_down(u):                 # 带 Ctrl/Alt/Shift/Win 的空格归系统热键，不抢
            return False
        fg = int(u.GetForegroundWindow() or 0)
        fg_cls = _class_of(u, fg) if fg else ""
        # 桌面两种形态：前台直接是 Progman/WorkerW，或被壁纸/桌面整理盖住（前台
        # 是那个 ATL 窗口）——后者仍按桌面走（读原生 SysListView32 选中）。
        desktop_mode = bool(fg) and (fg_cls in _DESKTOP_CLASSES or _is_desktop_cover(u, fg))
        focus_ok = bool(fg) and (desktop_mode or _is_file_list_focus(u, fg))
        # 钩子里绝不做文件 IO，诊断一律 singleShot 甩回事件循环再落日志
        if not focus_ok:
            self._fire(lambda: self._log(
                "空格未截：前台 hwnd=%d 类=%r 进程=%r 焦点判定=False"
                % (fg, fg_cls, foreground_process_name(fg))))
            return False
        self._fire(lambda: self._log(
            "空格命中：前台 hwnd=%d 类=%r%s" % (fg, fg_cls, "（桌面覆盖层）" if desktop_mode and fg_cls not in _DESKTOP_CLASSES else "")))
        self._fire_from_explorer(fg, desktop_mode)
        return True

    def _fire(self, fn):
        try:
            QTimer.singleShot(0, fn)
        except Exception:
            fn()

    def _fire_from_explorer(self, fg, is_desktop=None):
        """延迟到事件循环再读选区并弹层；这里已经决定吞掉空格，若最终没
        读到可预览文件，就当作无事发生（资源管理器里没选中时空格本就没什么动作）。

        桌面与资源管理器走两条不同的选区通道：资源管理器用 Shell.Application（能拿
        到精确路径），桌面不在 Shell.Windows() 里，只能跨进程读 SysListView32 选中项。

        is_desktop 由 _decide 传入（含“被壁纸/桌面整理盖住的原生桌面”）；不传则粗判。"""
        u = self._user32
        fg_cls = _class_of(u, fg) if (u and fg) else ""
        if is_desktop is None:
            is_desktop = (fg_cls in _DESKTOP_CLASSES) or (not fg)

        def act():
            if is_desktop:
                lv = _focus_hwnd(u, fg) if fg else 0
                if not lv or _class_of(u, lv) != _DESKTOP_LIST_CLASS:
                    lv = _find_desktop_listview(u)      # 焦点拿不到就实走遍历
                paths = desktop_selected_paths(lv)
                self._log("桌面通道 lv=%d 读到选中=%d 项：%r"
                          % (lv, len(paths), [os.path.basename(p) for p in paths][:4]))
            else:
                paths = selected_paths(fg)
                self._log("资源管理器通道 fg=%d 读到选中=%d 项：%r"
                          % (fg, len(paths), [os.path.basename(p) for p in paths][:4]))
            target = None
            for p in paths:
                if is_previewable(p):
                    target = p
                    break
            if target is None:
                if not paths:
                    self._log("桌面选区读空（选中态与光标命中都没拿到项）", warn=True)
                else:
                    detail = []
                    for p in paths:
                        try:
                            tag = "目录" if os.path.isdir(p) else pv.category(p)
                            detail.append("%s→%s" % (os.path.basename(p), tag))
                        except Exception:
                            detail.append(str(p))
                    self._log("选区里没有可预览项：%r（非图/视频/文档/PDF）" % (detail,), warn=True)
                return
            self._log("弹预览：%s（cat=%s）" % (os.path.basename(target),
                                               pv.category(target)))
            self.peek(target, siblings_for(target))
        self._fire(act)

    def _log(self, msg, warn=False):
        try:
            from core.logger import log
            (log.warning if warn else log.info)("QuickLook %s" % msg)
        except Exception:
            pass


#: 进程内单例：主窗口启动/退出时 start()/stop()，设置页开关调它，面板内 peek 也调它
manager = GlobalQuickLook()
