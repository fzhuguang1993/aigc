"""
core/tags.py —— 任务「内容标签」词库（开场钩子 / 活动促销 / 认证背书 …）

为什么要一份可维护的词库，而不是让人在任务表里随手敲：标签既打进任务表、
又直接决定归档后的子文件夹名（日期/产品/标签），必须是有限、规范的一组词——
随手乱敲会把同一类内容散成一堆近义文件夹，归档就白做了。

词库存 config.json 的 tags 段（整份 config.json 随配置包分发，故自动带上）。
本模块自带读写、不吃 core.config 的启动期缓存：设置页改完立即生效（同命名规则口径）。
"""
import json

from core.config import CONFIG_JSON

# 内置默认：短视频文案最常用的几种钩子/结构，设置页「恢复默认」也用它
DEFAULT_TAGS = ("开场钩子", "活动促销", "认证背书", "行动号召", "情景剧")

_CACHE = None                    # None = 还没从 config.json 读过


def _clean(name):
    """标签就是目录名：干掉路径分隔符等非法字符，去首尾空白"""
    s = str(name or "").strip()
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "_")
    return s.strip()


def normalize(tags):
    """去空、去重、保序；一个都不剩就退回默认（词库不能空，否则没法打标）"""
    out, seen = [], set()
    for t in tags or []:
        s = _clean(t)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out or list(DEFAULT_TAGS)


def load(force=False):
    """读 config.json 的 tags 段（每次自己解析，不吃启动期缓存）；返回拷贝"""
    global _CACHE
    if _CACHE is None or force:
        tags = list(DEFAULT_TAGS)
        try:
            if CONFIG_JSON.exists():
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("tags"), list):
                    tags = normalize(data["tags"])
        except Exception:
            pass                # 配置坏了就用默认，别让任务表/归档炸
        _CACHE = tags
    return list(_CACHE)


def set_all(tags, persist=True):
    """整体替换词库（去重清洗后）；persist=False 只改内存（测试/预览用）"""
    global _CACHE
    tags = normalize(tags)
    _CACHE = tags
    if persist:
        _save(tags)
    return list(tags)


def add(name):
    """加一个标签（已存在则原样返回）；返回新词库"""
    name = _clean(name)
    cur = load()
    if name and name not in cur:
        cur.append(name)
    return set_all(cur)


def remove(name):
    """删一个标签；只动词库，已打在任务上的旧标签不变（归档仍认它）"""
    name = _clean(name)
    cur = [t for t in load() if t != name]
    return set_all(cur or list(DEFAULT_TAGS))


def restore_default():
    return set_all(list(DEFAULT_TAGS))


def _save(tags):
    """合并写回 config.json：只换 tags 段，其它字段一个字不动"""
    data = {}
    try:
        if CONFIG_JSON.exists():
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
    except Exception:
        data = {}
    data["tags"] = list(tags)
    CONFIG_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_JSON)
