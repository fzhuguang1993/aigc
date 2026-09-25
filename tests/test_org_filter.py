"""
tests/test_org_filter.py —— 数据权限接入 task_store 查询的端到端口径

造数口径：A(小张) / B(小李) 两条线路各有 runs+tasks，C 无主，
另有一条空线路任务（员工新建没选线路＝公共工作台，keep_blank 放行）。
成员/会话/组织表的清理由 conftest autouse 兜底。
"""
import itertools
from datetime import date, datetime

import pytest

from store import db, org_store, task_store as ts

TODAY = date.today().isoformat()
HM = datetime.now().strftime("%H:%M")     # 钉在当前小时：hourly 只补到此刻
_seq = itertools.count(1)


@pytest.fixture()
def seeded():
    """两套数据 + 三种角色会话可用的世界"""
    org_store.create_member("老板", "1234")                        # 首个强制 admin
    org_store.create_member("小张", "1234", "member", "一部")
    org_store.create_member("小李", "1234", "member", "一部")
    org_store.create_member("主管", "1234", "manager", "一部")
    org_store.create_member("甲木", "1234")                        # 名下无线路
    assert org_store.set_line_owner("A线", "小张") is None
    assert org_store.set_line_owner("B线", "小李") is None
    _run("A线", "completed", "甲品")
    _run("A线", "failed", "甲品")
    _run("B线", "completed", "乙品")
    _run("C线", "completed", "丙品")               # 无主线路：只有 admin 见
    _task("A线", "甲品")
    _task("B线", "乙品")
    _task("C线", "丙品")
    _task("", "通用品")                            # 空线路任务全员可见


def _run(account, status, product):
    n = next(_seq)
    db.execute(
        "INSERT INTO runs(num,product,account,job_id,status,started_at,"
        " finished_at,duration) VALUES('1',?,?,?,?,?,?,60)",
        (product, account, f"jf{n}", status,
         f"{TODAY} {HM}:00", f"{TODAY} {HM}:10"))


def _task(account, product):
    tid = ts.add_task("1", product, f"提示词_{product}_{account}_{next(_seq)}")
    if account:
        db.execute("UPDATE tasks SET account=? WHERE id=?", (account, tid))
    return tid


def _login(name):
    assert org_store.login(name, "1234") is None


# ---------------- 不限制路径（旧行为原样） ----------------

def test_no_session_matches_legacy(seeded):
    """组织启用但本进程没登录（console 等）：与旧行为一致，全量"""
    assert org_store.current() is None
    assert ts.range_stats(7)["cur"]["total"] == 4
    assert len(ts.list_runs()) == 4
    assert ts.task_count() == 4


def test_admin_sees_everything(seeded):
    _login("老板")
    st = ts.range_stats(7)
    assert st["cur"]["total"] == 4 and st["cur"]["ok"] == 3
    assert {a["account"] for a in st["accounts"]} == {"A线", "B线", "C线"}
    assert {r["account"] for r in ts.runs_drill("all", None)} == {"A线", "B线", "C线"}
    assert len(ts.list_runs()) == 4


# ---------------- member：只看自己名下线路 ----------------

def test_member_only_own_line(seeded):
    _login("小张")
    st = ts.range_stats(7)
    assert st["cur"]["total"] == 2 and st["cur"]["ok"] == 1
    assert [a["account"] for a in st["accounts"]] == ["A线"]
    assert [r["account"] for r in ts.list_runs()] == ["A线", "A线"]
    # 失败原因也只算自己的：A线那条 failed（error 空 → 未记录原因）
    assert ts.extra_stats(7)["errors"] == [{"reason": "(未记录原因)", "count": 1}]
    # tasks 用 keep_blank：自己线路 + 空线路公共任务，别人的切掉
    assert ts.task_count() == 2
    df = ts.list_tasks_df()
    assert set(df["品名"]) == {"甲品", "通用品"}
    ch = ts.filter_choices()
    assert set(ch["products"]) == {"甲品", "通用品"}, \
        "别人线路上才有的品名不能出现在筛选下拉里"


def test_member_hourly_and_daily_filtered(seeded):
    """逐日/逐时也同口径——补零的日历轴不能漏放别人的量"""
    from datetime import datetime
    _login("小张")
    st = ts.range_stats(7)
    assert sum(d["total"] for d in st["daily"]) == 2
    assert sum(h["total"] for h in st["hourly"]) == 2
    assert st["hourly"][datetime.now().hour]["total"] == 2


# ---------------- manager：部门并集 ----------------

def test_manager_union_of_dept(seeded):
    _login("主管")
    assert org_store.visible_accounts() == ["A线", "B线"]
    assert ts.range_stats(7)["cur"]["total"] == 3          # A×2 + B×1，无主 C 不给
    agg = {r["k"] for r in ts.custom_agg("account", "total")}
    assert agg == {"A线", "B线"}


# ---------------- 空集：什么都看不到，但公共任务保留 ----------------

def test_empty_allow_blindfold(seeded):
    _login("甲木")
    assert org_store.visible_accounts() == []
    assert ts.range_stats(7)["cur"]["total"] == 0
    assert ts.range_stats(7)["running"] == 0
    assert ts.list_runs() == []
    assert ts.runs_drill("all", None) == []
    assert ts.custom_agg("product", "total") == []
    assert ts.extra_stats(7)["errors"] == []
    assert ts.report_stats()["total"] == 0
    # tasks 的 AND 0 也保留空账号行：员工新建未跑的任务不能被切没
    assert ts.task_count() == 1
    assert list(ts.list_tasks_df()["品名"]) == ["通用品"]


# ---------------- 下钻 / 汇报同步受控 ----------------

def test_drill_respects_scope(seeded):
    _login("主管")
    rows = ts.runs_drill("account", "B线")
    assert rows and all(r["account"] == "B线" for r in rows)
    _login("小张")
    assert ts.runs_drill("account", "B线") == [], "点别人线路下钻不能给明细"
    assert all(r["account"] == "A线" for r in ts.runs_drill("day", TODAY))
    assert all(r["account"] == "A线" for r in ts.runs_drill("status", "成功"))


def test_report_and_daily_filtered(seeded):
    _login("小张")
    st = ts.report_stats()
    assert st["total"] == 2 and st["ok"] == 1
    assert [p for p, _n in st["products"]] == ["甲品"]
    daily = ts.daily_report_stats(now_hm="23:59")
    assert daily["total"] == 2 and daily["ok"] == 1
    # 主管视角同一函数值就不同：权限真的在会话层面切换
    _login("主管")
    assert ts.report_stats()["total"] == 3
