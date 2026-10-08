r"""
paths.py —— 运行时目录 / 配置目录定位（不 import core.config）

单独成模块的原因：`core.setup_wizard` 必须在 config.json 生成**之前**就知道
该把文件写到哪儿，而它一旦 `from core.config import ...` 就会让 config 在
配置还是空的时候被加载并缓存（见 core/__init__.py 的警告）。
所以这里放一份零依赖的口径，config 与向导共用，避免两边算出两个目录。

三类目录：
- APP_DIR：软件/exe 所在目录（安装位置）。装进 Program Files 后这里普通用户
  不可写，所以只用来定位程序本体（图标、开机自启路径、找回旧版散在这的数据）。
- RUNTIME_DIR：数据家，放产物（data 库、logs、outputs、素材）。四种口径：
  · 开发态（未打包）＝APP_DIR（旧行为一字不变）
  · 绿色版（exe 旁有 .aigc_portable 标记）＝APP_DIR（整个文件夹拷走即搬家）
  · 安装版＝%LOCALAPPDATA%\\AIGC视频助手：装进 Program Files 也能写产物，
    且卸载/升级不会连数据一起被干掉（免 UAC）
  · AIGC_HOME 环境变量优先级最高（自动化测试隔离 / 多实例）
- CONFIG_DIR：隐藏的“配置家”（默认 %APPDATA%\\AIGC视频助手），放凭证类
  配置（config.json、ui_state.json、api_text 接口配置与白名单）。
"""
import ctypes
import os
import shutil
import sys
from pathlib import Path

#: 应用显示名（目录名与注册表键名共用）
APP_NAME = "AIGC视频助手"

#: 绿色版标记：在安装出来的 exe 旁手建一个同名空文件，数据就跟着 exe 走
#: （U 盘携带、维护机自用场景），不再落到 %LOCALAPPDATA%。
PORTABLE_MARKER = ".aigc_portable"


def _runtime_dir():
    """程序目录（旧名旧语义，历史测试与安装器都依赖这个名字）

    ⚠ 不能用 Path.cwd()：cwd 是“从哪个目录启动”而不是“软件在哪”：同事双击
    快捷方式、从命令行里跑、把 exe 换个文件夹打开，cwd 就变了，于是新建一份
    空 data/aigc.db，看板从零开始——内测现场就是被这个误判成“统计不持久化”。
    同理，向导写 config.json 若跟着 cwd 跑，写的位置和读取的位置就不是一个。"""
    if getattr(sys, "frozen", False):            # PyInstaller 打包运行
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]    # 开发：仓库根目录


def _is_frozen():
    return bool(getattr(sys, "frozen", False))


