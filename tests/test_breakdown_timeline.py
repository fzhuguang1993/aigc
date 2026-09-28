"""
tests/test_breakdown_timeline.py —— 详情页时间轴纯逻辑（index_at / build_timeline）
"""
from video_text_tools.breakdown import timeline
from video_text_tools.breakdown.models import (
    BreakdownResult, FrameAnalysis, SegmentPrompts)
from video_text_tools.asr.types import TranscriptSegment


def test_index_at_boundaries():
    assert timeline.index_at([], 5) == -1
    assert timeline.index_at([10, 20, 30], 5) == -1       # 早于首个
    assert timeline.index_at([10, 20, 30], 10) == 0       # 恰等首个
    assert timeline.index_at([10, 20, 30], 15) == 0       # 区间内
    assert timeline.index_at([10, 20, 30], 20) == 1       # 恰等中间点取该点
    assert timeline.index_at([10, 20, 30], 100) == 2      # 晚于末个取末个


def test_build_timeline_shots_align_segments():
    r = BreakdownResult(
        frame_analyses=[FrameAnalysis(idx=1, ts=0.0, shot_size="特写", camera="推"),
                        FrameAnalysis(idx=2, ts=3.0, shot_size="中景", camera="固定")],
        segments=[SegmentPrompts(index=1, time_range="00:00-00:03", summary="钩子",
                                 visual_prompt="画面1", copy_prompt="文案1",
                                 shoot_prompt="复刻1"),
                  SegmentPrompts(index=2, time_range="00:03-00:06", summary="卖点",
                                 visual_prompt="画面2")],
        transcript=[TranscriptSegment(start=0.0, end=2.4, text="家人们"),
                    TranscriptSegment(start=3.0, end=5.0, text="今天分享")])
    tl = timeline.build_timeline(r)
    assert [s["ts"] for s in tl["shots"]] == [0.0, 3.0]
    assert tl["shots"][0]["time_range"] == "00:00-00:03"
    assert tl["shots"][0]["copy_prompt"] == "文案1"
    assert tl["shots"][1]["summary"] == "卖点"
    assert [ln["start"] for ln in tl["lines"]] == [0.0, 3.0]
    assert tl["lines"][1]["text"] == "今天分享"


def test_build_timeline_empty_safe():
    tl = timeline.build_timeline(BreakdownResult())
    assert tl == {"shots": [], "lines": []}


def test_build_timeline_shots_without_segments():
    # 分镜多于提示词段（早期降级）：段字段留空但分镜仍在
    r = BreakdownResult(frame_analyses=[FrameAnalysis(idx=1, ts=2.0)])
    tl = timeline.build_timeline(r)
    assert tl["shots"][0]["ts"] == 2.0
    assert tl["shots"][0]["time_range"] == ""
    assert tl["shots"][0]["visual_prompt"] == ""
