"""
core/single_instance.py —— 同一台机器只留一个 GUI 实例

为什么这次必须做：托盘常驻 + 系统级热键的组合下，开第二个实例会——
1. 抢注同一批热键：第二个实例全部注册失败，用户以为"全局键失灵了"；
2. 两个托盘图标、两份轮询线程一起跑，云端在途任务数翻倍、日志互相踩；
3. 数据家虽然是同一个 SQLite，但两边界面各存一份状态，改完互相看不见。
所以第二个实例该做的事是"把已经开着的那个叫到前台"，然后自己安静退出。

实现用命名互斥体 CreateMutexW + 判 GetLastError()==ERROR_ALREADY_EXISTS，
不用文件锁：文件锁在进程被杀/断电后可能留下死锁文件，反而把用户挡在门外；
命名互斥体由系统在线句柄表里管，进程一没就自动释放。

非 Windows（开发Mac/Linux）：acquire 一律返回 True，不拦多次启动。
"""
import ctypes
import sys

try:                                    # wintypes 只在 Windows 上存在
    from ctypes import wintypes
except (ImportError, ValueError):
    wintypes = None

#: 互斥体已存在＝另一个实例正开着
ERROR_ALREADY_EXISTS = 183

#: 主窗口标题（与 gui/main_window.setWindowTitle 一致，唤回前台时按它找窗口）
WINDOW_TITLE = "AIGC 工厂"

_SW_RESTORE = 9

_handle = None          # 互斥体句柄：必须一直握着，句柄一关锁就没了


def _mutex_name(title=WINDOW_TITLE):
    """Local\\ 命名空间＝同一台机器的当前登录会话。
    开发态加后缀，避免手跑的 python 与装出来的 exe 互相顶掉。"""
    prefix = "" if getattr(sys, "frozen", False) else "Dev"
    return f"Local\\{prefix}{title}"


def acquire(title=WINDOW_TITLE):
    """占用单实例锁；True＝本机第一个实例（正常启动），False＝已有一个开着"""
    global _handle
    if wintypes is None or not sys.platform.startswith("win"):
        return True
    if _handle is not None:
        return True                     # 本进程已占过
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.restype = wintypes.DWORD
    h = kernel32.CreateMutexW(None, False, _mutex_name(title))
    if not h:
        return True                     # 拿不到句柄（权限/句柄耗尽）：宁可多开不误关
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(h)
        return False
    _handle = h
    return True


def release():
    """放锁（正常退出路径由系统回收，这里只给测试与异常分支用）"""
    global _handle
    if _handle is None:
        return
    try:
        ctypes.windll.kernel32.CloseHandle(_handle)
    except OSError:
        pass
    _handle = None


def _restore_if_needed(user32, hwnd):
    """需要时把已有实例的主窗口还原/显示出来，返回是否动过。

    只看 IsIconic 是不够的：本软件收进托盘走的是 hide()，窗口不是"最小化"态，
    IsIconic 返回 False，于是一个 SetForegroundWindow 打在不可见的窗口上——
    用户双击图标的观感就是"程序没反应"（任务栏里也没入口）。所以可见性要一起查：
    最小化或不可见，都补一发 ShowWindow(SW_RESTORE)（它对这两种状态都对）。

    单独成函数还为了可测：真句柄要装两个进程去撞，而这里包住的是 ctypes 调用，
    给个假 user32 就能钉住分派口径。"""
    try:
        iconic = bool(user32.IsIconic(hwnd))
        visible = bool(user32.IsWindowVisible(hwnd))
        if iconic or not visible:
            user32.ShowWindow(hwnd, _SW_RESTORE)
            return True
    except Exception:                     # 假对象缺方法 / 窗口已销毁：不能抛回启动路径
        return False
    return False


def activate_existing(title=WINDOW_TITLE):
    """把已经开着的那个实例叫到前台（最小化/藏在托盘里都要先显形）；返回是否找到了窗口"""
    if wintypes is None or not sys.platform.startswith("win"):
        return False
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.ShowWindow.restype = wintypes.BOOL
    _restore_if_needed(user32, hwnd)
    # 允许把前台给别的进程：本进程就是"用户刚刚手动启动"，替他切窗口是正当的
    user32.AllowSetForegroundWindow(-1)
    user32.SetForegroundWindow(hwnd)
    return True
