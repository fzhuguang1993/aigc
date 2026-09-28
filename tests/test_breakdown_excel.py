"""
tests/test_breakdown_excel.py —— 拆解结果回写任务表 12 列（临时 xlsx）

校验：write_breakdown 追加/定位行、多行文本原样保留（不像 update_row 折行）、
失败阶段在对应列写「XX失败：原因」。
"""
import pandas as pd
import pytest

import utils.excel_utils as ex
from core.config import (
    SHEET_TASK, COL_ID, COL_BK_LINK, COL_BK_SHOTS, COL_BK_SCRIPT,
    COL_BK_PROMPT_VISUAL, COL_BK_HOOK_SCORE, COL_BK_BLUEPRINT,
    BREAKDOWN_COLUMNS)
from video_text_tools.breakdown.models import (
    BreakdownResult, FrameAnalysis, SegmentPrompts, TranscriptSegment,
    OverallAnalysis, STAGE_ACQUIRE, STAGE_TRANSCRIBE, STAGE_VISION,
    STAGE_PROMPTS, STAGE_FRAMES, STAGE_REWRITE)


@pytest.fixture
def excel(tmp_path, monkeypatch):
    path = tmp_path / "tasks.xlsx"
    pd.DataFrame({COL_ID: ["A1", "A2"]}).to_excel(
        path, sheet_name=SHEET_TASK, index=False)
    monkeypatch.setattr(ex, "EXCEL_PATH", str(path))
    return path


def _full_result():
    r = BreakdownResult(link="https://v.douyin.com/x", title="标题T")
    for s in (STAGE_ACQUIRE, STAGE_FRAMES, STAGE_TRANSCRIBE,
              STAGE_VISION, STAGE_PROMPTS, STAGE_REWRITE):
        r.mark(s, ok=True)
    r.frame_analyses = [FrameAnalysis(1, 0.0, shot_size="特写", emotion="紧迫"),
                        FrameAnalysis(2, 3.0, shot_size="全景", camera="摇")]
    r.transcript = [TranscriptSegment(0.0, 3.0, "每天两条", [])]
    r.transcript_text = "每天两条\n肠道通畅"
    r.segments = [SegmentPrompts(1, "00:00-00:03", visual_prompt="画面A",
                                 copy_prompt="文案A", shoot_prompt="拍摄A"),
                  SegmentPrompts(2, "00:03-00:06", visual_prompt="画面B",
                                 copy_prompt="文案B", shoot_prompt="拍摄B")]
    r.overall = OverallAnalysis(hook_desc="抛痛点", hook_score="8",
                                factors="反差", emotion_curve="起伏",
                                formula="痛点+方案", blueprint="分步")
    return r


def test_append_new_row_and_columns(excel):
    placed = ex.write_breakdown(None, _full_result())
    df = ex.load_tasks()
    assert placed == len(df) - 1
    for col in BREAKDOWN_COLUMNS:
        assert col in df.columns
    row = df.iloc[placed]
    assert row[COL_BK_LINK] == "https://v.douyin.com/x"
    assert float(row[COL_BK_HOOK_SCORE]) == 8.0      # 读回被 pandas 推成数值
    assert row[COL_BK_BLUEPRINT] == "分步"


def test_multiline_preserved(excel):
    placed = ex.write_breakdown(None, _full_result())
    row = ex.load_tasks().iloc[placed]
    # 分镜两帧两行、口播含换行、提示词逐段——都必须留住 \n
    assert "\n" in str(row[COL_BK_SHOTS])
    assert str(row[COL_BK_SCRIPT]) == "每天两条\n肠道通畅"
    assert "画面A" in str(row[COL_BK_PROMPT_VISUAL])
    assert "画面B" in str(row[COL_BK_PROMPT_VISUAL])
    assert str(row[COL_BK_PROMPT_VISUAL]).count("\n") >= 1


def test_write_to_existing_row(excel):
    ex.write_breakdown(1, _full_result())
    df = ex.load_tasks()
    assert len(df) == 2                              # 没新增行
    assert df.iloc[1][COL_BK_LINK] == "https://v.douyin.com/x"
    assert str(df.iloc[0][COL_BK_LINK]) in ("", "nan")   # 另一行没被动（空读回 NaN）


def test_failure_annotation(excel):
    r = _full_result()
    r.mark(STAGE_TRANSCRIBE, ok=False, reason="没装 faster-whisper")
    r.mark(STAGE_VISION, ok=False, reason="限流")
    ex.write_breakdown(None, r)
    row = ex.load_tasks().iloc[-1]
    assert str(row[COL_BK_SCRIPT]).startswith("口播转写失败")
    assert "没装 faster-whisper" in str(row[COL_BK_SCRIPT])
    assert str(row[COL_BK_SHOTS]).startswith("画面分析失败")


def test_breakdown_cells_pure():
    r = _full_result()
    cells = ex.breakdown_cells(r)
    assert set(cells) == set(BREAKDOWN_COLUMNS)
    assert cells[COL_BK_HOOK_SCORE] == "8"
