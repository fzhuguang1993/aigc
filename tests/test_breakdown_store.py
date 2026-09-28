"""
tests/test_breakdown_store.py —— 拆解任务库入库/读取/列表过滤/产品关联（临时 AIGC_HOME 库）

覆盖：
  · save→list_tasks→load_result 往返、partial 标记、标题兜底、删除、排序；
  · only_ok 过滤（只有完整成功任务进列表，半成品/失败不进）；
  · 任务 ↔ 产品 多对多关联读写与删除级联（按产品沉淀爆款拆解样本）。
"""
import pytest

from store import db, breakdown_store, product_store
from video_text_tools.breakdown.models import (
    BreakdownResult, FrameAnalysis, SegmentPrompts, OverallAnalysis, STAGES,
    STAGE_ACQUIRE)
from video_text_tools.asr.types import TranscriptSegment


@pytest.fixture(autouse=True)
def _clean_bd():
    db.execute("DELETE FROM breakdown_tasks")
    db.execute("DELETE FROM breakdown_task_products")
    yield
    db.execute("DELETE FROM breakdown_tasks")
    db.execute("DELETE FROM breakdown_task_products")


def _ok_result():
    return BreakdownResult(
        link="https://v.douyin.com/x/", title="骨胶原爆款",
        video_path="/tmp/v.mp4", duration=31.5, shot_count=2,
        frame_analyses=[FrameAnalysis(idx=1, ts=0.0, shot_size="特写"),
                        FrameAnalysis(idx=2, ts=3.0, shot_size="中景")],
        transcript=[TranscriptSegment(start=0.0, end=2.4, text="家人们")],
        segments=[SegmentPrompts(index=1, time_range="00:00-00:03", visual_prompt="画面1"),
                  SegmentPrompts(index=2, time_range="00:03-00:06", visual_prompt="画面2")],
        overall=OverallAnalysis(hook_desc="反差开场", hook_score="8", formula="痛点+方案"),
        stage_status={STAGE_ACQUIRE: "ok"},
        report_path="/tmp/v.docx", cover_path="/tmp/lib/cover.jpg",
        gallery=[{"idx": 1, "ts": 0.0, "path": "/tmp/lib/gallery/f001.jpg"}])


def _result(title, partial=False):
    r = BreakdownResult(title=title, duration=12.0, shot_count=3,
                        overall=OverallAnalysis(hook_desc="钩子", hook_score="8"))
    for s in STAGES:
        r.mark(s, ok=True)
    if partial:
        r.mark(STAGES[0], ok=False, reason="解析失败")
    return r


def test_save_and_list_and_load_roundtrip():
    r = _ok_result()
    tid = breakdown_store.save(r, gallery_dir="/tmp/lib")
    assert tid and r.task_id == tid

    rows = breakdown_store.list_tasks()
    assert rows[0]["id"] == tid
    assert rows[0]["title"] == "骨胶原爆款"
    assert rows[0]["status"] == "ok"
    assert "payload" not in rows[0]               # 列表行不含 payload（轻量）

    r2 = breakdown_store.load_result(tid)
    assert isinstance(r2, BreakdownResult)
    assert r2.title == "骨胶原爆款"
    assert r2.task_id == tid
    assert r2.report_path == "/tmp/v.docx"
    assert r2.frame_analyses[1].shot_size == "中景"
    assert r2.segments[1].visual_prompt == "画面2"
    assert r2.overall.formula == "痛点+方案"
    assert r2.transcript[0].text == "家人们"
    assert r2.gallery == [{"idx": 1, "ts": 0.0, "path": "/tmp/lib/gallery/f001.jpg"}]


def test_partial_result_marked():
    r = BreakdownResult(title="半成品", video_path="/tmp/x.mp4")   # 未解析成功
    tid = breakdown_store.save(r)
    row = breakdown_store.get(tid)
    assert row["status"] == "partial"
    loaded = breakdown_store.load_result(tid)
    assert loaded.is_partial()


def test_title_falls_back_to_video_stem():
    r = BreakdownResult(video_path="/tmp/我的视频.mp4", duration=3,
                        stage_status={STAGE_ACQUIRE: "ok"})
    tid = breakdown_store.save(r)
    assert breakdown_store.get(tid)["title"] == "我的视频"


def test_delete_removes_row():
    tid = breakdown_store.save(_ok_result())
    assert breakdown_store.get(tid) is not None
    breakdown_store.delete(tid)
    assert breakdown_store.get(tid) is None
    assert breakdown_store.load_result(tid) is None


def test_list_order_newest_first():
    a = breakdown_store.save(_ok_result())
    b = breakdown_store.save(_ok_result())
    ids = [row["id"] for row in breakdown_store.list_tasks()]
    assert ids[0] == b and a in ids


def test_only_ok_filters_partial():
    ok = _result("完整任务")
    bad = _result("半成品任务", partial=True)
    breakdown_store.save(ok)
    breakdown_store.save(bad)
    assert ok.task_id and bad.task_id

    ids_all = {r["id"] for r in breakdown_store.list_tasks()}
    ids_ok = {r["id"] for r in breakdown_store.list_tasks(only_ok=True)}
    # 全部列表里两条都在；only_ok 只留完整成功的
    assert ok.task_id in ids_all and bad.task_id in ids_all
    assert ok.task_id in ids_ok and bad.task_id not in ids_ok


def test_product_association_roundtrip():
    r = _result("关联产品任务")
    breakdown_store.save(r)
    p1 = product_store.add_product("产品甲-测试")
    p2 = product_store.add_product("产品乙-测试")
    breakdown_store.set_products(r.task_id, [p1, p2])
    assert set(breakdown_store.products_of(r.task_id)) == {p1, p2}

    m = breakdown_store.products_map([r.task_id])
    assert sorted(m[r.task_id]) == ["产品乙-测试", "产品甲-测试"]
    assert set(breakdown_store.task_ids_for_product(p1)) == {r.task_id}

    # 重复设置幂等，传空即清空
    breakdown_store.set_products(r.task_id, [p1, p1, p2])
    assert set(breakdown_store.products_of(r.task_id)) == {p1, p2}
    breakdown_store.set_products(r.task_id, [])
    assert breakdown_store.products_of(r.task_id) == []


def test_delete_cleans_association():
    r = _result("删除联动任务")
    breakdown_store.save(r)
    p = product_store.add_product("产品丙-测试")
    breakdown_store.set_products(r.task_id, [p])
    breakdown_store.delete(r.task_id)
    assert breakdown_store.products_of(r.task_id) == []
    assert breakdown_store.get(r.task_id) is None
