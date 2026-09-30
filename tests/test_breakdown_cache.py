"""
tests/test_breakdown_cache.py —— 阶段缓存 + 断点续跑

link_key 规范化 / 稳定性；save→load 往返与坏文件自愈；pipeline 用假 acquire/
transcribe/豆包验证「同链接第二次零接口调用」「删某阶段只补跑该阶段」「force 全重跑」。
"""
import json

import pytest

from video_text_tools.breakdown import pipeline
from video_text_tools.breakdown import cache as bd_cache
from video_text_tools.breakdown import vision as vision_mod
from video_text_tools.breakdown.models import (
    TranscriptSegment, FrameShot, FrameAnalysis,
    STAGE_ACQUIRE, STAGE_TRANSCRIBE, STAGE_VISION, STAGE_PROMPTS)

FAKE_CFG = {"api_key": "k", "endpoint": "ep-1", "base_url": "https://x/api/v3"}


# ---------------- link_key：规范化 + 稳定性 ----------------
class TestLinkKey:
    def test_strips_tracking_params(self):
        a = bd_cache.link_key("https://v.douyin.com/abc/?utm_source=x&mid=1")
        b = bd_cache.link_key("https://v.douyin.com/abc/?share_times=2")
        assert a == b                              # 跟踪参数不进 key

    def test_extracts_url_from_share_text(self):
        noisy = "7.99 复制打开抖音，看看 https://v.douyin.com/abc/ 和ta的"
        assert bd_cache.link_key(noisy) == bd_cache.link_key("https://v.douyin.com/abc/")

    def test_case_and_trailing_slash_normalized(self):
        assert bd_cache.link_key("HTTPS://V.Douyin.com/ABC/") == \
            bd_cache.link_key("https://v.douyin.com/abc")

    def test_different_links_differ(self):
        assert bd_cache.link_key("https://v.douyin.com/aaa") != \
            bd_cache.link_key("https://v.douyin.com/bbb")


# ---------------- save/load 往返 + 坏文件自愈 ----------------
class TestStageRoundtrip:
    def test_roundtrip(self, tmp_path):
        key = "k1"
        assert not bd_cache.is_stage_ok(key, STAGE_TRANSCRIBE, base=tmp_path)
        bd_cache.save_stage(key, STAGE_TRANSCRIBE,
                            {"segments": [], "text": "你好"}, base=tmp_path)
        assert bd_cache.is_stage_ok(key, STAGE_TRANSCRIBE, base=tmp_path)
        assert bd_cache.load_stage(key, STAGE_TRANSCRIBE, base=tmp_path) == \
            {"segments": [], "text": "你好"}

    def test_bad_file_treated_as_miss(self, tmp_path):
        key = "k2"
        bd_cache.save_stage(key, STAGE_VISION, {"frame_analyses": []}, base=tmp_path)
        # 产物文件写坏 → 视为未缓存（不抛）
        (bd_cache.dir_for(key, tmp_path) / "vision.json").write_text("{坏json", "utf-8")
        assert bd_cache.load_stage(key, STAGE_VISION, base=tmp_path) is None

    def test_missing_file_clears_hit(self, tmp_path):
        key = "k3"
        bd_cache.save_stage(key, STAGE_ACQUIRE, {"title": "t"}, base=tmp_path)
        (bd_cache.dir_for(key, tmp_path) / "acquire.json").unlink()
        assert not bd_cache.is_stage_ok(key, STAGE_ACQUIRE, base=tmp_path)

    def test_drop_stage(self, tmp_path):
        key = "k4"
        bd_cache.save_stage(key, STAGE_PROMPTS, {"segments": []}, base=tmp_path)
        bd_cache.drop_stage(key, STAGE_PROMPTS, base=tmp_path)
        assert not bd_cache.is_stage_ok(key, STAGE_PROMPTS, base=tmp_path)


