"""
core/naming.py —— 输出视频的文件命名规则（可在「设置 → 🏷 命名规则」里自己排列组合）

为什么要单独一个模块：文件名是交付给同事看的成品，规则同时被三处用到
（下载保存时拼名、同日续排序号、撞名时的唯一化）。以前写死在
`utils/excel_utils.build_filename` 里，改一处就得跟着改另一处，
所以把「有哪些字段可拼」和「怎么拼」收在这一个地方。

默认规则与历史完全一致：`001_诺特兰德益生菌_0923_01_姓名.mp4`
存在 config.json 的 `filename` 段：{"tokens": [...], "sep": "_"}
保存后立即生效（不用重启）：本模块自带读取，不走 core.config 的启动期缓存。
"""
import json
import re
from datetime import datetime
from pathlib import Path

from core.config import CONFIG_JSON, DEFAULT_PRODUCT

EXT = ".mp4"                     # 云端产物目前只有 mp4
SEQ = "seq"                      # 特殊字段：续排序号（决定 next_seq 能不能用）

# 可选字段：key → (中文名, 渲染函数, 示例)
# 顺序即设置页里「可用字段」列表的展示顺序，常用的排前面
_FIELDS = (
    ("num",       "编号",     lambda c: f"{int(c.get('num') or 0):03d}", "001"),
    ("product",   "品名",     lambda c: c.get("product") or DEFAULT_PRODUCT, "诺特兰德益生菌"),
    ("date",      "日期",     lambda c: _when(c).strftime("%m%d"), "0923"),
    ("seq",       "序号",     lambda c: f"{int(c.get('seq') or 1):02d}", "01"),
    ("name",      "姓名",     lambda c: c.get("name") or "未知姓名", "雷亮"),
    ("date_full", "完整日期", lambda c: _when(c).strftime("%Y%m%d"), "20260923"),
    ("time",      "时刻",     lambda c: _when(c).strftime("%H%M"), "1032"),
    ("account",   "线路",     lambda c: c.get("account") or "", "acc1"),
    ("remark",    "备注",     lambda c: c.get("remark") or "", "已过审"),
    ("task_id",   "任务ID",   lambda c: str(int(c.get("task_id") or 0)), "128"),
    ("job",       "作业号",   lambda c: str(c.get("job_id") or "")[:8], "a1b2c3d4"),
)
FIELD_KEYS = tuple(k for k, *_ in _FIELDS)
FIELD_LABEL = {k: label for k, label, _, _ in _FIELDS}
FIELD_SAMPLE = {k: sample for k, _, _, sample in _FIELDS}
_RENDER = {k: fn for k, _, fn, _ in _FIELDS}

# 内置默认：动图里历史沿用的那一段，设置页「恢复默认」也用它
DEFAULT_TOKENS = ("num", "product", "date", "seq", "name")
DEFAULT_SEP = "_"
SEP_CHOICES = {"_": "下划线 _", "-": "短横 -", ".": "点 .", "": "不分隔（连着写）"}

_CACHE = None                    # (tokens, sep)：None=还没从 config.json 读过


def _when(ctx):
    v = ctx.get("when")
    return v if isinstance(v, datetime) else datetime.now()


def sanitize(s):
    """干掉文件名不允许的字符（跨 Windows/macOS 都能落地）"""
    return re.sub(r'[\\/:*?"<>|]', "_", str(s or "").strip())


def known_tokens(tokens):
    """过滤掉不认识/重复不出现在列表里的字段名；一个都不认就退回默认

    不这么兜底的话，config.json 被手改错一个字段名就会拼出空文件名，
    下载直接落到一个非法路径上，报错还很难看懂落在哪。"""
    out = [t for t in (tokens or []) if t in _RENDER]
    return out or list(DEFAULT_TOKENS)


def load(force=False):
    """读 config.json 的 filename 段（每次自己解析，不吃启动期缓存）"""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    tokens, sep = list(DEFAULT_TOKENS), DEFAULT_SEP
    try:
        if CONFIG_JSON.exists():
            cfg = json.loads(CONFIG_JSON.read_text(encoding="utf-8")).get("filename")
            if isinstance(cfg, dict):
                tokens = known_tokens(cfg.get("tokens"))
                sep = str(cfg.get("sep", DEFAULT_SEP))
    except Exception:
        pass                        # 配置坏了就用默认，别让下载环节炸
    _CACHE = (tokens, sep)
    return _CACHE


