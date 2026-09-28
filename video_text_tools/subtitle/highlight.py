"""
video_text_tools/subtitle/highlight.py —— 卖点/强调词的语义挑选（复用豆包文本对话）

把整条口播字幕喂给豆包，让它挑出值得在字幕里高亮的"卖点词/关键短语"（产品名、功效、
价格、促销、独特卖点），返回一个词表；ass.build_ass 拿这个词表对字幕逐词上色。

只找"卖点词"不做整句改写。没配豆包 / 调用失败 → 返回 []（高亮自动关闭，退回普通字幕），
不阻断整条字幕产出。用现成的 DoubaoVision.chat_text（纯文本），不引第二个模型服务。
"""
import json
import re

_PROMPT = ("下面是一段视频口播字幕。请挑出其中值得在字幕里高亮强调的\u201c卖点词/关键短语\u201d"
           "（如产品名、功效、价格、促销、独特卖点），不要普通虚词、不要整句。"
           "最多 20 个，去重，每个不超过 8 字。只回 JSON 数组，例：[\"美白\",\"淡纹\",\"限时\"]"
           "\n\n字幕：\n{}")

_ARR_RE = re.compile(r"\[.*\]", re.S)


def _parse_terms(content):
    """从模型返回里抠 JSON 数组并清洗：去重、限长度、截断到 30 个。"""
    m = _ARR_RE.search(content or "")
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    out = []
    for t in (arr if isinstance(arr, list) else []):
        t = str(t).strip()
        if 1 <= len(t) <= 12 and t not in out:
            out.append(t)
    return out[:30]


def pick_terms(segments, doubao_cfg, log=None):
    """返回卖点词表（可能为空 = 不高亮）。"""
    text = "\n".join((getattr(s, "text", "") or "") for s in segments).strip()
    if not text:
        return []
    from ..breakdown.vision import DoubaoVision
    from ..breakdown.models import ConfigError

    try:
        vision = DoubaoVision(doubao_cfg or {})
    except ConfigError:
        if log:
            log("  ⚠ 未配置豆包，卖点高亮自动关闭")
        return []
    except Exception as e:
        if log:
            log(f"  ⚠ 豆包初始化失败，跳过高亮：{e}")
        return []
    try:
        content = vision.chat_text(_PROMPT.format(text[:4000]),
                                   temperature=0.3, max_tokens=512)
    except Exception as e:
        if log:
            log(f"  ⚠ 卖点挑选失败，跳过高亮：{e}")
        return []
    terms = _parse_terms(content)
    if log:
        log(f"  ✓ 卖点高亮词 {len(terms)} 个" if terms else "  · 未挑出卖点词，用普通字幕")
    return terms
