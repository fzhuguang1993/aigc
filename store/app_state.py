"""
store/app_state.py —— 界面偏好的轻量持久化（不进数据库、不进 git）

为什么单独一个文件：
- 工具栏的时长/步数/KOL 是「这一批想怎么跑」，不是任务字段，存进 tasks 表
  会变成每条任务各存一份、改一次要写 N 行；
- 也不能写 config.json：那是使用者手填的接口配置，程序回写容易连带把
  账号地址/key 一起覆盖掉。

存法：CONFIG_DIR/ui_state.json（隐藏配置家），写临时文件 + os.replace 原子改名，
避免同时开两个实例时把文件写成半截。
"""
import json
import os

from core.config import CONFIG_DIR

STATE_FILE = CONFIG_DIR / "ui_state.json"


def _load():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}                      # 文件不存在/损坏都按“没有记录”处理


def get(key, default=None):
    return _load().get(key, default)


def set_value(key, value):
    """写一个键；失败不抛（界面偏好丢了不影响业务，不能因此打断操作）"""
    data = _load()
    data[key] = value
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError:
        return False
    return True