def rules():
    """当前生效的 (字段顺序, 分隔符)"""
    return load()


def set_rules(tokens, sep=DEFAULT_SEP):
    """只改内存（测试/预览用）；要落盘用 save_rules"""
    global _CACHE
    _CACHE = (known_tokens(tokens), str(sep))
    return _CACHE


def save_rules(tokens, sep=DEFAULT_SEP):
    """写回 config.json（合并写回，不动其它字段）并立即生效"""
    data = {}
    try:
        if CONFIG_JSON.exists():
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
    except Exception:
        data = {}
    tokens = known_tokens(tokens)
    data["filename"] = {"tokens": tokens, "sep": str(sep)}
    CONFIG_JSON.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return set_rules(tokens, sep)


def _resolve(tokens, sep):
    if tokens is None or sep is None:
        cur_tokens, cur_sep = load()
        tokens = cur_tokens if tokens is None else tokens
        sep = cur_sep if sep is None else sep
    return list(tokens), str(sep)


def parts(ctx, tokens=None, sep=None):
    """拼出各段（已清洗，丢掉空段：备注没填不会留个孤零零的分隔符）"""
    ts, _ = _resolve(tokens, sep)
    out = []
    for t in ts:
        v = sanitize(_RENDER[t](ctx))
        if v:
            out.append(v)
    return out


def render(ctx, tokens=None, sep=None):
    """按规则拼文件名（含扩展名）"""
    _, sp = _resolve(tokens, sep)
    return sp.join(parts(ctx, tokens, sep)) + EXT


def stem_before_seq(ctx, tokens=None, sep=None):
    """序号字段之前的那一段（含结尾分隔符）——续排序号时靠它认「同一批」文件

    规则里没放「序号」就返回 None：没得续排，唯一化交给 dup 后缀。"""
    ts, sp = _resolve(tokens, sep)
    if SEQ not in ts:
        return None
    before = ts[:ts.index(SEQ)]
    segs = parts(ctx, before, sp)
    return (sp.join(segs) + sp) if segs else ""


def next_seq(directory, ctx, tokens=None, sep=None):
    """同日同前缀的已有文件里最大序号 + 1（第一次是 1）"""
    prefix = stem_before_seq(ctx, tokens, sep)
    if prefix is None:
        return 1
    base = Path(directory)
    if not base.exists():
        return 1
    pat = re.compile(re.escape(prefix) + r"(\d+)")
    top = 0
    for p in base.glob(prefix + "*"):
        m = pat.match(p.name)
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def resolve_save_path(directory, ctx, tokens=None, sep=None):
    """定一个当前不存在、且符合命名规则的保存路径

    重跑/抽卡会同一秒产出多条，撞名就会互相覆盖，所以这里保证唯一：
    规则里有「序号」→ 序号往上加；没有 → 文件名尾巴挂 `(2)`。"""
    ts, sp = _resolve(tokens, sep)
    d = Path(directory)
    if SEQ in ts:
        n = next_seq(d, ctx, ts, sp)
        while n < 10000:
            cand = d / render({**ctx, SEQ: n}, ts, sp)
            if not cand.exists():
                return cand
            n += 1
    cand = d / render(ctx, ts, sp)
    if not cand.exists():
        return cand
    i = 2
    while True:
        alt = cand.with_name(f"{cand.stem}({i}){cand.suffix}")
        if not alt.exists():
            return alt
        i += 1


def subdirname(when=None):
    """成品按天放子目录（历史行为：outputs/0923/）"""
    return (when or datetime.now()).strftime("%m%d")


def describe(tokens=None, sep=None):
    """给设置页/悬浮提示用的人类可读规则：编号 _ 品名 _ 日期 _ 序号 _ 姓名"""
    ts, sp = _resolve(tokens, sep)
    labels = [FIELD_LABEL[t] for t in ts]
    return (sp or "／").join(labels) if sp else " ".join(labels)


def preview(ctx=None, tokens=None, sep=None):
    """用示例值渲染一个文件名，让使用者在保存前就看到结果"""
    c = {"num": 1, "product": "诺特兰德益生菌", "name": "雷亮", "seq": 1,
         "account": "acc1", "remark": "已过审", "task_id": 128,
         "job_id": "a1b2c3d4e5", "when": datetime(2026, 9, 23, 10, 32)}
    c.update(ctx or {})
    return render(c, tokens, sep)
