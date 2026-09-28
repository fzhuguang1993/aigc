"""
tests/test_subtitle_detect.py —— 抽帧判断"是否已有烧录字幕"（mock 抽帧 + 豆包，零网络）

口径：
- 多数帧回 has_subtitle=true → verdict True；全 false → verdict False；
- 豆包未配置（ConfigError）→ verdict None（未知，由 engine 保守当作无字幕继续）；
- 抽帧/调用整体抛异常 → verdict None，不冒泡。
detect 在函数内 `from ..breakdown... import`，故 patch 拆解模块属性即可生效。
"""
import video_text_tools.breakdown.frames as frames_mod
import video_text_tools.breakdown.vision as vision_mod
from video_text_tools.breakdown.models import FrameShot
from video_text_tools.subtitle.detect import has_burned_subtitle, _parse_yesno


class _FakeVision:
    def __init__(self, cfg):
        self._answers = cfg.get("_answers", ["true"])
        self._i = 0

    def _post(self, messages, **k):
        ans = self._answers[self._i % len(self._answers)]
        self._i += 1
        return '{"has_subtitle": %s}' % ans


def _shots(n=3):
    return [FrameShot(i, i * 3.0, f"f{i}.jpg") for i in range(n)]


def test_verdict_true(monkeypatch):
    monkeypatch.setattr(frames_mod, "extract_frames",
                        lambda *a, **k: _shots(3))
    monkeypatch.setattr(vision_mod, "_encode_image", lambda p: "x")
    monkeypatch.setattr(vision_mod, "DoubaoVision",
                        lambda cfg: _FakeVision({"_answers": ["true", "true", "true"]}))
    verdict, reason = has_burned_subtitle("v.mp4", {}, "work")
    assert verdict is True


def test_verdict_false_by_majority(monkeypatch):
    monkeypatch.setattr(frames_mod, "extract_frames",
                        lambda *a, **k: _shots(3))
    monkeypatch.setattr(vision_mod, "_encode_image", lambda p: "x")
    monkeypatch.setattr(vision_mod, "DoubaoVision",
                        lambda cfg: _FakeVision({"_answers": ["false", "false", "true"]}))
    verdict, _ = has_burned_subtitle("v.mp4", {}, "work")
    assert verdict is False


def test_no_config_returns_unknown(monkeypatch, tmp_path):
    # 不 patch DoubaoVision：真实空配置应抛 ConfigError → 归类为"未配置豆包，跳过检测"
    verdict, reason = has_burned_subtitle("v.mp4", {}, str(tmp_path))
    assert verdict is None
    assert "未配置豆包" in reason


def test_frames_error_returns_unknown(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ffmpeg 没了")
    monkeypatch.setattr(frames_mod, "extract_frames", boom)
    monkeypatch.setattr(vision_mod, "_encode_image", lambda p: "x")
    monkeypatch.setattr(vision_mod, "DoubaoVision", lambda cfg: _FakeVision({}))
    verdict, reason = has_burned_subtitle("v.mp4", {}, "work")
    assert verdict is None and "检测失败" in reason


def test_parse_yesno_variants():
    assert _parse_yesno('{"has_subtitle": true}') is True
    assert _parse_yesno('{"has_subtitle": false}') is False
    assert _parse_yesno('{"has_subtitle": "是"}') is True
    assert _parse_yesno("没有 JSON") is None
