"""
tests/test_purge_unapproved.py —— 一键清理未批准：只留「可用」，其余连文件带任务删

覆盖 task_store.purge_unapproved_plan / purge_unapproved：
- 只纳入磁盘真有成品文件的任务（没出片的在跑/失败任务不动）；
- 未标「可用」的成品（没标记 / 标了不可用）待删，标了可用的保留；
- 任务清完没有任何可用成品 → 整条删；否则保任务、把删掉的路径从输出剪掉；
- 全部可用时无事可做（幂等）。
系统回收站在测试里换成真删，不碰真实回收站。
"""
from pathlib import Path

import pytest

import utils.desktop_utils as du
from store import db, task_store


@pytest.fixture
def fake_trash(monkeypatch):
    """把 move_to_trash 换成直接删除：测试既不碰系统回收站，又能验证搬运结果"""
    def _trash(paths):
        ok, failed = [], []
        for p in paths:
            try:
                Path(p).unlink()
                ok.append(str(p))
            except OSError as e:
                failed.append((str(p), str(e)))
        return ok, failed
    monkeypatch.setattr(du, "move_to_trash", _trash)
    return _trash


def _mk(date_dir, name, product, tag="", mark=None):
    """造一条已出片的任务：写文件 + 建任务 + 记输出路径 +（可选）打标记"""
    f = date_dir / name
    f.write_text("v", encoding="utf-8")
    tid = task_store.add_task("1", product, f"提示词_{name}")
    if tag:
        task_store.update_row(tid, **{"标签": tag})
    db.execute("UPDATE tasks SET output=? WHERE id=?", (str(f), tid))
    if mark:
        task_store.set_file_mark(str(f), mark)
    return tid, f


def test_plan_only_lists_unapproved_existing(tmp_path, fake_trash):
    d = tmp_path / "outputs" / "0924"
    d.mkdir(parents=True)
    _mk(d, "ok.mp4", "维B", mark=task_store.MARK_OK)             # 可用：不进清单
    _mk(d, "unmarked.mp4", "维B")                                # 没标：待删
    _mk(d, "bad.mp4", "维B", mark=task_store.MARK_BAD)          # 不可用：待删
    ghost = task_store.add_task("1", "维B", "提示词_ghost")      # 没出片：不动
    db.execute("UPDATE tasks SET output=? WHERE id=?", (str(d / "gone.mp4"), ghost))

    plan = task_store.purge_unapproved_plan()
    drops = {Path(p).name for it in plan for p in it["drop"]}
    assert drops == {"unmarked.mp4", "bad.mp4"}
    assert ghost not in {it["id"] for it in plan}
    assert all(it["drop_task"] for it in plan)                   # 各自无可用残留


def test_purge_deletes_files_and_task_when_no_approved_left(tmp_path, fake_trash):
    d = tmp_path / "outputs" / "0925"
    d.mkdir(parents=True)
    tid, f = _mk(d, "junk.mp4", "维B")
    st = task_store.purge_unapproved()
    assert st["tasks_deleted"] == [tid]
    assert not f.exists()
    assert task_store.get_task(tid) is None                      # 任务行已删
    assert task_store.get_file_mark(str(f)) == ""                # 无孤儿标记


def test_purge_keeps_task_and_prunes_output(tmp_path, fake_trash):
    """一个任务两条抽卡：1 可用 1 未标 → 只删未标的，任务保留、输出剪成只剩可用"""
    d = tmp_path / "outputs" / "0926"
    d.mkdir(parents=True)
    keep = d / "keep.mp4"
    keep.write_text("v", encoding="utf-8")
    drop = d / "drop.mp4"
    drop.write_text("v", encoding="utf-8")
    task_store.set_file_mark(str(keep), task_store.MARK_OK)
    tid = task_store.add_task("1", "维B", "提示词_multi")
    db.execute("UPDATE tasks SET output=? WHERE id=?", (f"{keep}; {drop}", tid))

    plan = task_store.purge_unapproved_plan()
    assert [it["drop_task"] for it in plan] == [False]           # 有可用残留：保任务
    st = task_store.purge_unapproved(plan)
    assert st["tasks_deleted"] == [] and st["tasks_kept"] == 1
    assert not drop.exists() and keep.exists()
    assert task_store.get_task(tid)["output"] == str(keep)       # 输出剪成只剩可用
    assert task_store.get_task(tid) is not None


def test_purge_all_approved_is_noop(tmp_path, fake_trash):
    d = tmp_path / "outputs" / "0927"
    d.mkdir(parents=True)
    _mk(d, "a.mp4", "维B", mark=task_store.MARK_OK)
    assert task_store.purge_unapproved_plan() == []              # 全可用：无事可做
