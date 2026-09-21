r"""
paths.py —— 运行时目录 / 配置目录定位（不 import core.config）

单独成模块的原因：`core.setup_wizard` 必须在 config.json 生成**之前**就知道
该把文件写到哪儿，而它一旦 `from core.config import ...` 就会让 config 在
配置还是空的时候被加载并缓存（见 core/__init__.py 的警告）。
所以这里放一份零依赖的口径，config 与向导共用，避免两边算出两个目录。

两类目录：
- RUNTIME_DIR：软件/exe 所在目录，放产物（data 库、logs、outputs、素材）。
- CONFIG_DIR：隐藏的“配置家”（默认 %APPDATA%\\AIGC视频助手），放凭证类
  配置（config.json、ui_state.json、api_text 接口配置与白名单）。
"""
import ctypes
import os
import shutil
import sys
from pathlib import Path


def _runtime_dir():
    """锁定到代码/exe 所在目录，不能用 Path.cwd()！

    cwd 是“从哪个目录启动”而不是“软件在哪”：同事双击快捷方式、从命令行里跑、
    把 exe 换个文件夹打开，cwd 就变了，于是新建一份空 data/aigc.db，
    看板从零开始——内测现场就是被这个误判成“统计不持久化”。
    同理，向导写 config.json 若跟着 cwd 跑，写的位置和读取的位置就不是一个。"""
    if getattr(sys, "frozen", False):            # PyInstaller 打包运行
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]    # 开发：仓库根目录


def _default_config_dir():
    """平台默认配置家：Windows 用 %APPDATA%，其余用标准配置目录约定。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "AIGC视频助手"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AIGC视频助手"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "AIGC视频助手"


def _config_dir():
    """配置家：可用环境变量 AIGC_CONFIG_DIR 显式指定（测试隔离 / 多实例）。"""
    env = os.environ.get("AIGC_CONFIG_DIR")
    return Path(env) if env else _default_config_dir()


def _hide_on_windows(path):
    """给目录打 Windows 隐藏属性（best-effort，非 Windows 或失败都忽略）。"""
    if not sys.platform.startswith("win"):
        return
    try:
        FILE_ATTRIBUTE_HIDDEN = 0x2
        ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass


def _migrate_legacy(runtime, cfg):
    """把旧版本落在运行目录的配置搬进配置家；仅当目标不存在时搬，绝不覆盖新数据。

    返回实际迁移的路径条数（便于测试断言）。单项失败不影响其余项与启动。
    """
    moved = 0
    for rel in ("config.json", "ui_state.json", "material/api_text"):
        src = runtime / rel
        if not src.exists():
            continue
        dst = cfg / (Path(rel).name if rel != "material/api_text" else "api_text")
        if dst.exists():
            continue                       # 新家已有：保守起见旧文件原样留着
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved += 1
        except Exception:
            pass
    return moved


def ensure_config_home():
    """建好隐藏配置家 + 迁移旧配置。import 时执行一次，务必赶在读取 config.json 前。"""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _migrate_legacy(RUNTIME_DIR, CONFIG_DIR)
        _hide_on_windows(CONFIG_DIR)
    except Exception:
        pass


#: 数据文件（SQLite / 日志 / 输出 / 素材）统一落地目录，可用 AIGC_HOME 显式指定。
RUNTIME_DIR = Path(os.environ.get("AIGC_HOME") or _runtime_dir())

#: 凭证类配置（config.json / ui_state.json / api_text）的隐藏目录，可用 AIGC_CONFIG_DIR 指定。
CONFIG_DIR = _config_dir()

# 立即建目录并把老用户散在运行目录的配置搬进来（core.config import 本模块后即读到新家）
ensure_config_home()
