r"""
video_text_tools/subtitle/ass.py —— ASS 字幕渲染（含卖点内联上色）

ASS（v4+）能表达逐词颜色，libass 烧录时直接生效，比 SRT+force_style 更适合"卖点高亮"：
命中词包 {\c&HBBGGRR&}…{\c}，其余走默认样式。烧录与二次精修都以这份 .ass 为准，
纯文本 .srt 只作兜底。

颜色约定：调用方给 #RRGGBB（可选末两位透明度），这里翻成 ASS 的 &HAABBGGRR&（BGR 反序、
Alpha 在最前且 00=不透明）。文本里的花括号一律丢弃，避免与覆盖标签 {} 冲突；换行统一 \N。
"""
import re
from pathlib import Path

_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary},{secondary},{outline},{back},{bold},0,0,0,100,100,0,0,1,{outline_w},{shadow},{align},20,20,{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""


def _ass_color(hex_str, default="&H00FFFFFF&"):
    """#RRGGBB 或 #RRGGBBAA -> ASS &HAABBGGRR&；解析失败回退 default。"""
    s = (hex_str or "").strip().lstrip("#")
    if len(s) == 6:
        r, g, b, a = s[0:2], s[2:4], s[4:6], "00"
    elif len(s) == 8:
        r, g, b, a = s[0:2], s[2:4], s[4:6], s[6:8]
    else:
        return default
    try:                     # 校验是十六进制
        int(s, 16)
    except ValueError:
        return default
    return f"&H{a.upper()}{b.upper()}{g.upper()}{r.upper()}&"


def _ts(sec):
    """秒 -> ASS 时间 H:MM:SS.cs（厘秒）。"""
    sec = max(0.0, float(sec or 0))
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(text):
    """去花括号（避免撞覆盖标签）+ 折叠换行为空格。"""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    return t.replace("{", "").replace("}", "").strip()


def _wrap_lines(text, max_chars):
    """无高亮时按 max_chars 宽度硬断行（CJK 友好，直接按字符切）。"""
    t = _clean(text)
    n = max(4, int(max_chars or 0))
    if len(t) <= n:
        return t
    return "\\N".join(t[i:i + n] for i in range(0, len(t), n))


def _highlight(text, terms, hl_color):
    r"""命中 terms 的片段包 {\c 颜色}…{\c}；terms 已按长度降序做最长匹配。"""
    if not terms:
        return _clean(text)
    pat = re.compile("|".join(re.escape(t) for t in terms))
    col = _ass_color(hl_color, "&H0000D7FF&")
    out = []
    pos = 0
    for m in pat.finditer(text):
        out.append(_clean(text[pos:m.start()]))
        out.append("{\\c" + col + "}" + _clean(m.group(0)) + "{\\c}")
        pos = m.end()
    out.append(_clean(text[pos:]))
    return "".join(out)


def build_ass(segments, style, highlight_terms=None):
    """渲染整份 ASS 文本（CRLF）。highlight_terms 非空才逐词上色。"""
    terms = [t for t in (highlight_terms or []) if t and len(t) <= 12]
    terms.sort(key=len, reverse=True)          # 最长匹配优先
    hl_on = bool(terms)
    header = _HEADER.format(
        font=getattr(style, "font", "Microsoft YaHei"),
        size=int(style.font_size),
        primary=_ass_color(style.primary),
        secondary=_ass_color(style.primary),
        outline=_ass_color(style.outline, "&H00000000&"),
        back=_ass_color(style.back, "&H96000000&"),
        bold=-1, outline_w=2, shadow=0,
        align=8 if str(style.align) == "top" else 2,
        marginv=int(style.margin_v),
    )
    lines = [header]
    for seg in segments:
        text = (getattr(seg, "text", "") or "")
        if not text.strip():
            continue
        body = _highlight(text, terms, style.highlight) if hl_on \
            else _wrap_lines(text, style.max_chars)
        lines.append(
            f"Dialogue: 0,{_ts(seg.start)},{_ts(seg.end)},Default,,0,0,0,,{body}")
    return "\r\n".join(lines) + "\r\n"


def write_ass(segments, style, highlight_terms, path):
    """渲染并落 .ass（UTF-8 with BOM，兼容部分播放器/剪映的中文识别）。"""
    Path(path).write_text(build_ass(segments, style, highlight_terms),
                          encoding="utf-8-sig", newline="")
    return str(path)
