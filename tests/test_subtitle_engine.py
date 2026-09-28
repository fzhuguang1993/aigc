"""
tests/test_subtitle_engine.py —— run_one 编排（全 mock：转写/音轨/检测/高亮/烧录）

钉住几条契约，任何一步都不许冒泡：
- happy：出 .srt + .ass，ok=True；
- burn=True 且 libass 就位 → burned_path 落 <stem>_字幕.mp4、调 run_burn；
- ffmpeg 不含 libass → ok=True 且说明"未烧录"（不假成功、不静默）；
- DepMissing / ModelNotReady → ok=False + error 归类，且不抛；
- detect 判定 True → 跳过（"已含字幕"）；判定 None → 保守当作无字幕继续补生成；
- highlight=True → 词表进 SubtitleResult 并喂给 ASS。

patch 目标看导入方式：engine 顶层 `from .burn import ...` 绑进 engine 命名空间 → patch engine.*；
detect/highlight/transcribe 在函数内 import → patch 各自模块属性。
"""
import video_text_tools.ffmpeg_utils as ff
from video_text_tools.asr.types import (TranscriptSegment, DepMissing,
                                         ModelNotReady)
import video_text_tools.asr.transcribe as tr
import video_text_tools.asr.fixer as fixer_mod
import core.config as cfg_mod
from video_text_tools.subtitle import audio as audio_mod
from video_text_tools.subtitle import detect as detect_mod
from video_text_tools.subtitle import engine as engine_mod
from video_text_tools.subtitle import highlight as hl_mod
from video_text_tools.subtitle.models import SubtitleOptions, SubtitleStyle

SEGS = [TranscriptSegment(0.0, 2.0, "限时抢购美白面膜"),
        TranscriptSegment(2.0, 4.0, "只要 99 元")]


def _fake_transcribe(video, size=None, log=None, should_stop=None):
    return SEGS, "\n".join(s.text for s in SEGS)


def _fake_resolve(video, work_dir):
    return str(video), False


def _video(tmp_path):
    v = tmp_path / "录屏_1.mp4"
    v.write_bytes(b"x")
    return str(v)


