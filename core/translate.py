# ============================================================
# core/translate.py —— 机器翻译（火山引擎 文本翻译 MT）
#
# 只服务一件事：任务弹窗里帮人读懂、并改写得动英文提示词。
#   · 全文「英文 → 中文」：中文片段原样保留，换行/标点格式与原文对齐
#   · 全文「中文 → 英文」：在中文对照上改完，整段回译覆盖回原文（translate_back）
# 翻译结果只给人看，提交链路永远用原文（见 store/task_store.set_prompt_zh）。
#
# 额度与缓存：单机每天最多送 DAILY_LIMIT 字节原文（火山按原文量计费口径），
# 用完当场拒发、弹窗提示明日再用；翻过的片段落盘到配置家
# translate_state.json，重启后同样文字不再翻、不再耗额度。
#
# 依赖与密钥全部延迟导入：没装 volcengine / 没配 AK/SK 时本模块照常 import，
# 只有真去点翻译才抛 TranslateError，界面据此给「装什么、配什么」的可执行提示。
# ============================================================
import json
import re
import time

from core.config import TRANSLATE
from core.paths import CONFIG_DIR

# 火山「文本翻译」官方限制：一次请求 TextList 不超 16 条、总长不超 5000 字符，
# 单条不超 10000。取保守值，超长的行再按空格拆片，免得一整段长提示词被接口拦掉。
BATCH_SIZE = 16
BATCH_CHARS = 4500
MAX_CHARS = 3000

# 接口定位（实测可用：签名 service=translate、Version=2020-06-01）
HOST = "translate.volcengineapi.com"
SERVICE = "translate"
ACTION = "TranslateText"
VERSION = "2020-06-01"

ZH = "zh"
EN = "en"
DEFAULT_TARGET = ZH

# 火山 MT 的语向码
SUPPORTED = {"zh": "中文", "en": "英文", "ja": "日文", "fr": "法文",
             "de": "德文", "es": "西班牙文", "ru": "俄文", "ko": "韩文"}


class TranslateError(Exception):
    """翻译不可用/调用失败：消息直接给界面当弹窗文案用，必须是人都能看懂的一句话"""


# ---------------- 语种判定与切分 ----------------

# 中日韩汉字 + 假名 + CJK/全角标点（含中文引号、破折号、省略号）。
# 落在这些字符上的片段一律视为「不用翻」，于是英文块天然被它们隔开。
_CJK = ("\u2e80-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
        "\uff01-\uff60\u00b7\u2018\u2019\u201c\u201d\u2013\u2014\u2026")
_CUT = re.compile("([" + _CJK + "]+)")
_LATIN = re.compile(r"[A-Za-z]")


def has_cjk(text):
    return bool(re.search("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]",
                          text or ""))


def has_latin(text):
    return bool(_LATIN.search(text or ""))


def split_segments(text):
    """切成 [(可译?, 片段)]，可译片段 = 含拉丁字母的连续串。

    不可译片段（中文、标点、空白、纯数字）原样进结果，所以中英混排的提示词
    只动英文，中文那部分连标点位置都不会变。"""
    segs = []
    for part in _CUT.split(text or ""):
        if part:
            segs.append((bool(_LATIN.search(part)), part))
    return segs


def _strip_wrap(piece):
    """拆出 (前空白, 内容, 后空白)：接口多半会 trim，翻完再原样贴回去，缩进才不会塌。

    纯空白串（空行、行首缩进）不能拆——lead/trail 会重叠，回贴时能翻倍成两倍空格。"""
    if not piece.strip():
        return "", "", piece
    head = len(piece) - len(piece.lstrip())
    tail = len(piece) - len(piece.rstrip())
    return piece[:head], piece[head:len(piece) - tail], piece[len(piece) - tail:]


def _chunks(body, limit=MAX_CHARS):
    """超长行按空格拆成 <= limit 的片段；拆不开（无空格的长串）就硬切"""
    if len(body) <= limit:
        return [body]
    out, cur = [], ""
    for word in body.split(" "):
        if len(word) > limit:
            if cur:
                out.append(cur)
                cur = ""
            for i in range(0, len(word), limit):
                out.append(word[i:i + limit])
            continue
        cand = word if not cur else cur + " " + word
        if len(cand) > limit:
            out.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


# ---------------- 接口调用 ----------------

_CLIENT = None
# (源语种, 目标语种, 原文) -> 译文：一段提示词里重复短语很多（“close up”“medium shot”…），
# 不缓存就会为同一个词反复挨着限流（接口 -429 是真实存在的）。
# 缓存同时落盘到 CONFIG_DIR/translate_state.json：翻一次就一次的钱，
# 重开软件不该为同样的字再跑一趟——日额度是按机器算的，重启躲不开。
_CACHE = {}
CACHE_MAX = 2000           # 内存/盘上条目上限：超了丢最旧的（dict 保插入顺序）
DAILY_LIMIT = 100_000      # 单机每天送接口的原文字节数（utf-8 口径）
QUOTA_MSG = "今日翻译用量已达 10 万字节上限，使用受限，明日才能再次使用"

