"""
utils/desktop_utils.py —— 跨平台桌面动作：打开文件 / 定位选中 / 进回收站 / 文件进剪贴板
Windows 用 os.startfile / explorer /select, / SHFileOperationW / CF_HDROP；
macOS 用 open / open -R / osascript；Linux 用 xdg-open / gio trash / xclip。

为什么自己写而不拉依赖：打包分发时多一个 pip 包就多一个装不上的风险，
而这几个动作系统本身都有原生入口（ctypes 调 shell32 / osascript）。
"""
import os
import subprocess
import sys
from pathlib import Path


def open_path(path):
    """用系统默认程序打开文件或文件夹；空路径忽略，缺失的目录先创建再打开
    
    返回：成功 True，失败 False（不会抛异常）
    """
    s = str(path or "").strip()
    if not s:
        return False
    p = Path(s)
    try:
        # 已存在 → 直接打开；不存在且“像目录”（无扩展名）→ 先创建再打开；
        # 不存在且“像文件”（有扩展名）→ 不创建伪目录，直接返回失败
        if not p.exists():
            if p.suffix:
                return False
            p.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except Exception:
        return False


def reveal_in_folder(path):
    """打开文件所在文件夹，并选中该文件（Linux 无选中概念，退化为打开所在目录）

    Windows 必须把 `/select,` 与路径合成一个参数、路径自己带引号：
    分两个参数传时，路径里有空格或中文就会被 explorer 拆成两个东西，
    结果是只打开文件夹不选中文件（看上去就像“定位没生效”）。"""
    p = Path(str(path)).expanduser()
    try:
        target = str(p.resolve())
    except OSError:
        target = str(p.absolute())
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", f'/select,"{os.path.normpath(target)}"'])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", target])
    else:
        subprocess.Popen(["xdg-open", str(Path(target).parent)])


def move_to_trash(paths):
    """把文件送进系统回收站（标错/清错能捞回来）

    返回 (成功列表, [(路径, 原因)])。一个失败不影响其它：宁可剩下的没删，
    也不要在删除中途静默改成真删。
    """
    files = [str(Path(p)) for p in (paths or []) if str(p or "").strip()]
    if not files:
        return [], []
    if sys.platform.startswith("win"):
        return _trash_windows(files)
    if sys.platform == "darwin":
        return _trash_osascript(files)
    return _trash_linux(files)


def _trash_windows(files):
    """SHFileOperationW + FOF_ALLOWUNDO：走 shell 所以真的进回收站，
    也能删只读/被索引的文件。逐条删才能准确报哪条失败。"""
    ok, failed = [], []
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as e:                     # 非 Windows 误调 / 缺 ctypes
        return [], [(f, f"无法调用系统回收站：{e}") for f in files]

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_NOERRORUI, FOF_ALLOWUNDO = 0x4, 0x10, 0x400, 0x40
    shell = ctypes.windll.shell32
    for f in files:
        buf = ctypes.create_unicode_buffer(f + "\x00\x00")
        op = _SHFILEOPSTRUCTW(None, FO_DELETE, buf, None,
                              FOF_ALLOWUNDO | FOF_SILENT | FOF_NOCONFIRMATION
                              | FOF_NOERRORUI, False, None, None)
        try:
            code = shell.SHFileOperationW(ctypes.byref(op))
        except Exception as e:
            code = f"{type(e).__name__}: {e}"
        if code == 0 and not op.fAnyOperationsAborted:
            ok.append(f)
        else:
            failed.append((f, f"回收站返回 {code}"))
    return ok, failed


def _trash_osascript(files):
    """macOS：叫 Finder 删（才会在废纸篓里），直接 unlink 是真删"""
    ok, failed = [], []
    for f in files:
        script = 'tell application "Finder" to delete (POSIX file {} as alias)'.format(
            '"' + str(f).replace('"', '\\"') + '"')
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            ok.append(f)
        else:
            failed.append((f, (r.stderr or r.stdout or "osascript 失败").strip()[:120]))
    return ok, failed


