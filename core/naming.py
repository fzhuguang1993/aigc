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


# ====================================================================
# 命名规律识别（批量重命名「预读规则」按钮用；纯词法，无 Qt）
# ====================================================================
_PAT_SEPS = ("_", "-", ".")            # 候选分隔符
_PAT_DATE8 = re.compile(r"^\d{8}$")     # 20260923
_PAT_DATE4 = re.compile(r"^(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")  # MMDD
_PAT_NUM = re.compile(r"^\d{1,3}$")
# 对齐阶段尝试的字段（按此顺序认领某一列）
_PAT_ALIGN_KEYS = ("product", "tag", "block_type", "date", "time", "task_id")


def _pat_split(stem, sep):
    """按主分隔符切段；sep 为空＝整串一段（驼峰/连写）。"""
    return stem.split(sep) if sep else [stem]


def _pat_sequential(vals):
    """一列全是短数字且严格 +1 递增 → 视为序号列。"""
    try:
        nums = [int(v) for v in vals]
    except (TypeError, ValueError):
        return False
    return len(nums) >= 2 and all(_PAT_NUM.match(str(v)) for v in vals) and \
        all(b - a == 1 for a, b in zip(nums, nums[1:]))


def detect_pattern(stems, rows=None, context_fn=None):
    """从一批现有文件名主干里识别命名规律，供批量重命名「预读规则」回填。

    stems：文件名去后缀的主干列表。
    rows + context_fn：可选——传入原始行与 build_context(row, idx) 取值函数时，
      按「某字段的渲染值正好等于这一列所有行的取值」来对齐认领字段（最稳）。

    返回 dict：
      sep        主分隔符（"_"/"-"/"."/""；识别不到给 None）
      style      snake|kebab|dot|camel|pascal|none
      tokens     建议字段顺序（仅当每一列都能被认领时才给；否则 None 表示结构没吃透）
      mapped     bool，tokens 是否可信（全列命中）
    """
    clean = [str(s) for s in (stems or []) if str(s or "").strip()]
    res = {"sep": None, "style": "none", "tokens": None, "mapped": False}
    if not clean:
        return res

    # 1) 主分隔符：三种候选里选总出现次数最多者
    counts = {c: sum(s.count(c) for s in clean) for c in _PAT_SEPS}
    best = max(counts, key=counts.get)
    sep = best if counts[best] > 0 else ""
    res["sep"] = sep

    # 2) 风格：有分隔符按分隔符命名；否则看驼峰变换
    if sep:
        res["style"] = {"_": "snake", "-": "kebab", ".": "dot"}[sep]
    else:
        camel = any(re.search(r"[a-z0-9][A-Z]", s) for s in clean)
        if camel:
            pascal = all(s[:1].isupper() for s in clean if s)
            res["style"] = "pascal" if pascal else "camel"
        else:
            res["style"] = "none"

    # 3) 逐列认领字段；只有每一列都能被认领，才给出可信的 tokens
    table = [_pat_split(s, sep) for s in clean]
    ncol = max((len(t) for t in table), default=0)
    if ncol == 0:
        return res
    colvals = [[t[i] if i < len(t) else "" for t in table] for i in range(ncol)]

    can_align = bool(rows is not None and context_fn is not None
                     and len(rows) == len(clean))
    tokens, mapped = [], True
    for i in range(ncol):
        col = colvals[i]
        tok = None
        if can_align:
            for key in _PAT_ALIGN_KEYS:
                if all(str(context_fn(rows[j], j).get(key, "")) != ""
                       and str(context_fn(rows[j], j).get(key, "")) == col[j]
                       for j in range(len(clean))):
                    tok = key
                    break
        if tok is None:                            # 词法兜底：日期 / 序号
            nz = [v for v in col if v]
            if nz and all(_PAT_DATE8.match(v) for v in nz):
                tok = "date_full"
            elif nz and all(_PAT_DATE4.match(v) for v in nz):
                tok = "date"
            elif _pat_sequential(col):
                tok = "seq"
        if tok is None:
            mapped = False
            break
        if tok not in tokens:
            tokens.append(tok)

    if mapped and tokens:
        res["tokens"] = tokens
        res["mapped"] = True
    return res