# ---------------- pipeline 续跑（调用计数） ----------------
class FakeVision:
    instances = 0

    def __init__(self, cfg):
        FakeVision.instances += 1
        self.calls = 0

    def analyze_frames(self, frames, prompt, concurrency=1, log=None,
                       progress=None, inter_frame_delay=0.0):
        return [FrameAnalysis(idx=f.idx, ts=f.ts, shot_size="特写") for f in frames]

    def analyze_video(self, url, prompt=None, fps=0.5, timeout=600, log=None):
        self.calls += 1
        return [FrameAnalysis(idx=1, ts=0.0), FrameAnalysis(idx=2, ts=4.0)]

    def chat_text(self, prompt, temperature=0.4, max_tokens=2048):
        self.calls += 1
        if "整体分析" in prompt:
            return json.dumps({"hook_desc": "钩子", "hook_score": "8",
                               "factors": "f", "emotion_curve": "e",
                               "formula": "fo", "blueprint": "b"})
        if "规避平台查重" in prompt:
            return json.dumps([{"index": 1, "copy_prompt": "改写后"}])
        return json.dumps([{"index": 1, "time_range": "00:00-00:03",
                            "summary": "开场", "visual_prompt": "画面V",
                            "copy_prompt": "原文案", "shoot_prompt": "拍摄S"}])

    def cost(self):
        return {"vision_calls": self.calls, "tokens": 100}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """装好假全链路 + 调用计数；返回 (opts, counters, cache_dir, video)。"""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 512)
    counters = {"acquire": 0, "transcribe": 0}

    def acq(*a, **k):
        counters["acquire"] += 1
        return str(video), "标题T", ["备注"], "https://x/v.mp4"

    def tr(*a, **k):
        counters["transcribe"] += 1
        return [TranscriptSegment(0.0, 3.0, "每天两条", [])], "每天两条"

    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", acq)
    monkeypatch.setattr(pipeline.transcribe_mod, "transcribe", tr)
    monkeypatch.setattr(pipeline.frames_mod, "extract_frames",
                        lambda *a, **k: [FrameShot(1, 0.0, "f1.jpg"),
                                         FrameShot(2, 3.0, "f2.jpg")])
    monkeypatch.setattr(vision_mod, "DoubaoVision", FakeVision)
    FakeVision.instances = 0
    cache_dir = tmp_path / "cache"
    opts = {"doubao_cfg": FAKE_CFG, "out_dir": str(tmp_path / "out"),
            "model_size": "tiny", "vision_mode": "frames",
            "cache": True, "cache_dir": str(cache_dir)}
    return opts, counters, cache_dir


LINK = "https://v.douyin.com/abc"


def test_full_cache_hit_skips_all_calls(wired):
    opts, counters, cache_dir = wired
    r1 = pipeline.run(LINK, opts)
    assert counters["acquire"] == 1 and counters["transcribe"] == 1
    assert FakeVision.instances >= 1                # 首跑建了豆包客户端
    assert not r1.is_partial()

    FakeVision.instances = 0
    logs2 = []
    r2 = pipeline.run(LINK, opts, log=logs2.append)   # 第二次：全命中
    assert counters["acquire"] == 1                 # 没再下载
    assert counters["transcribe"] == 1              # 没再转写
    assert FakeVision.instances == 0                # 根本没建豆包客户端
    assert not r2.is_partial()
    assert r2.title == "标题T" and r2.transcript_text == "每天两条"
    assert r2.segments and r2.overall.hook_score == "8"
    joined = "\n".join(logs2)
    assert "命中缓存" in joined and "跳过解析下载" in joined


def test_resume_recomputes_only_missing_stage(wired):
    opts, counters, cache_dir = wired
    pipeline.run(LINK, opts)
    key = bd_cache.link_key(LINK)
    bd_cache.drop_stage(key, STAGE_PROMPTS, base=cache_dir)   # 只作废提示词阶段
    counters_before = dict(counters)
    FakeVision.instances = 0

    r2 = pipeline.run(LINK, opts)
    assert counters == counters_before              # acquire/transcribe 仍命中
    assert FakeVision.instances == 1                # 只有提示词重跑才重建豆包客户端
    assert r2.stage_ok(STAGE_PROMPTS)


def test_force_bypasses_cache(wired):
    opts, counters, cache_dir = wired
    pipeline.run(LINK, opts)
    assert counters["acquire"] == 1
    opts2 = dict(opts, force=True)
    pipeline.run(LINK, opts2)
    assert counters["acquire"] == 2                 # 强制重跑：重新下载
    assert counters["transcribe"] == 2


def test_cache_off_by_default(wired):
    opts, counters, cache_dir = wired
    opts = {k: v for k, v in opts.items() if k not in ("cache", "cache_dir")}
    pipeline.run(LINK, opts)
    pipeline.run(LINK, opts)                        # 没开缓存：每次都全跑
    assert counters["acquire"] == 2 and counters["transcribe"] == 2