def _local_appdata():
    """用户可写的应用数据根（Windows 用 %LOCALAPPDATA%，其余用 XDG_DATA_HOME）。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base)


def _data_home(app_dir):
    """数据家 + 模式标签（env/dev/portable/installed），口径见模块头。

    纯函数：只算路径不碰磁盘，方便单测把四种分支逐个钉住。"""
    env = os.environ.get("AIGC_HOME")
    if env:
        return Path(env), "env"
    if not _is_frozen():
        return app_dir, "dev"
    if (app_dir / PORTABLE_MARKER).exists():
        return app_dir, "portable"
    return _local_appdata() / APP_NAME, "installed"


def _default_config_dir():
    """平台默认配置家：Windows 用 %APPDATA%，其余用标准配置目录约定。"""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / APP_NAME


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
    """建好隐藏配置家 + 迁移旧配置。import 时执行一次，务必赶在读取 config.json 前。

    两个旧家都要探：安装版的数据家已搬到 %LOCALAPPDATA%，而老绿色版把
    config.json 写在 exe 旁边（APP_DIR）——只扫数据家就会漏掉这位老用户的凭证。

    ⚠ 只在安装版探：测试用 AIGC_HOME 把数据家指到临时目录时，APP_DIR 仍是仓库根，
    跟着探等于把开发机自己的 config.json / ui_state.json 搬出仓库。"""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _migrate_legacy(RUNTIME_DIR, CONFIG_DIR)
        if DATA_MODE == "installed":
            _migrate_legacy(APP_DIR, CONFIG_DIR)
        _hide_on_windows(CONFIG_DIR)
    except Exception:
        pass


#: 可能被旧版当成数据家的子目录（收养时逐个探测，非空才提示用户指过去）
_MEDIA_DIRS = ("outputs", "exports", "material")


def _adopt_legacy_data(data_home, app_home):
    """安装版首次运行：把旧版（绿色 exe）落在程序目录的任务库收进新数据家。

    只 **复制** `data/` 下的库文件（几 MB，是“历史任务”的唯一身份证），
    旧位置原件一律不动：那份可能还在另一台机器/另一个文件夹里被绿色版用着，
    搬走就是把别人的数据抢了。视频产物/素材动辄几十 GB，不自动拷：
    只把它们的路径记下来（legacy_media），让设置页一句话提示“旧目录在这，
    要不要指过来”，由用户自己决定。

    只在“新数据家还没有库、旧程序目录倒有库”时动手，其余一律原样返回；
    单项拷贝失败不影响启动（拷贝不回来下次再试，绝不能卡在开机上）。

    只对安装版生效（DATA_MODE=="installed"）：开发态与测试用 AIGC_HOME 隔离时，
    APP_DIR 就是仓库根，跟着探等于把仓库的 data/aigc.db 拷进临时家（用例秒歪）。
    """
    info = {"adopted": False, "legacy_home": "", "legacy_media": []}
    if DATA_MODE != "installed" or data_home == app_home:
        return info                                   # 开发/绿色版：本来就是一个家
    old_db = app_home / "data" / "aigc.db"
    new_db = data_home / "data" / "aigc.db"
    if not old_db.exists():
        return info
    info["legacy_home"] = str(app_home)
    if new_db.exists():
        return info                                   # 新家已有库：不拿旧数据盖用户新账
    try:
        (data_home / "data").mkdir(parents=True, exist_ok=True)
    except OSError:
        return info                                   # 新家都建不了：别拷，旧数据原样留着
    for name in (app_home / "data").iterdir():
        if not name.is_file():
            continue
        try:
            shutil.copy2(name, data_home / "data" / name.name)
            info["adopted"] = True
        except OSError:
            pass
    info["legacy_media"] = [str(app_home / d)
                            for d in _MEDIA_DIRS
                            if (app_home / d).is_dir() and any((app_home / d).iterdir())]
    return info


def ensure_data_home(data_home=None, app_home=None):
    """建好数据家子目录，并把旧版程序目录的数据收进来；返回收养信息。

    建目录失败（磁盘只读、路径非法）也不能让程序起不来：后续真写产物时
    会有各自的报错，这里只记一条日志级别的提示即可。"""
    home = Path(data_home) if data_home else RUNTIME_DIR
    app = Path(app_home) if app_home else APP_DIR
    info = {"mode": DATA_MODE, "home": str(home), "adopted": False,
            "legacy_home": "", "legacy_media": []}
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / "data").mkdir(parents=True, exist_ok=True)
        info.update(_adopt_legacy_data(home, app))
    except OSError:
        pass
    return info


#: 程序/exe 所在目录（安装位置）：图标、开机自启写注册表的路径都取这里
APP_DIR = _runtime_dir()

#: 数据落地目录（SQLite / 日志 / 输出 / 素材）：安装版＝%LOCALAPPDATA%，
#: 开发态与绿色版＝程序目录；可用 AIGC_HOME 显式指定。
RUNTIME_DIR, DATA_MODE = _data_home(APP_DIR)

#: 凭证类配置（config.json / ui_state.json / api_text）的隐藏目录，可用 AIGC_CONFIG_DIR 指定。
CONFIG_DIR = _config_dir()

# 立即建目录并把老用户散在运行目录的配置搬进来（core.config import 本模块后即读到新家）
ensure_config_home()

# 建数据家 + 收养旧版（绿色 exe）装在程序目录的历史任务库；结果供设置页读
DATA_HOME_INFO = ensure_data_home()
