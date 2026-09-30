"""
tests/test_breakdown_blocks.py —— 板块拆分（营销板块时间轴）

覆盖：BlockSegment 序列化往返、BreakdownResult.blocks 往返、build_blocks 的
白名单/钳位/去重叠兜底与降级单块「整片」、config blocks_types、pipeline 接入
（产出 result.blocks + 命中缓存跳过豆包调用）。
"""
import json

import pytest

from video_text_tools.breakdown import pipeline
from video_text_tools.breakdown import prompts as prompts_mod
from video_text_tools.breakdown import vision as vision_mod
from video_text_tools.breakdown.models import (
    BlockSegment, BreakdownResult, FrameShot, FrameAnalysis, TranscriptSegment,
    STAGE_ACQUIRE, STAGE_VISION, STAGE_BLOCKS)
from core.config import breakdown_config

FAKE_CFG = {"api_key": "k", "endpoint": "ep-1", "base_url": "https://x/api/v3"}


# ---------------- 2.1 数据模型：序列化往返 ----------------
def test_block_segment_roundtrip():
    b = BlockSegment(index=1, type="钩子", start=0.0, end=3.5, summary="开场")
    d = b.to_dict()
    assert d == {"index": 1, "type": "钩子", "start": 0.0, "end": 3.5, "summary": "开场"}
    assert BlockSegment.from_dict(d) == b


def test_result_blocks_roundtrip():
    r = BreakdownResult(link="x", blocks=[
        BlockSegment(1, "钩子", 0.0, 3.0),
        BlockSegment(2, "行动号召", 3.0, 8.0, summary="结尾引导"),
    ])
    r2 = BreakdownResult.from_dict(json.loads(json.dumps(r.to_dict())))
    assert len(r2.blocks) == 2
    assert all(isinstance(b, BlockSegment) for b in r2.blocks)
    assert r2.blocks[1].type == "行动号召" and r2.blocks[1].end == 8.0


def test_from_dict_tolerant_missing_blocks():
    r = BreakdownResult.from_dict({"title": "旧记录"})   # 老 payload 无 blocks
    assert r.blocks == []


# ---------------- 2.2 _normalize_blocks：白名单/钳位/去重叠 ----------------
def test_normalize_filters_whitelist_clamps_and_dedups():
    types = ["钩子", "痛点", "行动号召"]
    dur = 10.0
    raw = [
        {"type": "钩子", "start": 0, "end": 3},          # ok
        {"type": "广告", "start": 3, "end": 5},          # 白名单外 → 丢
        {"type": "痛点", "start": 2, "end": 6},          # 与钩子重叠 → 丢
        {"type": "行动号召", "start": 6, "end": 99},     # 越界 → 钳到 10
        {"type": "痛点", "start": 12, "end": 15},        # start>duration → 钳后 start>=end 丢
    ]
    out = prompts_mod._normalize_blocks(raw, types, dur)
    assert [b.type for b in out] == ["钩子", "行动号召"]
    assert out[0].start == 0.0 and out[0].end == 3.0
    assert out[1].end == 10.0                            # 钳到 duration
    assert [b.index for b in out] == [1, 2]              # 重排连续编号


def test_normalize_bad_numbers_dropped():
    out = prompts_mod._normalize_blocks(
        [{"type": "钩子", "start": "abc", "end": 3},
         {"type": "钩子", "start": 0, "end": None},
         {"type": "钩子", "start": 1, "end": 4}],
        ["钩子"], 10.0)
    assert len(out) == 1 and out[0].end == 4.0


def test_normalize_zero_duration_empty():
    assert prompts_mod._normalize_blocks([{"type": "钩子", "start": 0, "end": 3}],
                                         ["钩子"], 0) == []


class _FakeChatVision:
    """只提供 chat_text：按 prompt 关键字回板块 JSON / 抛错，用于驱动 build_blocks。"""

    def __init__(self, reply=None, boom=False):
        self._reply, self._boom = reply, boom
        self.calls = 0

    def chat_text(self, prompt, temperature=0.4, max_tokens=2048):
        self.calls += 1
        if self._boom:
            raise RuntimeError("限流")
        return self._reply


def _shots():
    return [{"idx": 1, "start": 0.0, "end": 5.0, "time_range": "00:00-00:05",
             "shot_size": "特写", "camera": "推", "composition": "", "transition": "",
             "on_screen_text": "", "emotion": "", "speech": "家人们"},
            {"idx": 2, "start": 5.0, "end": 10.0, "time_range": "00:05-00:10",
             "shot_size": "中景", "camera": "", "composition": "", "transition": "",
             "on_screen_text": "", "emotion": "", "speech": "点赞关注"}]


def test_build_blocks_happy_path():
    reply = json.dumps([
        {"type": "钩子", "start": 0, "end": 5, "summary": "抛痛点"},
        {"type": "行动号召", "start": 5, "end": 10, "summary": "引导下单"},
    ], ensure_ascii=False)
    v = _FakeChatVision(reply)
    blocks, ok = prompts_mod.build_blocks(_shots(), "口播", v,
                                          types=["钩子", "行动号召"], duration=10.0)
    assert ok and len(blocks) == 2
    assert blocks[0].type == "钩子" and blocks[1].end == 10.0


