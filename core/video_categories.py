"""
core/video_categories.py —— 成品库「视频类别」词库（品牌类 / 促销类 / 情景剧 …）

成品库侧栏的「视频类别」维度取自这份清单：一条成品手动归到某一类（存 output_categories
表，按路径），筛选、分组都从这份取。类别必须是有限、规范的一组词——随手敲近义名会把
同类散成好几组，筛选就名存实亡。所以在「设置 → 视频类别」里统一维护、支持自定义增删。

与「素材板块类别」core.block_categories 同一形态，只是这份存 config.json 顶层
video_categories 段（整份 config.json 随配置包分发，故自动带上）。本模块自带读写、
不吃启动期缓存：保存即生效。
"""
import json

from core.config import CONFIG_JSON

# 内置默认：短视频成品最常用的六类，「恢复默认」也用它
DEFAULT_CATEGORIES = ["品牌类", "促销类", "情景剧", "混剪", "AIGC", "大字报"]

_SECTION = "video_categories"
_CACHE = None                    # None = 还没从 config.json 读过


def _clean(name):
    """类别名会进筛选值：干掉路径分隔符等非法字符，去首尾空白"""
    s = str(name or "").strip()
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "_")
    return s.strip()


def normalize(types):
    """去空、去重、保序；一个都不剩就退回默认（词库不能空，否则没法分类）"""
    out, seen = [], set()
    for t in types or []:
        s = _clean(t)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out or list(DEFAULT_CATEGORIES)


def load(force=False):
    """读 config.json 的 video_categories 段（每次自己解析，不吃启动期缓存）；返回拷贝。"""
    global _CACHE
    if _CACHE is None or force:
        cats = list(DEFAULT_CATEGORIES)
        try:
            if CONFIG_JSON.exists():
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get(_SECTION), list):
                    cats = normalize(data[_SECTION])
        except Exception:
            pass                # 配置坏了就用默认，别让成品库分类炸
        _CACHE = cats
    return list(_CACHE)


def set_all(types, persist=True):
    """整体替换词库（去重清洗后）；persist=False 只改内存（测试/预览用）"""
    global _CACHE
    types = normalize(types)
    _CACHE = types
    if persist:
        _save(types)
    return list(types)


def add(name):
    """加一个类别（已存在则原样返回）；返回新词库"""
    name = _clean(name)
    cur = load()
    if name and name not in cur:
        cur.append(name)
    return set_all(cur)


def remove(name):
    """删一个类别；只动词库，已打在成品上的旧类别不变（侧栏仍认它）"""
    name = _clean(name)
    cur = [t for t in load() if t != name]
    return set_all(cur or list(DEFAULT_CATEGORIES))


def restore_default():
    return set_all(list(DEFAULT_CATEGORIES))


def _save(types):
    """合并写回 config.json：只换 video_categories 段，其它字段一个字不动"""
    data = {}
    try:
        if CONFIG_JSON.exists():
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
    except Exception:
        data = {}
    data[_SECTION] = list(types)
    CONFIG_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_JSON)
