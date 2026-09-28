"""
tests/test_subtitle_highlight.py —— 卖点词挑选（mock 豆包文本对话，零网络）

口径：正常返回去重、去空、限长词表；豆包未配置/调用失败 → [] （高亮自动关闭，
退回普通字幕，不阻断）；空字幕直接返回 [] 且不调用豆包。
highlight 在函数内 import DoubaoVision，patch 拆解 vision 模块属性即可。
"""
import video_text_tools.breakdown.vision as vision_mod
from video_text_tools.breakdown.models import TranscriptSegment
from video_text_tools.subtitle.highlight import pick_terms, _parse_terms


class _FakeVision:
    def __init__(self, cfg):
        self._content = cfg.get("_content", "[]")
        self.called = False

    def chat_text(self, prompt, **k):
        type(self).last_prompt = prompt
        self.called = True
        return self._content


def test_parse_terms_dedup_len_limit():
    content = '["美白","淡纹","美白","","超长长长长长长长长长长长长超短","限时"]'
    out = _parse_terms(content)
    assert "美白" in out and out.count("美白") == 1      # 去重
    assert "" not in out                                  # 去空
    assert all(1 <= len(t) <= 12 for t in out)


def test_parse_terms_no_json_returns_empty():
    assert _parse_terms("抱歉我给不了") == []


def test_pick_terms_ok(monkeypatch):
    fake = _FakeVision({"_content": '["限时","美白","限时"]'})
    monkeypatch.setattr(vision_mod, "DoubaoVision", lambda cfg: fake)
    segs = [TranscriptSegment(0, 2, "限时抢购美白面膜")]
    terms = pick_terms(segs, {"api_key": "k"})
    assert terms == ["限时", "美白"]


def test_pick_terms_config_error_returns_empty(monkeypatch, tmp_path):
    # 不 patch：真实空配置 DoubaoVision({}) 抛 ConfigError → 降级 []
    segs = [TranscriptSegment(0, 2, "有内容")]
    assert pick_terms(segs, {}) == []


def test_pick_terms_call_failure_returns_empty(monkeypatch):
    class Boom:
        def __init__(self, cfg):
            pass

        def chat_text(self, *a, **k):
            raise RuntimeError("网络炸了")
    monkeypatch.setattr(vision_mod, "DoubaoVision", lambda cfg: Boom())
    segs = [TranscriptSegment(0, 2, "有内容")]
    assert pick_terms(segs, {"api_key": "k"}) == []


def test_pick_terms_empty_text_skips_call(monkeypatch):
    fake = _FakeVision({"_content": '["x"]'})
    monkeypatch.setattr(vision_mod, "DoubaoVision", lambda cfg: fake)
    assert pick_terms([TranscriptSegment(0, 2, "   ")], {"api_key": "k"}) == []
    assert fake.called is False                           # 空字幕不浪费一次调用
