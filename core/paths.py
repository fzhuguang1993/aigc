"""
paths.py —— 运行时目录定位（不 import core.config）

单独成模块的原因：`core.setup_wizard` 必须在 config.json 生成**之前**就知道
该把文件写到哪儿，而它一旦 `from core.config import ...` 就会让 config 在
配置还是空的时候被加载并缓存（见 core/__init__.py 的警告）。
所以这里放一份零依赖的口径，config 与向导共用，避免两边算出两个目录。
"""
import os
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


#: 数据文件（SQLite / 日志 / 输出 / ui_state.json / config.json）统一落地目录，
#: 可用环境变量 AIGC_HOME 显式指定（自动化测试 / 多实例场景）。
RUNTIME_DIR = Path(os.environ.get("AIGC_HOME") or _runtime_dir())