def _trash_linux(files):
    """Linux：gio trash 优先，没 gio 再试 trash-put，都没有就一条都不删"""
    for cmd in (["gio", "trash", "--"], ["trash-put"]):
        if subprocess.run(["which", cmd[0]], capture_output=True).returncode != 0:
            continue
        ok, failed = [], []
        for f in files:
            r = subprocess.run(cmd + [f], capture_output=True, text=True)
            if r.returncode == 0:
                ok.append(f)
            else:
                failed.append((f, (r.stderr or "失败").strip()[:120]))
        return ok, failed
    return [], [(f, "本机没有可用的回收站命令（gio/trash-put）") for f in files]


def copy_paths_to_clipboard(paths):
    """把文件本身放进系统剪贴板，粘到微信/钉钉/资源管理器就是一个文件

    为什么不用 Qt 自带的 `QMimeData.setUrls`：它在 Windows 上只会写
    text/uri-list，不写 CF_HDROP，粘到微信里就是一串文字而不是附件。
    返回 (ok, 提示文字)。
    """
    files = []
    for p in list(paths or []):
        s = str(p or "").strip()
        if s and Path(s).exists():
            files.append(os.path.normpath(str(Path(s).resolve())))
    if not files:
        return False, "没有可复制的文件（可能已被移动或删除）"
    if sys.platform.startswith("win"):
        return _copy_files_windows(files)
    if sys.platform == "darwin":
        return _copy_files_osascript(files)
    return _copy_files_xclip(files)


def _copy_files_windows(files):
    """CF_HDROP：微软官方定义的「文件列表」剪贴板格式

    句柄必须显式声明成 64 位返回类型（c_void_p）：默认按 int 截断，
    在 64 位系统上拿到的就是个废句柄，粘到对方程序里直接没反应。"""
    try:
        import ctypes
    except Exception as e:
        return False, f"剪贴板不可用：{e}"
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

    CF_HDROP, GMEM_MOVEABLE = 15, 0x0002
    HDR = 20        # DROPFILES 头：pFiles(4) + pt(8) + fNC(4) + fWide(4)
    # pFiles=固定 20（路径数据从这开始）、pt/fNC=0、fWide=1（后面是 Unicode）
    blob = (HDR).to_bytes(4, "little") + bytes(8) + bytes(4) + (1).to_bytes(4, "little")
    data = ("\x00".join(files) + "\x00\x00").encode("utf-16-le")
    body = blob + data

    if not user32.OpenClipboard(None):
        return False, "剪贴板被其它程序占用，稍等一下再试"
    try:
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(body))
        if not h:
            return False, "内存分配失败"
        user32.EmptyClipboard()
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            return False, "锁内存失败"
        ctypes.memmove(p, body, len(body))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_HDROP, h):
            return False, "写入剪贴板失败"
        # 写成功后内存归剪贴板所有，不能 GlobalFree
        return True, f"已复制 {len(files)} 个文件到剪贴板"
    finally:
        user32.CloseClipboard()


def _copy_files_osascript(files):
    """macOS：只能一条一条给（剪贴板上放多文件的写法依赖 Finder 接口）"""
    script = 'set the clipboard to (POSIX file "{}")'.format(
        str(files[0]).replace('"', '\\"'))
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return False, ((r.stderr or r.stdout or "osascript 失败").strip()
                       or "系统剪贴板不可用")[:120]
    return True, (f"已复制到剪贴板（Mac 上一次只给一个，多余的已忽略）"
                  if len(files) > 1 else "已复制 1 个文件到剪贴板")


def _copy_files_xclip(files):
    """Linux：靠 xclip 写 text/uri-list（装了 xclip 才能用，没装就明说）"""
    if subprocess.run(["which", "xclip"], capture_output=True).returncode != 0:
        return False, "本机没装 xclip，无法把文件放进剪贴板"
    uri = "\n".join(Path(f).absolute().as_uri() for f in files) + "\n"
    r = subprocess.run(["xclip", "-selection", "clipboard", "-t", "text/uri-list"],
                       input=uri.encode("utf-8"), capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stderr or "xclip 失败").strip()[:120]
    return True, f"已复制 {len(files)} 个文件到剪贴板"