_STATE = None              # {"usage": {"date": …, "bytes": …}}；缓存本体在 _CACHE


def _state_path():
    return CONFIG_DIR / "translate_state.json"


def _ck_key(source, target, text):
    """缓存键的 JSON 化写法：元组存成 JSON 数组，免得提示词里真出现分隔符"""
    return json.dumps([source, target, text], ensure_ascii=False)


def _load_state():
    """懒加载持久缓存与当日用量（第一次用时才碰盘，不在 import 期）"""
    global _STATE
    if _STATE is not None:
        return _STATE
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        data = {}          # 没文件/坏了＝冷启动，无所谓
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    try:
        saved = int(usage.get("bytes") or 0)
    except (TypeError, ValueError):
        saved = 0
    _STATE = {"usage": {"date": str(usage.get("date") or ""), "bytes": saved}}
    for k, v in (data.get("cache") or {}).items():
        try:
            key = tuple(json.loads(k))
        except Exception:
            continue       # 坏键丢键不炸，缓存丢了只是多翻一次
        if len(key) == 3 and isinstance(v, str):
            _CACHE.setdefault(key, v)   # 盘上的旧条目不当新条目复活（不挤新鲜位）
    return _STATE


def _save_state():
    """回写缓存+用量：写失败只丢缓存，不影响这次翻译给人看"""
    try:
        items = list(_CACHE.items())[-CACHE_MAX:]
        data = {"cache": {_ck_key(*k): v for k, v in items},
                "usage": _load_state()["usage"]}
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _today_usage():
    """取当日用量 dict；日期翻篇就地归零（今天第一次翻自动重置昨天）"""
    u = _load_state()["usage"]
    if u["date"] != time.strftime("%Y-%m-%d"):
        u["date"], u["bytes"] = time.strftime("%Y-%m-%d"), 0
    return u


def used_bytes():
    """今天已送接口的原文字节数（给界面/测试看额度还剩多少用）"""
    return _today_usage()["bytes"]


def quota_left():
    """额度是否还有：纯命中缓存的翻译不受额度限制（字已经付过钱）"""
    return used_bytes() < DAILY_LIMIT


def _add_bytes(n):
    """记一笔用量：只要真送进了接口就算用掉（接口报错也可能计费，宁严勿松）"""
    _today_usage()["bytes"] += int(n)
    _save_state()


def configured():
    """密钥是否可用（占位模板值不算配置）"""
    cfg = TRANSLATE or {}
    ak = str(cfg.get("ak") or "").strip()
    sk = str(cfg.get("sk") or "").strip()
    return bool(ak and sk and not ak.startswith("<"))


def _new_client(ak, sk):
    """用指定密钥搭一个火山 MT 服务类客户端（_client 与 probe 共用）。

    官网文档那段 `from volcengine.mt.MtService import MtService` 在 1.0.228 里已经
    没得了（包内压根没有 mt 子模块），但 base.Service 是各服务共用的，自己拼
    ServiceInfo/ApiInfo 反而不依赖具体 SDK 版本。"""
    try:
        from volcengine.ApiInfo import ApiInfo
        from volcengine.Credentials import Credentials
        from volcengine.ServiceInfo import ServiceInfo
        from volcengine.base.Service import Service
    except ImportError:
        raise TranslateError("缺少翻译依赖，请先执行：pip install volcengine")
    cfg = TRANSLATE or {}
    region = str(cfg.get("region") or "cn-north-1")
    info = ServiceInfo(HOST, {}, Credentials("", "", SERVICE, region), 10, 10)
    apis = {ACTION: ApiInfo("POST", "/", {"Action": ACTION, "Version": VERSION}, {}, {})}
    cli = Service(info, apis)
    # SDK 的 Service.init() 会被环境变量 HOME 下的 ~/.volc/credentials 里的密钥抢位，
    # 构完再显式写一次，保证用的一定是传进来这一对
    cli.set_ak(str(ak).strip())
    cli.set_sk(str(sk).strip())
    return cli


def _client():
    """火山 MT 客户端：只借 SDK 的 V4 签名，服务类自己搭（启动配置那份，建好缓存）"""
    global _CLIENT
    if not configured():
        raise TranslateError(
            "还没配置翻译密钥：在本机 core/config_local.py 里填 "
            'TRANSLATE = {"ak": "...", "sk": "..."}'
            "（火山引擎控制台申请，需先开通「文本翻译」），"
            '或在 config.json 写 "translate": {"ak": "…", "sk": "…"}')
    if _CLIENT is None:
        cfg = TRANSLATE or {}
        _CLIENT = _new_client(cfg["ak"], cfg["sk"])
    return _CLIENT


