"""
video_text_tools/breakdown/cache.py —— 爆款拆解「按链接」的阶段缓存

同一条链接拆过一次，产物（下载的视频 + 各阶段结果）按 link_key 落盘到
BREAKDOWN_LIBRARY/_cache/<key>/。重跑同一链接时 pipeline 逐阶段查缓存：命中的阶段
直接跳过下载 / 转写 / 豆包调用（省接口费与本地耗时），缺的阶段才补跑——即「断点续跑」。
任一阶段没成功就不写 ok 标记，下次自动从该阶段续。

link_key 只认规范化后的作品主链（抠出 http 链接、去 query 跟踪参数、去尾斜杠、小写域名），
所以同一条链接的重复分享文案命中同一份缓存。缓存目录、文件级读写全部幂等：
坏文件 / 缺文件一律视为「该阶段未缓存」。
"""
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .models import (STAGE_ACQUIRE, STAGE_TRANSCRIBE,
                     STAGE_VISION, STAGE_BLOCKS, STAGE_PROMPTS)

# 每阶段一个产物文件；meta.json 记「哪些阶段已完成」（ok）。抽帧阶段不缓存
# （帧图是临时产物、跑完即删，缓存路径反成累赘）。
_STAGE_FILE = {
    STAGE_ACQUIRE: "acquire.json",
    STAGE_TRANSCRIBE: "transcript.json",
    STAGE_VISION: "vision.json",
    STAGE_BLOCKS: "blocks.json",
    STAGE_PROMPTS: "prompts.json",
}
_META = "meta.json"
_VIDEO = "video.mp4"

# 分享链接常见跟踪参数：前缀命中或整键命中即从 key 计算中剔除，保证同作品同 key。
_TRACK_PREFIX = ("utm_", "share_", "from_", "tt_from", "ref_", "refer", "spm",
                 "scene", "channel", "location", "msclkid", "feature_id",
                 "is_custom", "recommend_for", "social_share", "enter_method",
                 "before_download", "comment_authority", "relation_tag")
_TRACK_EXACT = {"ch", "mid", "src", "source", "source_type", "type", "pd",
                "u_code", "timestamp", "verify_result", "user_id", "region",
                "app", "language"}


def _is_tracked(kv):
    """判断一个 query 片段（如 utm_source=x）是否属跟踪噪声。"""
    k = kv.split("=", 1)[0].strip().lower()
    if not k:
        return True
    return k in _TRACK_EXACT or any(k.startswith(p) for p in _TRACK_PREFIX)


def _cache_root(base=None):
    """缓存根：显式 base（测试用）优先，否则 BREAKDOWN_LIBRARY/_cache。"""
    if base:
        return Path(base)
    from core.config import BREAKDOWN_LIBRARY
    return Path(BREAKDOWN_LIBRARY) / "_cache"


def dir_for(key, base=None):
    return _cache_root(base) / str(key)


def _extract_primary_url(text):
    """从粘贴的分享文案里抠出第一条 http(s) 链接（文案常是「7.99 复制…http://…/」）。"""
    m = re.search(r"https?://[^\s\u4e00-\u9fff，。！？、；：]+", text or "")
    return m.group(0) if m else (text or "").strip()


def normalize_link(url):
    """规范成用于取 key 的链接：抠主链 + 去跟踪 query + 去尾斜杠 + 域名小写。"""
    u = _extract_primary_url(url)
    try:
        parts = urlsplit(u)
    except ValueError:
        return u.strip().lower()
    if not parts.netloc:                           # 不是 URL（纯文本）→ 原样小写压空白
        return re.sub(r"\s+", " ", u).strip().lower()
    kept = [kv for kv in parts.query.split("&")
            if kv and not _is_tracked(kv)]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip("/"), "&".join(kept), "")).lower()


def link_key(url):
    """规范化主链的 sha1[:16]，作为缓存目录名。"""
    return hashlib.sha1(normalize_link(url).encode("utf-8")).hexdigest()[:16]


def _read_meta(key, base=None):
    try:
        d = json.loads((dir_for(key, base) / _META).read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_meta(key, meta, base=None):
    d = dir_for(key, base)
    d.mkdir(parents=True, exist_ok=True)
    (d / _META).write_text(json.dumps(meta, ensure_ascii=False), "utf-8")


def is_stage_ok(key, stage, base=None):
    """阶段标了 ok 且产物文件在，才算命中；否则当作未缓存（坏文件自愈）。"""
    fn = _STAGE_FILE.get(stage)
    if not key or not fn:
        return False
    if _read_meta(key, base).get("stages", {}).get(stage) != "ok":
        return False
    return (dir_for(key, base) / fn).exists()


def load_stage(key, stage, base=None):
    """读某阶段产物 dict；未命中 / 坏文件 → None。"""
    fn = _STAGE_FILE.get(stage)
    if not fn or not is_stage_ok(key, stage, base):
        return None
    try:
        obj = json.loads((dir_for(key, base) / fn).read_text("utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def save_stage(key, stage, obj, base=None):
    """写某阶段产物 + 标 ok（文件级幂等）。未登记的阶段忽略。"""
    fn = _STAGE_FILE.get(stage)
    if not key or not fn:
        return
    d = dir_for(key, base)
    d.mkdir(parents=True, exist_ok=True)
    (d / fn).write_text(json.dumps(obj, ensure_ascii=False), "utf-8")
    meta = _read_meta(key, base)
    stages = meta.get("stages", {})
    stages[stage] = "ok"
    meta["stages"] = stages
    meta["link_key"] = key
    _write_meta(key, meta, base)


def drop_stage(key, stage, base=None):
    """撤销某阶段的 ok 标记（重算失败时用），产物文件留着无妨。"""
    meta = _read_meta(key, base)
    if meta.get("stages", {}).pop(stage, None) is not None:
        _write_meta(key, meta, base)


def video_path(key, base=None):
    return dir_for(key, base) / _VIDEO


def save_video_from(key, src, base=None):
    """把下载到的视频复制进缓存目录，返回缓存视频路径。"""
    dst = video_path(key, base)
    dst.parent.mkdir(parents=True, exist_ok=True)
    s, d = Path(src), dst
    if s.exists() and s.resolve() != d.resolve():
        shutil.copy2(s, d)
    return dst


def clear(key, base=None):
    shutil.rmtree(dir_for(key, base), ignore_errors=True)
