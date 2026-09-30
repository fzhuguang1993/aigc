"""
tests/test_splitter.py —— 板块切割归档素材库

覆盖：ffmpeg cut/concat 命令拼装（copy/reencode）、cut_segment 参数校验、
plan_cuts 的目录/命名/占用去重、手动区间成块、run_cuts 用假 ffmpeg 断言调用与落库。
"""
from pathlib import Path

import pytest

from video_text_tools import ffmpeg_utils as ff
from video_text_tools.breakdown import splitter
from video_text_tools.breakdown.models import BreakdownResult, BlockSegment
from store import material_store


# ---------------- 3.1 命令拼装 ----------------
def test_build_cut_command_copy():
    cmd = ff.build_cut_command("a.mp4", 3.5, 9.0, "out/b.mp4", ffmpeg="ffmpeg")
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd and "3.500" in cmd
    assert "-to" in cmd and "9.000" in cmd
    assert "-c" in cmd and "copy" in cmd
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "a.mp4"
    assert cmd[-1] == "out/b.mp4"
    assert "libx264" not in cmd


def test_build_cut_command_reencode():
    cmd = ff.build_cut_command("a.mp4", 0, 5, "b.mp4", reencode=True, ffmpeg="ffmpeg")
    assert "libx264" in cmd and "aac" in cmd
    assert "copy" not in cmd


def test_build_concat_command():
    cmd = ff.build_concat_command("list.txt", "out.mp4", ffmpeg="ffmpeg")
    assert "-f" in cmd and "concat" in cmd and "-safe" in cmd
    assert "-c" in cmd and "copy" in cmd
    assert cmd[cmd.index("-i") + 1] == "list.txt"
    assert cmd[-1] == "out.mp4"


def test_write_concat_list_quotes():
    tmp = Path(__file__)
    p = tmp.parent / "_concat_probe.txt"
    try:
        ff.write_concat_list(["a.mp4", "b'c.mp4"], p)
        text = p.read_text("utf-8")
        assert "file 'a.mp4'" in text
        assert "'\\''" in text                       # 单引号被转义
    finally:
        p.unlink(missing_ok=True)


def test_cut_segment_bad_args_no_run():
    ok, why = ff.cut_segment("nope.mp4", 5, 3, "out.mp4")     # end<=start
    assert ok is False and "区间非法" in why
    ok, why = ff.cut_segment("nope.mp4", 0, 5, "out.mp4")     # 源不存在
    assert ok is False and "源视频不存在" in why


# ---------------- 3.2 plan_cuts：目录 / 命名 / 去重 ----------------
@pytest.fixture
def result(tmp_path):
    video = tmp_path / "关节不舒服.mp4"
    video.write_bytes(b"x" * 64)
    r = BreakdownResult(link="https://v.douyin.com/x", title="关节不舒服",
                        video_path=str(video), task_id=42)
    r.blocks = [BlockSegment(1, "钩子", 0.0, 3.0, summary="开场"),
                BlockSegment(2, "行动号召", 3.0, 8.5, summary="下单")]
    return r


def test_plan_cuts_layout_and_naming(result, tmp_path):
    lib = tmp_path / "素材库"
    items = splitter.plan_cuts(result, mode="auto", product="骨胶原", lib_root=lib)
    assert len(items) == 2
    it = items[0]
    dst = Path(it["dst"])
    # 素材库/产品/板块类型/文件名
    assert dst.parent.parent.parent == lib
    assert dst.parent.parent.name == "骨胶原"
    assert dst.parent.name == "钩子"
    assert dst.name.startswith("关节不舒服_0000-0003") and dst.suffix == ".mp4"
    assert it["start"] == 0.0 and it["end"] == 3.0
    assert it["duration"] == 3.0 and it["source_task_id"] == 42


def test_plan_cuts_skips_bad_and_empty_src(result, tmp_path):
    lib = tmp_path / "lib"
    r2 = BreakdownResult(video_path=str(tmp_path / "missing.mp4"))
    r2.blocks = [BlockSegment(1, "钩子", 0, 3)]
    assert splitter.plan_cuts(r2, lib_root=lib) == []       # 源不在 → 空
    result.blocks.append(BlockSegment(3, "痛点", 5.0, 5.0))  # end<=start
    items = splitter.plan_cuts(result, lib_root=lib)
    assert len(items) == 2                                   # 非法区间被跳


def test_plan_cuts_dedup_occupied(result, tmp_path):
    lib = tmp_path / "lib"
    first = splitter.plan_cuts(result, lib_root=lib)[0]["dst"]
    Path(first).parent.mkdir(parents=True, exist_ok=True)
    Path(first).write_bytes(b"")                       # 占住这个名字
    second = splitter.plan_cuts(result, lib_root=lib)[0]["dst"]
    assert second != first and "(2)" in Path(second).name


def test_plan_cuts_manual(result, tmp_path):
    lib = tmp_path / "lib"
    manual = [{"type": "钩子", "start": 1.0, "end": 4.0, "summary": "手动"},
              {"type": "行动号召", "start": 20.0, "end": 25.0}]
    items = splitter.plan_cuts(result, mode="manual", manual=manual, lib_root=lib)
    assert [i["block_type"] for i in items] == ["钩子", "行动号召"]
    assert items[0]["start"] == 1.0 and items[0]["end"] == 4.0


# ---------------- run_cuts：假 ffmpeg 断言调用 + 落库 ----------------
@pytest.fixture
def clean_clips():
    material_store.db.execute("DELETE FROM material_clips")
    yield
    material_store.db.execute("DELETE FROM material_clips")


def test_run_cuts_fake_ffmpeg(result, tmp_path, monkeypatch, clean_clips):
    lib = tmp_path / "lib"
    items = splitter.plan_cuts(result, product="骨胶原", lib_root=lib)
    calls = []

    def fake_cut(src, start, end, dst, reencode=False, **kw):
        calls.append((src, start, end, dst, reencode))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"clip")
        return True, ""

    monkeypatch.setattr(splitter.ff, "cut_segment", fake_cut)
    res = splitter.run_cuts(items, storage=type("S", (), {"backend": "local"})())

    assert len(res["cut"]) == 2 and not res["failed"]
    assert len(calls) == 2
    rows = material_store.list_clips()
    assert len(rows) == 2
    hook = [r for r in rows if r["block_type"] == "钩子"][0]
    assert hook["product"] == "骨胶原"
    assert hook["source_task_id"] == 42
    assert Path(hook["path"]).exists()


def test_run_cuts_failure_skipped(result, tmp_path, monkeypatch, clean_clips):
    lib = tmp_path / "lib"
    items = splitter.plan_cuts(result, lib_root=lib)

    def fake_cut(src, start, end, dst, reencode=False, **kw):
        if "钩子" in dst:
            return False, "切割失败"
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"c")
        return True, ""

    monkeypatch.setattr(splitter.ff, "cut_segment", fake_cut)
    res = splitter.run_cuts(items, storage=type("S", (), {"backend": "local"})())
    assert len(res["cut"]) == 1 and len(res["failed"]) == 1
    assert len(material_store.list_clips()) == 1          # 只成功那条入库