def probe(ak, sk, text="hello world"):
    """拿指定密钥直连火山 MT 翻一句：接口管理页「🌐 测试翻译」用。

    不碰全局客户端、结果缓存与日额度——密钥填对没有，当场见分晓，
    不用保存重启再试；维护人自测也不该占使用者的当日额度。"""
    ak, sk = str(ak or "").strip(), str(sk or "").strip()
    if not (ak and sk):
        raise TranslateError("先把 AK / SK 都填上再测")
    cli = _new_client(ak, sk)
    project = str((TRANSLATE or {}).get("project") or "default")
    body = {"SourceLanguage": "en", "TargetLanguage": DEFAULT_TARGET,
            "ProjectName": project, "TextList": [text]}
    try:
        resp = json.loads(cli.json(ACTION, {}, json.dumps(body)))
    except Exception as e:
        raise TranslateError(_friendly(str(e)))
    return _extract(resp, 1)[0]


def _item_text(item):
    if isinstance(item, dict):
        for key in ("Translation", "translation", "dst", "text"):
            if item.get(key) is not None:
                return str(item[key])
        return ""
    return str(item if item is not None else "")


def _extract(resp, n):
    """从返回里取出 n 条译文；结构不对就带上原始返回报错，别静默给空串"""
    if not isinstance(resp, dict):
        raise TranslateError(f"翻译接口返回无法识别：{str(resp)[:200]}")
    meta = resp.get("ResponseMetadata")
    err = ((meta or {}).get("Error") or {}) if isinstance(meta, dict) else {}
    if err:
        raise TranslateError("翻译接口报错：{} {}".format(
            err.get("Code") or err.get("CodeN") or "", err.get("Message") or ""))
    code = resp.get("code", resp.get("status_code"))
    if code not in (None, 0, "0"):
        raise TranslateError(
            f"翻译接口报错（code={code}）：{resp.get('message') or resp.get('msg') or ''}")
    items = resp.get("TranslationList") or resp.get("translation_list") or []
    if isinstance(items, list) and len(items) == n:
        return [_item_text(x) for x in items]
    raise TranslateError("翻译接口返回条数对不上："
                         + json.dumps(resp, ensure_ascii=False)[:200])


def _batches(texts):
    """按「条数 + 总字数」双封阀切批（接口按总长计费限长，光按条数切会爆）"""
    cur, total = [], 0
    for t in texts:
        if cur and (len(cur) >= BATCH_SIZE or total + len(t) > BATCH_CHARS):
            yield cur
            cur, total = [], 0
        cur.append(t)
        total += len(t)
    if cur:
        yield cur


def _cache_get(key):
    return _CACHE.get(key, "")


def _cache_put(key, value):
    if value:
        _CACHE[key] = value
        if len(_CACHE) > CACHE_MAX:
            # 满了丢最旧的（dict 保插入顺序），不像旧版 clear 全扔——
            # 现在缓存还落盘，全扔等于把付过钱的译文也丢回去重翻
            for k in list(_CACHE)[:len(_CACHE) - CACHE_MAX]:
                _CACHE.pop(k, None)


def translate_texts(texts, source="auto", target=DEFAULT_TARGET):
    """批量翻译（自动分批 + 持久缓存 + 单机日额度）。
    失败抛 TranslateError，调用方直接展消息。

    当日送出字节数已达 DAILY_LIMIT 就拒发新请求（消息给界面弹窗）；
    纯命中缓存的翻译不走额度——同样的字不再重复耗接口。"""
    texts = [str(t) for t in texts]
    if not texts:
        return []
    _load_state()          # 先拾回盘上缓存再查缺失，否则重启后第一次必重翻
    cli = _client()
    project = str((TRANSLATE or {}).get("project") or "default")
    todo = []
    seen = set()
    for t in texts:
        key = (source, target, t)
        if key not in _CACHE and key not in seen:
            seen.add(key)
            todo.append(t)
    for batch in _batches(todo):
        if not quota_left():
            raise TranslateError(QUOTA_MSG)
        body = {"TargetLanguage": target, "ProjectName": project, "TextList": batch}
        if source and source != "auto":       # 文档：不传该字段 = 自动检测
            body["SourceLanguage"] = source
        batch_bytes = sum(len(t.encode("utf-8")) for t in batch)
        try:
            resp = json.loads(cli.json(ACTION, {}, json.dumps(body)))
        except Exception as e:                 # SDK 把网关/限流/参数错都包成异常
            _add_bytes(batch_bytes)            # 发出去就计费：失败也记额度
            raise TranslateError(_friendly(str(e)))
        _add_bytes(batch_bytes)
        for src, got in zip(batch, _extract(resp, len(batch))):
            _cache_put((source, target, src), got)
    if todo:
        _save_state()                          # 本轮新翻的译文落盘
    return [_cache_get((source, target, t)) for t in texts]


