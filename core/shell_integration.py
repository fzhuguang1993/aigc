"""
core/shell_integration.py —— Windows 资源管理器右键「入库到素材库 / 成品库」

需求：在桌面 / 文件夹里对任意视频文件右键，一键把它「上传入库」到素材库或成品库。

设计取舍：
- 只写 HKCU（当前用户级），不碰 HKLM：免管理员权限，卸载/清理只影响当前用户；
  动词挂在 Software\\Classes\\SystemFileAssociations\\<扩展名>\\shell 下，绕开
  ProgID 重定向——无论 .mp4 当前被关联给哪个播放器，都能稳定挂上右键项。
- 右键项只是「发起者」：启动本程序并带 --aigc-import <目标> "<文件>"。程序把这条
  请求落进一个共享队列文件；若已有实例在跑，就把它叫到前台，由在跑的实例排空队列
  （见 desktop.py 的参数解析 + gui/main_window.py 的 drain），避免多实例抢锁。
- 先只收视频扩展名（与 store.output_store.VID_EXT 同口径）；图片/音频留待后续。
- 卸载即从这些 SystemFileAssociations 子键删掉我们写的两个 shell 键，不残留。

非 Windows（开发 Mac/Linux）：所有写操作静默 no-op，读接口返回"未注册"。
"""
import json
import os
import sys
import time
from pathlib import Path

#: 目标库 → (注册表 shell 子键名, 右键显示文案)；子键名保持 ASCII，显示文案走中文
MENU_ITEMS = (
    ("AigcImportMaterial", "🎬 入库到素材库", "material"),
    ("AigcImportOutput", "📦 入库到成品库", "output"),
)

#: 现阶段只挂视频扩展名（与成品库扫描口径一致）
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")

#: SystemFileAssociations 根：给"某个扩展名"加右键动词的标准位置（不受关联 ProgID 影响）
_BASE = r"Software\Classes\SystemFileAssociations"


def supported():
    """右键入库菜单只在 Windows 上有意义。"""
    return sys.platform.startswith("win")


def _launch_cmd():
    """命令行前缀：打包态用 exe 本体；开发态用当前解释器 + desktop.py。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    root = Path(__file__).resolve().parents[1]
    return f'"{sys.executable}" "{root / "desktop.py"}"'


def _icon_arg():
    """右键项图标：打包态取 exe 内嵌图标（,0）；开发态取 assets/app.ico。找不到就留空。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}",0'
    ico = Path(__file__).resolve().parents[1] / "assets" / "app.ico"
    return f'"{ico}",0' if ico.exists() else ""


def _command(target):
    """写进 command 子键的完整命令：发起导入 + 目标库 + 选中文件（%1 由资源管理器替换）。"""
    return f'{_launch_cmd()} --aigc-import {target} "%1"'


# ---------------- 注册 / 卸载 ----------------
def register():
    """给每个视频扩展名挂两条右键项（素材库 / 成品库）。

    返回 (成功条数, 错误信息列表)；调用方据此如实提示，不假装全成。"""
    if not supported():
        return 0, ["仅 Windows 支持右键入库菜单"]
    import winreg
    icon = _icon_arg()
    done, errors = 0, []
    for ext in VIDEO_EXTS:
        for keyname, label, target in MENU_ITEMS:
            shell_path = rf"{_BASE}\{ext}\shell\{keyname}"
            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, shell_path) as k:
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, label)      # 默认值 = 菜单文字
                    winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, label)
                    if icon:
                        winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, shell_path + r"\command") as ck:
                    winreg.SetValueEx(ck, "", 0, winreg.REG_SZ, _command(target))
                done += 1
            except OSError as e:
                errors.append(f"{ext} · {label}：{e}")
    return done, errors


def unregister():
    """删掉我们写的右键项（RegDeleteKey 连子键 command 一起删）。返回 (删除条数, 错误列表)。"""
    if not supported():
        return 0, ["仅 Windows 支持右键入库菜单"]
    import winreg
    removed, errors = 0, []
    for ext in VIDEO_EXTS:
        for keyname, _label, _target in MENU_ITEMS:
            shell_path = rf"{_BASE}\{ext}\shell\{keyname}"
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, shell_path)
                removed += 1
            except FileNotFoundError:
                pass        # 本来就没有：关掉＝已是目标态，不报错
            except OSError as e:
                errors.append(f"{ext} · {keyname}：{e}")
    return removed, errors


def is_registered():
    """以第一个扩展名的第一条为准，判断菜单是否已装（供开关回显）。"""
    if not supported():
        return False
    import winreg
    keyname = MENU_ITEMS[0][0]
    path = rf"{_BASE}\{VIDEO_EXTS[0]}\shell\{keyname}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            winreg.CloseKey(k)
        return True
    except OSError:
        return False


# ---------------- 导入请求队列（进程间交接） ----------------
def _queue_file():
    from core.paths import RUNTIME_DIR
    return Path(RUNTIME_DIR) / "shell_imports.jsonl"


def enqueue(target, path):
    """把一条「入库请求」追加进队列文件；已在跑的实例会排空它。返回是否写成功。"""
    target = str(target or "").strip()
    path = str(path or "").strip()
    if target not in ("material", "output") or not path:
        return False
    try:
        f = _queue_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"target": target, "path": path,
                                 "ts": time.time()}, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def drain():
    """原子占坑后读出并清空队列，返回 [(target, path)]。

    先 os.replace 把队列改名占坑：此后右键触发的新写入落到一个全新文件，不会被这次
    drain 丢掉；改名失败（正被别的进程持有）就返回空，下一轮再来。"""
    f = _queue_file()
    if not f.exists():
        return []
    tmp = f.with_name(f"{f.name}.{os.getpid()}.proc")
    try:
        os.replace(f, tmp)
    except OSError:
        return []
    out = []
    try:
        for line in tmp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t, p = d.get("target"), d.get("path")
            if t in ("material", "output") and p:
                out.append((t, str(p)))
    except OSError:
        pass
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return out
