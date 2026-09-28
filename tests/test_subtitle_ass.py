"""
tests/test_subtitle_ass.py —— ASS 渲染（颜色/时间/上色/转义/断行，全离线）

钉住几个易错口径：
- #RRGGBB → ASS &HAABBGGRR&（BGR 反序、Alpha 前置）；
- 卖点命中词包 {\\c 色}…{\\c}，未命中保持默认；
- 文本里的花括号一律丢弃（避免撞覆盖标签）；
- 无高亮时按 max_chars 宽度 \\N 断行；
- 时间戳 H:MM:SS.cs 厘秒。
"""
from video_text_tools.breakdown.models import TranscriptSegment
from video_text_tools.subtitle.ass import (
    _ass_color, _ts, _clean, _wrap_lines, build_ass)
from video_text_tools.subtitle.models import SubtitleStyle


def _segs(*texts):
    return [TranscriptSegment(i * 2.0, i * 2.0 + 2.0, t)
            for i, t in enumerate(texts)]


def test_ass_color_bgr_reorder():
    assert _ass_color("#FFFFFF") == "&H00FFFFFF&"
    assert _ass_color("#FFD400") == "&H0000D4FF&"     # R=FF G=D4 B=00 → 00 00 D4 FF
    assert _ass_color("#000000", "&H00000000&") == "&H00000000&"
    # 带透明度（末两位）：Alpha 原样前置
    assert _ass_color("#FFFFFF80") == "&H80FFFFFF&"
    # 非法回退默认
    assert _ass_color("nope", "&H00FFFFFF&") == "&H00FFFFFF&"


def test_ts_centiseconds():
    assert _ts(0) == "0:00:00.00"
    assert _ts(3661.5) == "1:01:01.50"
    assert _ts(65.0) == "0:01:05.00"
    assert _ts(0.999) == "0:00:01.00"                # 厘秒满 100 向秒进位


def test_clean_drops_braces_and_folds_newlines():
    assert _clean("你{好}\n世界") == "你好 世界"


def test_wrap_lines_by_max_chars():
    assert _wrap_lines("一二三四", 4) == "一二三四"    # 未超长
    assert _wrap_lines("一二三四五六七八九十", 4) == "一二三四\\N五六七八\\N九十"


def test_build_ass_has_header_and_events():
    txt = build_ass(_segs("第一句", "第二句"), SubtitleStyle())
    assert "[V4+ Styles]" in txt and "[Events]" in txt
    assert "&H00FFFFFF&" in txt                         # 默认前景色进了样式头
    assert txt.count("Dialogue:") == 2
    assert txt.endswith("\r\n") and "\r\n" in txt


def test_build_ass_highlight_wraps_only_hits():
    txt = build_ass(_segs("限时抢购美白面膜"), SubtitleStyle(),
                    highlight_terms=["限时", "美白"])
    assert "{\\c&H0000D4FF&}限时{\\c}" in txt
    assert "{\\c&H0000D4FF&}美白{\\c}" in txt
    # 未命中的普通字仍裸奔（没被包进上色标签）
    assert "{\\c&H0000D4FF&}抢购{\\c}" not in txt


def test_build_ass_no_terms_plain_style():
    txt = build_ass(_segs("普通字幕"), SubtitleStyle(), highlight_terms=[])
    assert "{\\c" not in txt
    assert "普通字幕" in txt


def test_build_ass_discards_user_braces():
    # 文本里的花括号不能污染覆盖标签：命中词外，多余 {} 被清掉
    txt = build_ass(_segs("小心{标签}"), SubtitleStyle())
    seg_line = [ln for ln in txt.split("\r\n") if ln.startswith("Dialogue:")][0]
    assert "{" not in seg_line.replace("{\\c", "").replace("{\\c}", "")
