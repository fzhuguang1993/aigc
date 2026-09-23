"""
tests/test_output_mark.py —— 审片标记（改名 + 落库 + 一键清理）回归测试

锁死四条：
1. 标「不可用」＝文件名加记号 **且** 库里路径同步改到新名字
   （只做一半就会出现：列表那一格还指着老名字，双击报「文件不存在」）；
2. 标记挂在成品的当前路径上，改名要跟键、再点一次要能取消；
3. 「清理」只移进回收站 + 清标记，**不删任务与执行记录**
   （那次执行确实发生过，成功率与平均生成时长不该因事后删片而变）；
4. 移不进回收站的文件绝不降级成真删，标记也保留着下次再清。
"""
from pathlib import Path

import pytest

from processors import output_mark
from store import db, task_store

NAME = "001_诺特兰德益生菌_0923_01_雷亮"


@pytest.fixture()
def made(tmp_path):
    """造「一条已完成任务 + 它的成品文件」，返回 (任务ID, 成品路径)"""
    def _make(name=NAME, num="1"):
        f = tmp_path / "0923" / f"{name}.mp4"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"0123456789")
        tid = task_store.add_task(num, "诺特兰德益生菌", f"提示词::{name}")
        task_store.update_row(tid, **{"输出": str(f)})
        job = f"job::{name}"
        task_store.record_run_start(tid, num, "诺特兰德益生菌", "acc1", job)
        task_store.record_run_end(job, "completed", output=str(f))
        return tid, f
    return _make


def _outputs(tid):
    return task_store.get_task(tid)["output"]


# ====================================================================
# 标不可用：文件名 + 库里路径一起改
# ====================================================================
def test_mark_bad_renames_and_syncs_db(made):
    tid, f = made()
    ok, new, msg = output_mark.set_mark(str(f), task_store.MARK_BAD)
    assert ok and msg
    assert new.endswith(f"_{output_mark.BAD}.mp4")
    assert not f.exists(), "老名字该没了"
    assert Path(new).is_file()
    assert _outputs(tid) == new, "任务表没跟着改，双击就会 404"
    assert task_store.get_file_mark(new) == task_store.MARK_BAD
    assert task_store.get_file_mark(str(f)) == "", "老路径上不该留孤儿标记"
    runs = db.query("SELECT output FROM runs WHERE task_id=?", (tid,))
    assert runs[0]["output"] == new, "执行记录里的输出路径也要跟过去"


def test_mark_bad_then_unmark_restores_name(made):
    tid, f = made()
    _, new, _ = output_mark.set_mark(str(f), task_store.MARK_BAD)
    ok, back, _ = output_mark.set_mark(new, "")
    assert ok
    assert back == str(f), "取消标记要把文件名改回去，否则清理完看着别扭"
    assert _outputs(tid) == str(f)
    assert task_store.get_file_mark(str(f)) == ""


def test_mark_ok_clears_bad_suffix_but_keeps_record(made):
    _, f = made()
    _, bad, _ = output_mark.set_mark(str(f), task_store.MARK_BAD)
    ok, clean, _ = output_mark.set_mark(bad, task_store.MARK_OK)
    assert ok and clean == str(f)
    assert task_store.get_file_mark(str(f)) == task_store.MARK_OK
    assert output_mark.mark_of(str(f)) == task_store.MARK_OK


def test_mark_is_idempotent(made):
    """已经标过的再点一次「不可用」：不该叠出 `_不可用_不可用`"""
    _, f = made()
    _, once, _ = output_mark.set_mark(str(f), task_store.MARK_BAD)
    ok, twice, _ = output_mark.set_mark(once, task_store.MARK_BAD)
    assert ok and twice == once
    assert twice.count(output_mark.BAD) == 1


def test_marking_missing_file_fails_loudly(tmp_path):
    ok, path, msg = output_mark.set_mark(str(tmp_path / "没有这个文件.mp4"),
                                         task_store.MARK_BAD)
    assert not ok and "不存在" in msg


def test_mark_empty_path_fails_loudly():
    ok, _, msg = output_mark.set_mark("", task_store.MARK_BAD)
    assert not ok and msg


# ====================================================================
# 列表派生的「审核」列 + 同事手改文件名也认
# ====================================================================
def test_list_df_exposes_audit_label(made):
    tid, f = made()
    df = task_store.list_tasks_df()
    assert df.loc[df["_id"] == tid, "审核"].iloc[0] == ""
    output_mark.set_mark(str(f), task_store.MARK_BAD)
    df = task_store.list_tasks_df()
    assert df.loc[df["_id"] == tid, "审核"].iloc[0] == "不可用"


def test_mark_of_recognizes_manual_rename(made, tmp_path):
    """同事自己在资源管理器里加了记号：库里没记录也要认出来（清理要扫得到）"""
    _, _ = made()
    hand = tmp_path / "0923" / f"009_别人的成品_{output_mark.BAD}.mp4"
    hand.write_bytes(b"x")
    assert task_store.get_file_mark(str(hand)) == ""
    assert output_mark.mark_of(str(hand)) == task_store.MARK_BAD


