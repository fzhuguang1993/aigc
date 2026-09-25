"""
tests/test_ui_signature.py —— 任务表脏检测指纹（ui_signature + write_revision）

指纹必须对「会改变任务列表显示的写入」都变：漏一次，界面就可能停在旧数据上
（任务中心的 2 秒定时刷新靠它决定要不要重建表格）。重点覆盖两类：
- 聚合值覆盖的常规写入（新增任务 / 执行落库 / 审片标记）
- 时间戳签名看不见的写入（同一秒内连续写、不带 updated_at 的更新）→ 由
  本连接写计数（total_changes）兜底
"""
from store import task_store


def _sig():
    return task_store.ui_signature()


def test_signature_stable_without_write():
    """没有写入时指纹必须稳定（否则每 2 秒都会白白重建表格）"""
    assert _sig() == _sig()


def test_add_task_changes_signature():
    before = _sig()
    task_store.add_task("1", "品A", "提示词")
    assert _sig() != before


def test_update_row_changes_signature():
    tid = task_store.add_task("1", "品A", "提示词")
    before = _sig()
    task_store.update_row(tid, **{"备注": "随手标一笔"})
    assert _sig() != before


def test_prompt_zh_changes_signature():
    """中文对照写回刻意不带 updated_at：必须靠本连接写计数感知"""
    tid = task_store.add_task("1", "品A", "提示词")
    before = _sig()
    task_store.set_prompt_zh(tid, "译文")
    assert _sig() != before


def test_run_lifecycle_changes_signature():
    tid = task_store.add_task("1", "品A", "提示词")
    before = _sig()
    task_store.record_run_start(tid, "1", "品A", "线1", "job-1")
    assert _sig() != before
    before = _sig()
    task_store.record_run_end(
        "job-1", "completed", output="x.mp4",
        cloud={"submitted_at": "2026-09-25T01:00:00+00:00",
               "started_at": "2026-09-25T01:00:05+00:00",
               "completed_at": "2026-09-25T01:05:00+00:00"})
    assert _sig() != before


def test_file_mark_changes_signature():
    before = _sig()
    task_store.set_file_mark("C:/x/a.mp4", "ok")
    assert _sig() != before


def test_same_second_updates_all_seen():
    """同一秒内连续两次写入都不能漏（时间戳类签名只能分辨到秒）"""
    tid = task_store.add_task("1", "品A", "提示词")
    task_store.update_row(tid, **{"备注": "第一次"})
    s1 = _sig()
    task_store.update_row(tid, **{"备注": "第二次"})
    assert _sig() != s1
