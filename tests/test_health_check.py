"""
tests/test_health_check.py —— 「线路绿灯必须来自真实检测」回归测试

钉住内测现场的投诉：刚打开软件侧栏一排绿灯，其实一条都没测过——
`AccountState.healthy` 默认 True，而监控线程旧实现是「先睡 30 秒再首检」，
于是那 30 秒里显示的全是缓存的默认值。

约定（改之前先读完）：
- 默认 True 只为「刚开机也能提交」，展示层必须先问 `first_check_done()`；
- 一次网络抖动不许降灯：一轮内重试一次，两轮累计到阈值才判故障（防误杀）；
- 检测恢复后必须重新亮绿灯并清零 fail_count（否则线路再也回不来）。

所有 HTTP 通过 monkeypatch 注入，无网络、无真实服务。
"""
import inspect

import pytest

import registry.manager as mgr


class FakeAcc:
    def __init__(self, name):
        self.name = name
        self.base = f"http://{name}.test/api/v1"
        self.concurrency = 1
        self.healthy = True          # 与 AccountState 同构：默认「未测即当可用」
        self.fail_count = 0


@pytest.fixture()
def lines(monkeypatch):
    accs = [FakeAcc(f"acc{i}") for i in range(1, 4)]
    monkeypatch.setattr(mgr, "ACCOUNTS", accs)
    monkeypatch.setattr(mgr.time, "sleep", lambda s: None)   # 别真等 0.5s 重试
    monkeypatch.setattr(mgr, "raw_info", lambda *a: None)
    monkeypatch.setattr(mgr, "raw_warning", lambda *a: None)
    mgr.FIRST_CHECK_DONE.clear()
    yield {a.name: a for a in accs}
    mgr.FIRST_CHECK_DONE.clear()


def _probe(monkeypatch, ok_names):
    """按名单放行探活：不在名单里的线路一律抛异常"""
    called = []

    def fake_health(base):
        name = base.split("//", 1)[1].split(".", 1)[0]
        called.append(name)
        if name not in ok_names:
            raise RuntimeError("connection refused")
        return {"status": "ok"}

    monkeypatch.setattr(mgr, "health", fake_health)
    return called


# ====================================================================
# 1. 首检时机
# ====================================================================
def test_no_result_claimed_before_first_check(lines, monkeypatch):
    """没测过之前不许宣称健康：展示层靠这个标志显示「检测中」而不是绿灯"""
    assert mgr.first_check_done() is False
    _probe(monkeypatch, set(lines))
    mgr.check_all_accounts()
    assert mgr.first_check_done() is True


def test_first_check_runs_before_the_30s_sleep():
    """回归：旧实现 while 里先 sleep(30) 再检测，首屏永远是没有测过的默认值"""
    src = inspect.getsource(mgr.health_monitor_worker)
    assert src.index("check_all_accounts()") < src.index("time.sleep"), src


def test_every_line_is_probed_each_round(lines, monkeypatch):
    called = _probe(monkeypatch, set(lines))
    mgr.check_all_accounts()
    assert sorted(called) == sorted(lines)


# ====================================================================
# 2. 防误杀：抖动不降灯
# ====================================================================
def test_single_flaky_round_keeps_the_light_green(lines, monkeypatch):
    """一轮内两次尝试全败才计一次失败，离阈值（3）还远，不该立刻降灯"""
    _probe(monkeypatch, set(lines) - {"acc2"})
    mgr.check_all_accounts()
    assert lines["acc2"].fail_count == 1
    assert lines["acc2"].healthy is True


def test_repeated_failures_do_flip_the_light(lines, monkeypatch):
    _probe(monkeypatch, set(lines) - {"acc2"})
    for _ in range(3):
        mgr.check_all_accounts()
    assert lines["acc2"].healthy is False
    assert lines["acc1"].healthy is True         # 好线路不受牵连


def test_recovery_clears_fail_count(lines, monkeypatch):
    """恢复后必须重新亮灯并清零，否则一条线路一旦被标坏就再也回不来"""
    bad = set(lines) - {"acc2"}
    _probe(monkeypatch, bad)
    for _ in range(3):
        mgr.check_all_accounts()
    assert lines["acc2"].healthy is False

    _probe(monkeypatch, set(lines))
    mgr.check_all_accounts()
    assert lines["acc2"].healthy is True
    assert lines["acc2"].fail_count == 0


def test_retry_within_a_round_absorbs_a_momentary_blip(lines, monkeypatch):
    """一轮里的第一次失败不算数：只坏一次的线路本轮就该判为正常"""
    attempts = {"acc2": 0}

    def flaky(base):
        if base.startswith("http://acc2."):
            attempts["acc2"] += 1
            if attempts["acc2"] == 1:
                raise RuntimeError("瞬时抖动")
        return {"status": "ok"}

    monkeypatch.setattr(mgr, "health", flaky)
    mgr.check_all_accounts()
    assert attempts["acc2"] == 2                  # 确实重试过
    assert lines["acc2"].fail_count == 0
    assert lines["acc2"].healthy is True


# ====================================================================
# 3. 未检测不挡路：默认值只影响展示，不影响能不能提交
# ====================================================================
def test_submission_still_works_before_first_check(lines, monkeypatch):
    """首检要几秒，这期间点「执行」不能被告知没有可用线路"""
    monkeypatch.setattr(mgr, "list_jobs", lambda base, limit=100: [])
    assert mgr.first_check_done() is False
    assert mgr.pick_account() in lines.values()