def test_build_blocks_degrade_on_bad_json():
    v = _FakeChatVision("这不是 JSON")
    blocks, ok = prompts_mod.build_blocks(_shots(), "口播", v, duration=10.0)
    assert ok and len(blocks) == 1                     # 降级仍算 ok（不拖累主链）
    assert blocks[0].type == "整片" and blocks[0].start == 0.0 and blocks[0].end == 10.0


def test_build_blocks_degrade_on_exception():
    v = _FakeChatVision(boom=True)
    blocks, ok = prompts_mod.build_blocks(_shots(), "口播", v, duration=10.0)
    assert ok and blocks[0].type == "整片"


def test_build_blocks_no_duration_empty():
    v = _FakeChatVision("[]")
    blocks, ok = prompts_mod.build_blocks(_shots(), "口播", v, duration=0)
    assert blocks == [] and ok is False
    assert v.calls == 0                                # 无时长不发请求


# ---------------- config：blocks_types ----------------
def test_config_blocks_types_default(monkeypatch):
    monkeypatch.setattr("core.config.read_section",
                        lambda name, d: dict(d))
    cfg = breakdown_config()
    assert cfg["blocks_types"] == ["钩子", "痛点", "产品介绍",
                                   "权威背书", "使用场景", "行动号召"]


def test_config_blocks_types_custom_and_clean(monkeypatch):
    monkeypatch.setattr("core.config.read_section", lambda name, d: {
        "vision_mode": "video_url", "fps": 0.5,
        "blocks_types": [" 钩子 ", "", "CTA", None]})
    cfg = breakdown_config()
    assert cfg["blocks_types"] == ["钩子", "CTA"]      # 去空白/空项，保序


# ---------------- pipeline 接入：产出 + 缓存命中 ----------------
class FakeVision:
    instances = 0

    def __init__(self, cfg):
        FakeVision.instances += 1
        self.calls = 0

    def analyze_frames(self, frames, prompt, concurrency=1, log=None,
                       progress=None, inter_frame_delay=0.0):
        return [FrameAnalysis(idx=f.idx, ts=f.ts, shot_size="特写") for f in frames]

    def chat_text(self, prompt, temperature=0.4, max_tokens=2048):
        self.calls += 1
        if "板块" in prompt and "type" in prompt:
            return json.dumps([
                {"type": "钩子", "start": 0, "end": 3, "summary": "开场"},
                {"type": "行动号召", "start": 3, "end": 6, "summary": "结尾"},
            ], ensure_ascii=False)
        if "整体分析" in prompt:
            return json.dumps({"hook_desc": "钩子", "hook_score": "8"})
        if "规避平台查重" in prompt:
            return json.dumps([{"index": 1, "copy_prompt": "改写后"}])
        return json.dumps([{"index": 1, "time_range": "00:00-00:03",
                            "summary": "开场", "visual_prompt": "画面V",
                            "copy_prompt": "原文案", "shoot_prompt": "拍摄S"}])

    def cost(self):
        return {"vision_calls": self.calls, "tokens": 100}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 512)

    def acq(*a, **k):
        return str(video), "标题T", ["备注"], "https://x/v.mp4"

    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", acq)
    monkeypatch.setattr(pipeline.transcribe_mod, "transcribe",
                        lambda *a, **k: ([TranscriptSegment(0.0, 3.0, "每天两条", [])],
                                         "每天两条"))
    monkeypatch.setattr(pipeline.frames_mod, "extract_frames",
                        lambda *a, **k: [FrameShot(1, 0.0, "f1.jpg"),
                                         FrameShot(2, 3.0, "f2.jpg")])
    monkeypatch.setattr(vision_mod, "DoubaoVision", FakeVision)
    FakeVision.instances = 0
    cache_dir = tmp_path / "cache"
    opts = {"doubao_cfg": FAKE_CFG, "out_dir": str(tmp_path / "out"),
            "model_size": "tiny", "vision_mode": "frames",
            "cache": True, "cache_dir": str(cache_dir),
            "block_types": ["钩子", "痛点", "行动号召"]}
    return opts, cache_dir


LINK = "https://v.douyin.com/blk"


def test_pipeline_produces_blocks(wired):
    opts, cache_dir = wired
    r = pipeline.run(LINK, opts)
    assert r.stage_ok(STAGE_BLOCKS)
    assert not r.is_partial()                          # 板块阶段不污染半成品判定
    assert [b.type for b in r.blocks] == ["钩子", "行动号召"]


def test_pipeline_blocks_hit_cache_skips(wired):
    from video_text_tools.breakdown import cache as bd_cache
    opts, cache_dir = wired
    pipeline.run(LINK, opts)
    key = bd_cache.link_key(LINK)
    assert bd_cache.is_stage_ok(key, STAGE_BLOCKS, base=cache_dir)
    logs2 = []
    r2 = pipeline.run(LINK, opts, log=logs2.append)
    assert "跳过板块拆分" in "\n".join(logs2)
    assert r2.stage_ok(STAGE_ACQUIRE)                  # 全命中：解析下载都跳了
    assert [b.type for b in r2.blocks] == ["钩子", "行动号召"]


def test_pipeline_blocks_disabled(wired):
    opts, cache_dir = wired
    opts = dict(opts, use_blocks=False)
    r = pipeline.run(LINK, opts)
    assert r.blocks == []                              # 关掉：不产出板块
    assert STAGE_BLOCKS not in r.stage_status          # 阶段未执行