def test_multi_output_only_replaces_the_matching_segment(made, tmp_path):
    """抽卡两条：标其中一条，另一条的路径不许被顺带改坏

    `..._01_雷亮` 正好是 `..._01_雷亮2` 的字符串前缀，整串 replace 会一起改掉。"""
    other = tmp_path / "0923" / f"{NAME}2.mp4"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"x")
    tid, f = made()
    both = f"{f}; {other}"
    task_store.update_row(tid, **{"输出": both})
    db.execute("UPDATE runs SET output=? WHERE task_id=?", (both, tid))

    _, new, _ = output_mark.set_mark(str(f), task_store.MARK_BAD)
    assert _outputs(tid).split("; ") == [new, str(other)], "只该换被标记的那一段"
    run_out = db.query("SELECT output FROM runs WHERE task_id=?", (tid,))[0]["output"]
    assert run_out.split("; ") == [new, str(other)]


# ====================================================================
# 收集与清理
# ====================================================================
def test_collect_bad_unions_db_and_disk(made, tmp_path):
    _, f = made()
    output_mark.set_mark(str(f), task_store.MARK_BAD)
    bad_new = tmp_path / "0923" / f"002_{output_mark.BAD}.mp4"      # 只改了名没入库
    bad_new.write_bytes(b"abc")
    root = tmp_path / "0923"
    got = {item["path"]: item for item in output_mark.collect_bad(root)}
    assert set(got) == {str(f.parent / "001_诺特兰德益生菌_0923_01_雷亮_不可用.mp4"),
                        str(bad_new)}
    assert all(i["exists"] and i["size"] > 0 for i in got.values())


def test_collect_bad_keeps_dead_marks(made, tmp_path):
    """库里有标记但文件早被删了：要还能列出来（顺手清孤儿标记），不能当不存在"""
    _, f = made()
    _, new, _ = output_mark.set_mark(str(f), task_store.MARK_BAD)
    Path(new).unlink()
    got = output_mark.collect_bad(root=tmp_path / "0923")
    assert [i["path"] for i in got] == [new]
    assert got[0]["exists"] is False and got[0]["size"] == 0


@pytest.fixture()
def fake_trash(monkeypatch):
    """替掉真回收站：返回 (假删除列表, 失败列表) 供各用例设定"""
    box = {"deleted": [], "fail": set()}

    def _move(paths):
        paths = list(paths)
        ok = [p for p in paths if p not in box["fail"]]
        bad = [p for p in paths if p in box["fail"]]
        box["deleted"].extend(ok)
        return ok, bad

    monkeypatch.setattr(output_mark, "move_to_trash", _move)
    return box


def test_cleanup_moves_only_bad_and_keeps_records(made, fake_trash, tmp_path):
    tid, f = made()
    keep = tmp_path / "0923" / "002_诺特兰德益生菌_0923_02_雷亮.mp4"
    keep.write_bytes(b"yy")                      # 没标记的好片
    _, new, _ = output_mark.set_mark(str(f), task_store.MARK_BAD)

    stat = output_mark.cleanup()
    assert stat["trashed"] == [new] and stat["failed"] == []
    assert stat["freed"] > 0, "释放的体积要在删之前取，事后 stat 不到"
    assert fake_trash["deleted"] == [new]
    assert keep.exists(), "没标记的成品一个都不许碰"
    assert task_store.get_file_mark(new) == "", "文件清掉了，标记也要跟着清"
    assert task_store.get_task(tid) is not None, "任务不许被删"
    assert db.query("SELECT id FROM runs WHERE task_id=?", (tid,)), "执行记录不许被删"


def test_cleanup_skips_failures_and_keeps_their_marks(made, fake_trash):
    """移不进回收站（文件被别的软件占用）：标记保留、报告里写清楚，绝不静默真删"""
    _, f = made()
    _, new, _ = output_mark.set_mark(str(f), task_store.MARK_BAD)
    fake_trash["fail"].add(new)
    stat = output_mark.cleanup()
    assert stat["trashed"] == [] and stat["failed"] == [new]
    assert task_store.get_file_mark(new) == task_store.MARK_BAD, "没删掉就不该清标记"
    assert stat["freed"] == 0


def test_cleanup_accepts_explicit_list(made, fake_trash):
    _, f = made()
    _, new, _ = output_mark.set_mark(str(f), task_store.MARK_BAD)
    stat = output_mark.cleanup([new])
    assert stat["trashed"] == [new]
    assert output_mark.cleanup([])["trashed"] == [], "空列表什么都不该删"


def test_cleanup_reports_missing_as_gone(made, fake_trash, tmp_path):
    """标记还在、文件已经不在的：算已清理（顺手把孤儿标记删掉），不算失败"""
    _, f = made(name="003_孤儿")
    _, new, _ = output_mark.set_mark(str(f), task_store.MARK_BAD)
    Path(new).unlink()
    stat = output_mark.cleanup()
    assert stat["trashed"] == [] and stat["failed"] == []
    assert stat["missing"] == [new]
    assert task_store.get_file_mark(new) == ""
