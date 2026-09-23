"""
tests/test_health_check.py —— 「线路绿灯必须来自真实检测」回归测试

钉住内测现场的投诉：刚打开软件侧栏一排绿灯，其实一条都没测过——
`AccountState.healthy` 默认 True，而监控线程旧实现是「先睡 30 秒再首检」，
于是那 30 秒里显示的全是缓存的默认值。

约定（改之前先读完）：
- 默认 True 只为「刚开机也能提交」，展示层必须先问 `first_check_done()`；
- 一次网络抖动不许降灯：一轮内重试一次，两轮累计到阈值才判故障（防误杀）；
- 检测恢复后必须重新亮绿灯并清零 fail_count（否则线路再也回不来）；
- 探活拿到任何 HTTP 响应（含 404）就是「服务活着」，不许当线路故障。

所有 HTTP 通过 monkeypatch 注入，无网络、无真实服务。
"""
import inspect

import pytest

import registry.manager as mgr
from core.api_client import ApiError


class FakeAcc:
    def __init__(self, name):
        self.name = name
        self.base = f"http://{name}.test/api/v1"
        self.concurrency = 1
        self.healthy = True          # 与 AccountState 同构：默认「未测即当可用」
        self.fail_count = 0
        self.probe_state = None


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


# ====================================================================
# 4. 探活归类：服务有话回就不算线路故障
# ====================================================================
@pytest.mark.parametrize("code, expect", [
    (None, "down"),        # 连不上（拒连/超时/DNS）：真故障
    (502, "down"),         # 网关报错：后端已经挂了，提交上去也是白提
    (500, "down"),
    (404, "alive"),        # 云端没实现 GET /health（实测回 HTML「页面未找到」）
    (405, "alive"),
    (401, "alive"),        # 要鉴权：机器是活的
])
def test_classify_probe_by_status_code(code, expect):
    assert mgr.classify_probe(ApiError("boom", code)) == expect


def test_probe_404_does_not_turn_the_line_red(lines, monkeypatch):
    """旧实现把所有异常一律计失败：云端没实现 /health 时，30 秒×3 轮
    就能把全部线路标红，接着选线退化成“只留一条”（同事反馈的现场）"""
    def fake_health(base):
        if base.startswith("http://acc2."):
            raise ApiError("HTTP 404: <!doctype html>\n页面未找到", 404)
        return {"status": "ok"}

    monkeypatch.setattr(mgr, "health", fake_health)
    for _ in range(5):                      # 远超阈值（3）轮
        mgr.check_all_accounts()
    assert lines["acc2"].healthy is True     # 不降灯，仍可被选中去提交
    assert lines["acc2"].fail_count == 0
    assert lines["acc2"].probe_state == "alive"   # 但不谎称“测过了”
    assert lines["acc1"].probe_state == "ok"


def test_probe_5xx_still_flips_the_line_red(lines, monkeypatch):
    def fake_health(base):
        raise ApiError("HTTP 502: bad gateway", 502)

    monkeypatch.setattr(mgr, "health", fake_health)
    for _ in range(3):
        mgr.check_all_accounts()
    assert lines["acc1"].healthy is False
    assert lines["acc1"].probe_state == "down"