def _base(monkeypatch):
    """公共底：音轨 + 转写 + 配置都打桩，各用例只补自己关心的那步。"""
    monkeypatch.setattr(audio_mod, "resolve_audio_source", _fake_resolve)
    monkeypatch.setattr(audio_mod, "find_sidecar", lambda v: None)
    monkeypatch.setattr(tr, "transcribe", _fake_transcribe)
    monkeypatch.setattr(ff, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(cfg_mod, "doubao_vision_config", lambda: {})
    monkeypatch.setattr(cfg_mod, "subtitle_config",
                        lambda: {"output_suffix": "_字幕"})


def test_happy_writes_srt_and_ass(tmp_path, monkeypatch):
    _base(monkeypatch)
    opts = SubtitleOptions(gen_srt=True, highlight=False, burn=False)
    res = engine_mod.run_one(_video(tmp_path), opts)
    assert res.ok and res.error == ""
    assert res.srt_path.endswith(".srt") and (tmp_path / "录屏_1.srt").is_file()
    assert res.ass_path.endswith(".ass") and (tmp_path / "录屏_1.ass").is_file()


def test_burn_produces_output_path(tmp_path, monkeypatch):
    _base(monkeypatch)
    called = {}
    monkeypatch.setattr(engine_mod, "ffmpeg_has_ass", lambda f: True)

    def fake_burn(cmd, log=None, should_stop=None):
        called["cmd"] = cmd
        return True
    monkeypatch.setattr(engine_mod, "run_burn", fake_burn)

    opts = SubtitleOptions(gen_srt=False, burn=True)
    res = engine_mod.run_one(_video(tmp_path), opts)
    assert res.ok
    assert res.burned_path.endswith("录屏_1_字幕.mp4")
    assert "cmd" in called                                 # 真去烧了


def test_burn_skipped_without_libass(tmp_path, monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(engine_mod, "ffmpeg_has_ass", lambda f: False)
    monkeypatch.setattr(engine_mod, "run_burn",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该烧")))
    opts = SubtitleOptions(gen_srt=True, burn=True)
    res = engine_mod.run_one(_video(tmp_path), opts)
    assert res.ok and res.burned_path == ""               # 文件出了、没烧
    assert "libass" in res.message


def test_dep_missing_classified_not_raised(tmp_path, monkeypatch):
    _base(monkeypatch)
    def boom(*a, **k):
        raise DepMissing("没装 faster-whisper")
    monkeypatch.setattr(tr, "transcribe", boom)
    res = engine_mod.run_one(_video(tmp_path), SubtitleOptions())
    assert res.ok is False and res.error == "DepMissing"
    assert "faster-whisper" in res.message


def test_model_not_ready_classified(tmp_path, monkeypatch):
    _base(monkeypatch)
    def boom(*a, **k):
        raise ModelNotReady("模型没下")
    monkeypatch.setattr(tr, "transcribe", boom)
    res = engine_mod.run_one(_video(tmp_path), SubtitleOptions())
    assert res.ok is False and res.error == "ModelNotReady"


def test_detect_true_skips(tmp_path, monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(detect_mod, "has_burned_subtitle",
                        lambda *a, **k: (True, "已有字幕"))
    opts = SubtitleOptions(detect=True, gen_srt=True)
    res = engine_mod.run_one(_video(tmp_path), opts)
    assert res.ok and res.has_subtitle is True
    assert "已含字幕" in res.message
    assert res.srt_path == ""                              # 提前返回，没再出文件


def test_detect_unknown_continues(tmp_path, monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(detect_mod, "has_burned_subtitle",
                        lambda *a, **k: (None, "未配置豆包"))
    opts = SubtitleOptions(detect=True, gen_srt=True)
    res = engine_mod.run_one(_video(tmp_path), opts)
    assert res.ok and res.has_subtitle is None
    assert res.srt_path.endswith(".srt")                   # 保守当作无字幕补生成


def test_highlight_terms_flow_to_ass(tmp_path, monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(hl_mod, "pick_terms",
                        lambda segs, c, log=None: ["限时"])
    opts = SubtitleOptions(highlight=True, gen_srt=False)
    res = engine_mod.run_one(_video(tmp_path), opts)
    assert res.ok and res.highlight_terms == ["限时"]
    ass = (tmp_path / "录屏_1.ass").read_text(encoding="utf-8-sig")
    assert "{\\c&H0000D4FF&}限时{\\c}" in ass


def test_run_batch_aggregates_without_stopping(tmp_path, monkeypatch):
    _base(monkeypatch)
    vids = [_video(tmp_path), str(tmp_path / "b.mp4")]
    (tmp_path / "b.mp4").write_bytes(b"y")
    out = engine_mod.run_batch(vids, SubtitleOptions(gen_srt=True))
    assert len(out) == 2 and all(r.ok for r in out)


def test_save_txt_writes_transcript(tmp_path, monkeypatch):
    """save_txt → 逐字稿落 <stem>.txt（一行一句）；gen_srt=False 不碍事。"""
    _base(monkeypatch)
    opts = SubtitleOptions(save_txt=True, gen_srt=False, burn=False)
    res = engine_mod.run_one(_video(tmp_path), opts)
    assert res.ok and res.txt_path.endswith(".txt")
    body = (tmp_path / "录屏_1.txt").read_text(encoding="utf-8")
    assert body.splitlines() == ["限时抢购美白面膜", "只要 99 元"]


def test_asr_fix_corrects_before_render(tmp_path, monkeypatch):
    """asr_fix 开：转写后先过 apply_fix（就地改 seg.text），SRT 用纠正后文本，note 进 message。"""
    _base(monkeypatch)

    def fake_apply(segments, glossary=None, cfg=None):
        segments[0].text = "限时抢购美白面膜（已纠错）"
        return 1, {"fix_calls": 1, "fix_tokens": 8}, "已 DeepSeek 语义纠错：改 1/2 句"
    monkeypatch.setattr(fixer_mod, "apply_fix", fake_apply)

    opts = SubtitleOptions(asr_fix=True, gen_srt=True, burn=False)
    res = engine_mod.run_one(_video(tmp_path), opts)
    assert res.ok and "纠错" in res.message
    assert res.transcript_text.splitlines()[0] == "限时抢购美白面膜（已纠错）"
    srt = (tmp_path / "录屏_1.srt").read_text(encoding="utf-8-sig")
    assert "已纠错" in srt                                  # 字幕用纠正后文本


def test_asr_fix_off_by_default_does_not_call(tmp_path, monkeypatch):
    """asr_fix 默认关：绝不碰 apply_fix（不擅自联网）。"""
    _base(monkeypatch)
    monkeypatch.setattr(fixer_mod, "apply_fix",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调纠错")))
    res = engine_mod.run_one(_video(tmp_path), SubtitleOptions(gen_srt=True))
    assert res.ok
