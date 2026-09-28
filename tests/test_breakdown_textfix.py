"""
tests/test_breakdown_textfix.py —— 识别后大模型语义纠错（mock requests.post，无网络）

校验：未配置抛 FixNotConfigured；编号行按序回填且行数不变；编号/行数错乱→回退原文不吞段；
空编号行不覆盖；分块循环各自回填；token/调用次数统计；单块整体失败该块保留原文；
词库进入 system prompt。纠错只换文本、不动结构（时间戳由调用方保留）。
"""
import requests

import video_text_tools.asr.fixer as tf
from video_text_tools.asr.types import FixNotConfigured, TranscriptSegment


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok(content, tokens=100):
    return Resp(payload={
        "choices": [{"message": {"content": content}}],
        "usage": {"total_tokens": tokens}})


_CFG = {"api_key": "sk", "model": "deepseek-chat", "base_url": "https://x/v1"}


def test_no_config_raises():
    try:
        tf.AsrFixer({"api_key": "", "model": ""})
        assert False, "应抛 FixNotConfigured"
    except FixNotConfigured:
        pass


def test_numbered_lines_backfill_in_order(monkeypatch):
    """正常返回：编号 1..n 一一对应回填，行数与顺序都不变。"""
    posts = []

    def fake(url, **k):
        posts.append(k["json"]["messages"])
        # 模型把"盖片"改成"钙片"，保持编号
        return _ok("1\t每天吃钙片\n2\t骨密度高")

    monkeypatch.setattr(requests, "post", fake)
    fx = tf.AsrFixer(_CFG)
    segs = [TranscriptSegment(0.0, 2.0, "每天吃盖片"),
            TranscriptSegment(2.0, 4.0, "股民度高")]
    out = fx.correct_segments(segs)
    assert out == ["每天吃钙片", "骨密度高"]
    assert fx.cost()["fix_calls"] == 1 and fx.cost()["tokens"] == 100


def test_glossary_goes_into_system_prompt(monkeypatch):
    captured = {}

    def fake(url, **k):
        captured["sys"] = k["json"]["messages"][0]["content"]
        return _ok("1\t钙片")

    monkeypatch.setattr(requests, "post", fake)
    fx = tf.AsrFixer(_CFG)
    fx.correct_segments(["盖片"], glossary=["钙片", "骨密度"])
    assert "钙片" in captured["sys"] and "骨密度" in captured["sys"]


def test_mismatched_numbers_fall_back_to_original(monkeypatch):
    """模型编号越界/丢失：解析不到的行回退原文，绝不吞段或错位（长度恒定）。"""
    def fake(url, **k):
        # 只回编号 1，且多给一个越界编号 9（应被丢弃）
        return _ok("1\t每天吃钙片\n9\t乱七八糟")

    monkeypatch.setattr(requests, "post", fake)
    fx = tf.AsrFixer(_CFG)
    segs = [TranscriptSegment(0.0, 2.0, "每天吃盖片"),
            TranscriptSegment(2.0, 4.0, "股民度高")]
    out = fx.correct_segments(segs)
    assert len(out) == 2                         # 行数不变
    assert out[0] == "每天吃钙片"                 # 编号 1 命中
    assert out[1] == "股民度高"                   # 编号 2 没回→保留原文


def test_empty_parsed_line_keeps_original(monkeypatch):
    """某行解析成空串：视为未纠正，保留原文，不写成空。"""
    def fake(url, **k):
        return _ok("1\t\n2")                      # 第1行纠正为空，第2行编号无内容

    monkeypatch.setattr(requests, "post", fake)
    fx = tf.AsrFixer(_CFG)
    out = fx.correct_segments(["原文一", "原文二"])
    assert out[0] == "原文一"                      # 空纠正→不覆盖
    assert out[1] == "原文二"                      # "2" 无正文→解析空→不覆盖


def test_block_failure_keeps_original_no_raise(monkeypatch):
    """某块 HTTP 4xx 直接抛 VisionError → correct_segments 兜住，该块保留原文，不整体上抛。"""
    monkeypatch.setattr(tf.time, "sleep", lambda *_: None)

    def fake(url, **k):
        return Resp(status=400, text="bad request")

    monkeypatch.setattr(requests, "post", fake)
    fx = tf.AsrFixer(_CFG)
    out = fx.correct_segments(["盖片", "股民度"])
    assert out == ["盖片", "股民度"]               # 失败块整块保留原文


def test_chunking_loops_multiple_requests(monkeypatch):
    """超过 chunk 行数会分多块，每块各自按 1..chunk 编号回填。"""
    calls = {"n": 0}

    def fake(url, **k):
        body = k["json"]["messages"][1]["content"]
        first = body.split("\t")[0]
        calls["n"] += 1
        # 每块都只纠正它自己的第 1 行
        return _ok(f"{first}\t改{first}")

    monkeypatch.setattr(requests, "post", fake)
    fx = tf.AsrFixer(_CFG)
    segs = [f"行{i}" for i in range(1, 5)]
    out = fx.correct_segments(segs, chunk=2)
    assert calls["n"] == 2                          # 4 行 / chunk 2 → 两块
    assert out == ["改1", "行2", "改1", "行4"]      # 每块首行回填，未命中行保留各自原文


def test_plain_strings_and_token_accumulate(monkeypatch):
    """入参可以是纯字符串列表；多次调用 token 累加。"""
    seq = iter([_ok("1\ta", tokens=30), _ok("1\tb", tokens=40)])

    monkeypatch.setattr(requests, "post", lambda *a, **k: next(seq))
    fx = tf.AsrFixer(_CFG)
    assert fx.correct_segments(["x"]) == ["a"]
    assert fx.correct_segments(["y"]) == ["b"]
    assert fx.cost() == {"fix_calls": 2, "tokens": 70}


# ---------------- apply_fix：pipeline / engine / 工具共用的统一入口 ----------------
def test_apply_fix_configured_rewrites_in_place(monkeypatch):
    """已配置：就地改 seg.text（时间戳不动），返回 changed/cost/note。"""
    monkeypatch.setattr(requests, "post",
                        lambda url, **k: _ok("1\t每天吃钙片\n2\t骨密度高"))
    segs = [TranscriptSegment(0.0, 2.0, "每天吃盖片"),
            TranscriptSegment(2.0, 4.0, "股民度高")]
    changed, cost, note = tf.apply_fix(segs, glossary=["钙片"], cfg=_CFG)
    assert changed == 2
    assert segs[0].text == "每天吃钙片" and segs[1].text == "骨密度高"
    assert segs[0].start == 0.0 and segs[0].end == 2.0      # 时间戳原样
    assert cost["fix_calls"] == 1 and "改 2/2" in note


def test_apply_fix_unconfigured_keeps_original_no_raise():
    """未配置（空 key/model）：不抛、原文保留，只给 note（供调用方记日志不弹窗）。"""
    segs = [TranscriptSegment(0.0, 2.0, "每天吃盖片")]
    changed, cost, note = tf.apply_fix(segs, cfg={"api_key": "", "model": ""})
    assert changed == 0 and cost == {}
    assert segs[0].text == "每天吃盖片"                      # 原文不动
    assert note                                              # 人话说明未配置
