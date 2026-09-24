"""
tests/test_translate.py —— 提示词翻译的纯逻辑回归（不碰网络）

要守住的三件事，全靠这两层的分段/回贴：
1. 只动英文片段，中文和中文标点一个字都不能变；
2. 行数、缩进、换行位置与原文严格对齐（界面「原文 / 中文对照」切换靠这个）；
3. 接口抽风（返回空、条数对不上、语向不支持）时报的是人看得懂的话，
   而不是把提示词吞成空串——提交用的仍是原文，但对照错了会误导人。
"""
import json

import pytest

from core import translate as T


@pytest.fixture
def fake_api(monkeypatch):
    """把网络层换成“回显译文”的假实现，并记录每次收到的待发列表与语向"""
    calls = {"batches": [], "meta": [], "reply": None}

    def fake_translate_texts(texts, source="auto", target=T.DEFAULT_TARGET):
        calls["batches"].append(list(texts))
        calls["meta"].append((source, target))
        if calls["reply"] is not None:
            return list(calls["reply"])
        return [f"《{t}》" for t in texts]

    def fake_client():
        return object()

    monkeypatch.setattr(T, "translate_texts", fake_translate_texts)
    monkeypatch.setattr(T, "_client", fake_client)
    T._CACHE.clear()
    return calls


# ---------------- 分段 ----------------

def test_only_latin_runs_are_translatable():
    segs = T.split_segments("A young man walking，手里拿着一杯 coffee，镜头推进")
    assert [s for ok, s in segs if ok] == ["A young man walking", " coffee"]
    assert [s for ok, s in segs if not ok] == ["，手里拿着一杯", "，镜头推进"]


def test_pure_chinese_has_nothing_to_send(fake_api):
    """全中文提示词一次请求都不该发：省额度，也免得接口把中文绕一圈翻坏"""
    out = T.translate_mixed("一位男明星站在客厅里，对镜头口播推荐")
    assert out == "一位男明星站在客厅里，对镜头口播推荐"
    assert fake_api["batches"] == []


def test_numbers_and_punctuation_are_not_translatable():
    """纯数字/符号片段（如 1080P 里的 1080）没有字母就不送，避免翻出“一千零八十”"""
    assert [s for ok, s in T.split_segments("比例 16:9 ，镜头 3 个") if ok] == []


# ---------------- 整段回贴 ----------------

def test_mixed_keeps_line_breaks_and_indent(fake_api):
    raw = ("A young man walking on the street，手里拿着一杯 coffee\n"
           "    medium shot, warm lighting  \n纯中文一行不动")
    out = T.translate_mixed(raw)
    assert out.split("\n")[0] == "《A young man walking on the street》，手里拿着一杯 《coffee》"
    assert out.split("\n")[1] == "    《medium shot, warm lighting》  "   # 缩进与行尾空格保住
    assert out.split("\n")[2] == "纯中文一行不动"
    assert len(out.split("\n")) == len(raw.split("\n"))


def test_mixed_batches_all_english_pieces_of_a_long_prompt(fake_api):
    """多行多片段要一次请求带走（分批在 translate_texts 里），别一段一个来回"""
    T.translate_mixed("shot one 中文\nshot two 中文\nshot three 中文")
    assert fake_api["batches"] == [["shot one", "shot two", "shot three"]]


def test_empty_translation_falls_back_to_source(monkeypatch):
    """接口偶尔回空串：贴原文回去，宁可不翻也不能把提示词看成一个字不剩"""
    monkeypatch.setattr(T, "translate_texts",
                        lambda texts, source="auto", target=T.DEFAULT_TARGET:
                        ["" for _ in texts])
    raw = "A young man walking，手里拿着一杯 coffee"
    assert T.translate_mixed(raw) == raw


# ---------------- 中文改完回译英文（写回原文）----------------

