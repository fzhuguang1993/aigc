"""
tests/test_demo_seed.py —— 看板演示数据的播种/清除边界

关注点：
  · 只在「库里没有任何真实任务」时才播种，绝不污染开发/老用户机；
  · 播过（已有 demo=1）不重复播；
  · 清除只删 demo=1 的 tasks 及其名下 runs，真实任务（demo=0）永不触碰。
库由 conftest 每例前后清空 tasks/runs，用例互不干扰。
"""
from store import db, demo_seed


def _real_task(num="R1"):
    return db.execute(
        "INSERT INTO tasks(num, product, status, demo) VALUES(?,?,?,0)",
        (num, "真实产品", "completed"))


def _count(demo):
    return db.query(
        "SELECT COUNT(*) c FROM tasks WHERE COALESCE(demo,0)=?", (demo,))[0]["c"]


def test_seed_when_empty():
    assert demo_seed.seed_demo(count=10) == 10
    assert demo_seed.demo_present()
    assert _count(1) == 10
    assert _count(0) == 0


def test_seed_skips_when_real_tasks_exist():
    _real_task()
    assert demo_seed.seed_demo(count=10) == 0
    assert not demo_seed.demo_present()
    assert _count(0) == 1


def test_seed_is_idempotent():
    demo_seed.seed_demo(count=5)
    assert demo_seed.seed_demo(count=5) == 0
    assert _count(1) == 5


def test_clear_removes_only_demo_keeps_real():
    demo_seed.seed_demo(count=8)
    _real_task("KEEP")
    n = demo_seed.clear_demo()
    assert n == 8
    assert not demo_seed.demo_present()
    nums = sorted(r["num"] for r in db.query("SELECT num FROM tasks"))
    assert nums == ["KEEP"]


def test_clear_also_removes_demo_runs():
    demo_seed.seed_demo(count=6)
    assert db.query("SELECT COUNT(*) c FROM runs")[0]["c"] > 0
    demo_seed.clear_demo()
    assert db.query("SELECT COUNT(*) c FROM runs")[0]["c"] == 0


def test_clear_is_noop_when_no_demo():
    _real_task()
    assert demo_seed.clear_demo() == 0
    assert _count(0) == 1
