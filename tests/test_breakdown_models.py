"""
tests/test_breakdown_models.py —— 拆解结果序列化往返（to_dict / from_dict）

校验 BreakdownResult 及其嵌套 dataclass 经 JSON 往返后字段不丢、类型复原，
新增的任务库可选字段（cover_path/gallery/task_id 等）也在往返范围内。
"""
import json

from video_text_tools.breakdown.models import (
    BreakdownResult, FrameAnalysis, SegmentPrompts, OverallAnalysis,
    STAGE_ACQUIRE, STAGE_VISION, STAGE_PROMPTS,
)
from video_text_tools.asr.types import TranscriptSegment, TranscriptWord


def _full_result():
    return BreakdownResult(
        link="https://v.douyin.com/x/", title="骨胶原爆款",
        video_path="/tmp/骨胶原-9.27-142_11.mp4", duration=31.5,
        frame_analyses=[
            FrameAnalysis(idx=1, ts=0.0, shot_size="特写", camera="推",
                          composition="居中", transition="", on_screen_text="开头",
                          emotion="紧张", raw="..."),
            FrameAnalysis(idx=2, ts=3.0, shot_size="中景", camera="固定",
                          composition="三分", transition="切", on_screen_text="",
                          emotion="平静", raw=""),
        ],
        transcript=[
            TranscriptSegment(start=0.0, end=2.4, text="家人们",
                              words=[TranscriptWord(word="家", start=0.0, end=0.5)]),
            TranscriptSegment(start=3.0, end=5.0, text="今天分享"),
        ],
        transcript_text="家人们 今天分享",
        segments=[
            SegmentPrompts(index=1, time_range="00:00-00:03", visual_prompt="画面1",
                           copy_prompt="文案1", shoot_prompt="复刻1", summary="钩子"),
            SegmentPrompts(index=2, time_range="00:03-00:06", visual_prompt="画面2",
                           copy_prompt="文案2", shoot_prompt="复刻2", summary="卖点"),
        ],
        overall=OverallAnalysis(hook_desc="反差开场", hook_score="8",
                                factors="节奏", emotion_curve="上扬",
                                formula="痛点+方案", blueprint="分镜脚本"),
        shot_count=2,
        stage_status={STAGE_ACQUIRE: "ok", STAGE_VISION: "ok", STAGE_PROMPTS: "ok"},
        cost={"vision_calls": 3, "tokens": 1234},
        notes=["降级提示"],
        report_path="/tmp/骨胶原-9.27-142_11.docx", cover_path="/tmp/cover.jpg",
        gallery=[{"idx": 1, "ts": 0.0, "path": "/tmp/gallery/f001.jpg"}],
        task_id=7, source_kind="url",
    )


def test_roundtrip_preserves_fields():
    r = _full_result()
    d = json.loads(json.dumps(r.to_dict(), ensure_ascii=False))
    r2 = BreakdownResult.from_dict(d)

    assert r2.title == "骨胶原爆款"
    assert r2.video_path == r.video_path
    assert r2.duration == 31.5
    assert r2.shot_count == 2
    assert r2.transcript_text == "家人们 今天分享"
    assert r2.report_path == r.report_path
    assert r2.cover_path == r.cover_path
    assert r2.task_id == 7
    assert r2.source_kind == "url"
    assert r2.stage_status == r.stage_status
    assert r2.cost == {"vision_calls": 3, "tokens": 1234}
    assert r2.notes == ["降级提示"]
    assert r2.gallery == [{"idx": 1, "ts": 0.0, "path": "/tmp/gallery/f001.jpg"}]


def test_roundtrip_rebuilds_nested_types():
    r2 = BreakdownResult.from_dict(_full_result().to_dict())
    assert all(isinstance(a, FrameAnalysis) for a in r2.frame_analyses)
    assert all(isinstance(s, SegmentPrompts) for s in r2.segments)
    assert isinstance(r2.overall, OverallAnalysis)
    assert isinstance(r2.transcript[0], TranscriptSegment)
    assert isinstance(r2.transcript[0].words[0], TranscriptWord)
    assert r2.frame_analyses[1].camera == "固定"
    assert r2.segments[1].shoot_prompt == "复刻2"
    assert r2.overall.formula == "痛点+方案"
    assert r2.transcript[0].words[0].word == "家"


def test_from_dict_tolerant_of_missing_and_extra():
    # 旧 payload：缺新字段 + 混入未知键，都应正常重建、不抛
    data = {"title": "旧记录", "frames": [], "mystery_key": 123}
    r = BreakdownResult.from_dict(data)
    assert r.title == "旧记录"
    assert r.task_id == 0
    assert r.gallery == []
    assert r.overall.hook_desc == ""


def test_empty_result_roundtrip():
    r = BreakdownResult()
    r2 = BreakdownResult.from_dict(json.loads(json.dumps(r.to_dict())))
    assert r2.is_partial()          # 没解析成功 → 半成品
    assert r2.gallery == [] and r2.cost.get("vision_calls") == 0