def test_back_translates_whole_chinese_lines(fake_api):
    """回译按行整句送：中文里夹的数字/品牌名切开会翻成病句"""
    zh = "一个男人手里拿着一杯咖啡，每天 2 粒 Probiotics\n    特写：瓶子放到桌上  \nalready english"
    out = T.translate_back(zh)
    assert fake_api["batches"] == [["一个男人手里拿着一杯咖啡，每天 2 粒 Probiotics",
                                    "特写：瓶子放到桌上"]]
    assert fake_api["meta"] == [(T.ZH, T.EN)]           # 不指定源语种会被整行当成英文退回
    lines = out.split("\n")
    assert lines[0] == "《一个男人手里拿着一杯咖啡，每天 2 粒 Probiotics》"
    assert lines[1] == "    《特写：瓶子放到桌上》  "      # 缩进与行尾空格保住
    assert lines[2] == "already english"                # 没中文的行不送、不改


def test_back_keeps_line_count(fake_api):
    zh = "第一行中文\n\n第三行中文"
    assert len(T.translate_back(zh).split("\n")) == 3


def test_back_sends_nothing_for_pure_english(fake_api):
    """对照里一句中文都没有（已经是英文）：不发请求也不改原文"""
    raw = "A man walking on the street"
    assert T.translate_back(raw) == raw
    assert fake_api["batches"] == []


def test_back_empty_translation_keeps_chinese(monkeypatch):
    """接口回空：把中文原样贴回，绝不能把提示词那一行变成空白"""
    monkeypatch.setattr(T, "translate_texts",
                        lambda texts, source="auto", target=T.DEFAULT_TARGET:
                        ["" for _ in texts])
    zh = "一个男人拿着咖啡"
    assert T.translate_back(zh) == zh


# ---------------- 分批与拆片 ----------------

def test_batches_respect_both_count_and_length_limits():
    texts = ["x"] * (T.BATCH_SIZE + 4)
    batches = list(T._batches(texts))
    assert [len(b) for b in batches] == [T.BATCH_SIZE, 4]
    long_text = "word " * 1200                       # 单条就把字数封阀打满
    batches = list(T._batches([long_text, long_text]))
    assert len(batches) == 2 and all(len(b) == 1 for b in batches)


def test_chunks_never_exceed_limit():
    body = "x" * 5000 + " yyy " + "z" * 6000
    chunks = T._chunks(body, T.MAX_CHARS)
    assert all(len(c) <= T.MAX_CHARS for c in chunks)
    assert "".join(chunks).replace(" ", "") == body.replace(" ", "")


def test_strip_wrap_keeps_pure_whitespace_whole():
    """纯空白串不能拆：lead/trail 会重叠，回贴时空行能翻倍"""
    assert T._strip_wrap("   \n ") == ("", "", "   \n ")
    assert T._strip_wrap("  hi  ") == ("  ", "hi", "  ")


# ---------------- 返回解析 ----------------

def test_extract_reads_official_shape():
    resp = json.dumps({"TranslationList": [{"Translation": "你好"},
                                           {"Translation": "世界"}]})
    assert T._extract(json.loads(resp), 2) == ["你好", "世界"]


def test_extract_raises_on_count_mismatch():
    resp = {"TranslationList": [{"Translation": "你好"}]}
    with pytest.raises(T.TranslateError):
        T._extract(resp, 2)


def test_extract_surfaces_gateway_error():
    resp = {"ResponseMetadata": {"Error": {"Code": "-429", "Message": "too frequent"}}}
    with pytest.raises(T.TranslateError, match="-429"):
        T._extract(resp, 1)


def test_friendly_translates_rate_limit():
    raw = json.dumps({"ResponseMetadata": {"Error": {"Code": "-429",
                                                     "Message": "request too frequent"}}})
    assert "太频繁" in T._friendly("b'%s'" % raw)


def test_configured_rejects_placeholder_keys(monkeypatch):
    monkeypatch.setattr(T, "TRANSLATE", {"ak": "<你的 AccessKeyID>", "sk": "x"})
    assert T.configured() is False
    monkeypatch.setattr(T, "TRANSLATE", {"ak": "AK123", "sk": "SK123"})
    assert T.configured() is True
    monkeypatch.setattr(T, "TRANSLATE", {})
    with pytest.raises(T.TranslateError):
        T._client()
