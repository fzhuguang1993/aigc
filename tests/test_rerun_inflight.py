"""
tests/test_rerun_inflight.py —— 「重跑时旧执行还在跑」回归测试

钉住同事反馈的现场：任务跑到一半又点「▶ 执行选中」，旧实现会怎样——
1. 给它弹「已经执行成功过」的错文案（它根本没成功过，正在跑）；
2. 确认后不取消那份在跑的，直接再提交一份：新旧两份同时占并发额度，
   还各下载一个成品；
3. 单条时甚至再给一个「抽卡次数」，一次把 1 份变成 4 份。

现在的口径：先用 `inflight_execs()` 查出还有执行在跑的任务，交给同一个弹窗
问「⏹ 取消并重跑 / 🔁 直接重跑 / 跳过」，取消动作排在提交之前。

不依赖真实网络，也不起线程：取消用 monkeypatch 替掉 `cancel_one`。
"""
import pytest

import gui.pages_tasks as pt
from registry.manager import REG


def _at(job_id, tid, status="running"):
    """REG.get_all_by_row 返回的那种执行记录（只填用到的字段）"""
    return {"job_id": job_id, "row_idx": tid, "account": "acc1",
            "status": status, "progress": 40}


# ====================================================================
# 1. 谁算「还在跑」
# ====================================================================
def test_only_live_executions_count_as_inflight():
    REG.add("j-run", 1, "acc1", "p", "品名")
    REG.update("j-run", "running", 40)
    REG.add("j-queue", 2, "acc1", "p", "品名")        # 排队中：也占额度
    REG.add("j-done", 3, "acc1", "p", "品名")
    REG.update("j-done", "completed", 100)            # 跑完了：没有可取消的东西
    REG.add("j-cancel", 4, "acc1", "p", "品名")
    REG.update("j-cancel", "cancelled", 100)

    got = pt.inflight_execs([1, 2, 3, 4, 5])
    assert set(got) == {1, 2}, got
    assert [a["job_id"] for a in got[1]] == ["j-run"]
    # 抽卡：同一任务多个执行要一次全收，不能只取消第一个
    REG.add("j-run2", 1, "acc1", "p", "品名")
    REG.update("j-run2", "queued", 0)
    assert len(pt.inflight_execs([1])[1]) == 2


def test_cancelling_is_still_counted_until_cloud_confirms():
    """已发取消请求 ≠ 额度已释放：云端确认前仍算占着线路"""
    REG.add("j-c", 7, "acc1", "p", "品名")
    REG.update("j-c", "cancelling", 60)
    assert 7 in pt.inflight_execs([7])


# ====================================================================
# 2. 弹窗答案 → 提交清单 + 待取消清单
# ====================================================================
ITEMS = [(1, "品名A", "提示词1")]
FORCE = [(2, "品名B", "提示词2")]
INFLIGHT = {3: [_at("j-old", 3)]}


def test_skip_drops_both_force_and_running():
    """点「跳过」：要重跑的、正在跑的都不许提交——否则“跳过”了还多出一个成品"""
    items, cancel = pt.decide_rerun(ITEMS, FORCE, INFLIGHT, None)
    assert items == ITEMS and cancel == []


def test_rerun_without_cancel_keeps_old_job_running():
    """「🔁 直接重跑」是显式选择：旧执行保留，不发取消请求"""
    choice = {"repeat": 2, "cancel_first": False}
    items, cancel = pt.decide_rerun(ITEMS, FORCE, INFLIGHT, choice)
    assert [i[0] for i in items] == [1, 2, 2] and cancel == []


def test_cancel_first_queues_every_live_execution():
    """「⏹ 取消并重跑」：先取消在跑的，再提交（含强制重跑与抽卡复制）"""
    choice = {"repeat": 1, "cancel_first": True}
    items, cancel = pt.decide_rerun(ITEMS, FORCE, INFLIGHT, choice)
    assert [i[0] for i in items] == [1, 2]
    assert [a["job_id"] for a in cancel] == ["j-old"]


def test_nothing_inflight_behaves_as_before():
    """纯新任务 + 没在跑：跟改动前一样，不多加一条也不发取消请求"""
    items, cancel = pt.decide_rerun(ITEMS, [], {}, {"repeat": 1, "cancel_first": True})
    assert items == ITEMS and cancel == []


# ====================================================================
# 3. 取消动作本身
# ====================================================================
def test_cancel_reports_partial_failure_without_raising(monkeypatch):
    """取消失败不能把整批提交带崩：继续提交，但要在播报里给出失败数"""
    calls = []

    def fake_cancel(at):
        calls.append(at["job_id"])
        return at["job_id"] != "j-bad"

    monkeypatch.setattr(pt, "cancel_one", fake_cancel)
    spoken = []
    ok, fail = pt.cancel_inflight([_at("j-ok", 1), _at("j-bad", 2)], spoken.append)
    assert (ok, fail) == (1, 1)
    assert calls == ["j-ok", "j-bad"]
    assert "1/2" in spoken[-1] and "云端释放额度" in spoken[-1]


def test_cancel_nothing_stays_silent(monkeypatch):
    """没有在跑的执行时一个字都不播报（正常提交不该多出⏹ 噪音）"""
    monkeypatch.setattr(pt, "cancel_one",
                        lambda at: pytest.fail("没有待取消的执行却调了取消"))
    spoken = []
    assert pt.cancel_inflight([], spoken.append) == (0, 0)
    assert spoken == []
