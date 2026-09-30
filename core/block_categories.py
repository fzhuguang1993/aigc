"""
core/block_categories.py —— 素材库「板块类别」词库（钩子 / 痛点 / 产品介绍 …）

素材库按板块类型（block_type）归档爆款拆解切出的片段，混剪取片也按它挑。类别必须
是一份有限、规范的清单：随手敲近义名会把同类片段散成好几组，筛选就名存实亡。所以
在「设置 → 素材类别」里统一维护，拆解切割、素材库筛选都从这份取。

类别存 config.json 的 breakdown.blocks_types 段（与拆解面板配置同段，随配置包分发）。
本模块自带读写、不吃启动期缓存：写回时保留 breakdown 段其它键（vision_mode/fps），
保存即生效（与「内容标签」core.tags 同一口径）。
"""
from core.config import BREAKDOWN_DEFAULTS, breakdown_config, read_section, write_section

# 内置默认：营销板块最常用的六类，「恢复默认」也用它
DEFAULT_CATEGORIES = list(BREAKDOWN_DEFAULTS.get("blocks_types") or [])


def _clean(name):
    """类别名会进文件名与筛选值：干掉路径分隔符等非法字符，去首尾空白"""
    s = str(name or "").strip()
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "_")
    return s.strip()


def normalize(types):
    """去空、去重、保序；一个不剩就退回默认（类别清单不能空，否则没法归档）"""
    out, seen = [], set()
    for t in types or []:
        s = _clean(t)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out or list(DEFAULT_CATEGORIES)


def load():
    """读生效的类别清单（每次现解析 config，不吃缓存）；返回拷贝。"""
    return list(breakdown_config().get("blocks_types") or DEFAULT_CATEGORIES)


def set_all(types, persist=True):
    """整体替换类别（去重清洗后）；persist=False 只改内存（测试/预览用）。

    写回时保留 breakdown 段其它键（vision_mode/fps），只换 blocks_types。"""
    types = normalize(types)
    if persist:
        sec = dict(read_section("breakdown", BREAKDOWN_DEFAULTS))
        sec["blocks_types"] = list(types)
        write_section("breakdown", sec)
    return list(types)


def add(name):
    """加一个类别（已存在则原样返回）；返回新清单。"""
    name = _clean(name)
    cur = load()
    if name and name not in cur:
        cur.append(name)
    return set_all(cur)


def remove(name):
    """删一个类别；只动清单，已切出的旧片段不变（素材库仍认它）。"""
    name = _clean(name)
    cur = [t for t in load() if t != name]
    return set_all(cur or list(DEFAULT_CATEGORIES))


def restore_default():
    return set_all(list(DEFAULT_CATEGORIES))