def _friendly(raw):
    """把接口原始错误（含 bytes repr / JSON）翻成人能看懂的一句"""
    text = raw.strip()
    if text.startswith("b'") or text.startswith('b"'):
        text = text[1:-1]
    try:
        data = json.loads(text)
        err = ((data.get("ResponseMetadata") or {}).get("Error") or {})
        if err:
            text = f"{err.get('Code')} {err.get('Message')}"
    except Exception:
        pass
    low = text.lower()
    if "-429" in text or "too frequent" in low or "throttl" in low:
        return "翻译请求太频繁（-429），歇几秒再点一次"
    if "-415" in text or "not support" in low:
        return "该语向暂不支持（-415），请确认中英文互译"
    return f"翻译请求失败：{text[:200]}"


# ---------------- 对外两个动作 ----------------

def translate_mixed(text, target=DEFAULT_TARGET):
    """整段提示词英译中：只翻含英文的片段，逐行处理，换行与标点原样保留。

    返回的字符串和原文一一对齐（行数、缩进、中文部分都不动），
    所以「原文 / 中文对照」来回切换时看不出格式差异。"""
    text = str(text or "")
    if not text.strip():
        return ""
    segs = split_segments(text)
    # 先把要送进接口的行收集起来：片段 -> [行, …]，纯数字/无字母的行不送
    jobs = []           # (片段序号, 行序号, 送去翻译的文本)
    plan = []           # 每个可译片段：[(前空白, 结果占位, 后空白, 原样行?)]
    for si, (translatable, seg) in enumerate(segs):
        if not translatable:
            plan.append(None)
            continue
        rows = []
        for li, line in enumerate(seg.split("\n")):
            lead, body, trail = _strip_wrap(line)
            if body and _LATIN.search(body):
                for ci, chunk in enumerate(_chunks(body)):
                    jobs.append((si, li, ci, chunk))
                rows.append([li, lead, body, trail])     # body 兼作“翻出空串时兜底”的原文
            else:
                rows.append([li, lead, body, trail])
        plan.append(rows)
    if not jobs:
        return text
    outs = translate_texts([j[3] for j in jobs], target=target)
    joiner = "" if target == ZH else " "      # 超长行拆成多片时，中文不留词间空格
    filled = {}         # (片段, 行) -> 拼接后的译文
    for (si, li, _ci, _src), txt in zip(jobs, outs):
        prev = filled.get((si, li))
        filled[(si, li)] = (prev + joiner + txt) if prev else (txt or "")
    pieces = []
    for si, (translatable, seg) in enumerate(segs):
        if not translatable:
            pieces.append(seg)
            continue
        rows = []
        for li, lead, mid, trail in plan[si]:
            # 接口给空（偶尔会）就把原文贴回去，宁可不翻也不能吞字
            rows.append(lead + (filled.get((si, li)) or mid) + trail)
        pieces.append("\n".join(rows))
    return "".join(pieces)


def translate_back(text, target=EN):
    """把（人工改过的）中文对照整行回译成英文，用来把改动写回原文提示词。

    与 translate_mixed 相反，这里按「行」整行送接口，不再按中英切段：
    中文句子里夹的数字、品牌名（“每天 2 粒”“益生菌”）被切开翻会碎成病句，
    整句送过去接口才读得通。没中文的行（本来就是英文）原样留着。
    行数、换行、行首缩进都与输入对齐，回写后原文的排版不会塌。"""
    src = str(text or "")
    lines = src.split("\n")
    jobs = []           # (待译行序号, 块序号, 送翻译的文本)
    rows = []           # 每个待译行：[原行下标, 前空白, 正文, 后空白]
    for i, line in enumerate(lines):
        lead, body, trail = _strip_wrap(line)
        if not body or not has_cjk(body):
            continue
        rows.append([i, lead, body, trail])
        for ci, chunk in enumerate(_chunks(body)):
            jobs.append((len(rows) - 1, ci, chunk))
    if not jobs:
        return src
    # 中文行里本来就带着英文专有名词，不指定源语种会被接口整行判成英文原样退回
    outs = translate_texts([j[2] for j in jobs], source=ZH, target=target)
    filled = {}         # 待译行序号 -> 拼好的英文
    for (ri, _ci, _src), got in zip(jobs, outs):
        prev = filled.get(ri)
        filled[ri] = (prev + " " + got) if prev else (got or "")
    out = list(lines)
    for ri, (i, lead, body, trail) in enumerate(rows):
        # 接口给空就把中文原样贴回去：宁可不翻，也不能把这行变成空白
        out[i] = lead + (filled.get(ri) or body) + trail
    return "\n".join(out)
