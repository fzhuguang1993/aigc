"""
tests/test_registry_load.py —— 负载均衡回归测试

钉住四个线上事故场景：
1. 云端排队态（pending/waiting/…）被旧口径漏计 → 所有线路负载恒为 0；
2. 负载全等时 min() 平局恒取配置第一条 → 三台同配置的电脑一起把一条线灌到 20+；
3. concurrency=1 只锁住「提交动作」那几百毫秒 → 从没真正限制过一条线的在途任务；
4. 探活失败到阈值后全部线路标红，候选池塌缩成一条 → 整批只跑一条线（同事反馈）。

所有 HTTP 通过 monkeypatch 注入，无网络、无真实服务。
"""
import pytest

import registry.manager as mgr
from core.api_client import ApiError
from registry.manager import (REG, is_active, measure_load, pick_account,
                              pick_and_wait)


LINES = [f"acc{i}" for i in range(1, 8)]
LINES_SET = set(LINES)


class FakeAcc:
    def __init__(self, name, concurrency=1, healthy=True):
        self.name = name
        self.base = f"http://{name}.test/api/v1"
        self.concurrency = concurrency
        self.healthy = healthy
        self.fail_count = 0
        self.probe_state = None


@pytest.fixture(autouse=True)
def _clean_load_cache():
    """负载缓存、降级标记、全红告警位跨用例残留会让断言变得看运气"""
    mgr.invalidate_load_cache()
    mgr._load_errors.clear()
    mgr._ALL_DOWN_NOTIFIED = False
    yield
    mgr.invalidate_load_cache()
    mgr._load_errors.clear()
    mgr._ALL_DOWN_NOTIFIED = False


@pytest.fixture()
def lines(monkeypatch):
    """7 条线路、每条并发 1 —— 与真实分发配置同构"""
    accs = [FakeAcc(f"acc{i}") for i in range(1, 8)]
    monkeypatch.setattr(mgr, "ACCOUNTS", accs)
    return {a.name: a for a in accs}


def _job(status, job_id="j"):
    return {"job_id": job_id, "status": status}


# ====================================================================
# 1. 状态口径：排除终态，而不是枚举活跃态
# ====================================================================
@pytest.mark.parametrize("st", ["queued", "running", "starting", "pending",
                                "waiting", "created", "processing", "cancelling", ""])
def test_non_terminal_status_still_occupies_the_line(st):
    assert is_active(st)


@pytest.mark.parametrize("st", ["completed", "succeeded", "success", "SUCCESS",
                                "failed", "error", "cancelled", "canceled",
                                "timeout", "expired"])
def test_terminal_status_frees_the_line(st):
    assert not is_active(st)


def test_registry_stops_tracking_only_on_terminal(lines):
    """回归：排队态若叫 pending，旧实现的 active_by_account 会永久失去该任务的
    跟踪（进度卡死、既不下载也不置终态）"""
    REG.add("job-p", 7, "acc1", "提示词", "品名A")
    REG.update("job-p", "pending", 0)
    assert [t["job_id"] for t in REG.active_by_account("acc1")] == ["job-p"]
    assert REG.count_active_by_account("acc1") == 1


# ====================================================================
# 2. 负载读数
# ====================================================================
def test_pending_jobs_are_counted_in_load(lines, monkeypatch):
    items = [_job("pending", "1"), _job("running", "2"),
             _job("completed", "3"), _job("failed", "4")]
    monkeypatch.setattr(mgr, "list_jobs", lambda base, limit=100: items)
    assert measure_load(lines["acc1"]) == (2, "cloud")


def test_load_never_below_what_this_machine_submitted(lines, monkeypatch):
    """云端从提交成功到出现在 /jobs 里有几秒延迟，本机在途必须参与取 max，
    否则批量提交自己就能把一条线灌穿"""
    monkeypatch.setattr(mgr, "list_jobs", lambda base, limit=100: [])
    REG.add("j1", 1, "acc1", "p", "品名")
    REG.add("j2", 2, "acc1", "p", "品名")
    REG.add("j3", 3, "acc2", "p", "品名")
    assert measure_load(lines["acc1"])[0] == 2
    assert measure_load(lines["acc2"])[0] == 1


def test_degraded_view_is_announced_once(lines, monkeypatch):
    """/jobs 不可用时降级为「仅本机视角」，要告警但不刷屏，恢复时再说一声"""
    def boom(base, limit=100):
        raise ApiError("HTTP 404: not found", 404)

    warned, infos = [], []
    monkeypatch.setattr(mgr, "list_jobs", boom)
    monkeypatch.setattr(mgr, "raw_warning", warned.append)
    monkeypatch.setattr(mgr, "raw_info", infos.append)

    assert measure_load(lines["acc1"]) == (0, "local")
    measure_load(lines["acc1"], force=True)
    measure_load(lines["acc1"], force=True)
    assert len(warned) == 1 and "404" in warned[0]

    monkeypatch.setattr(mgr, "list_jobs", lambda base, limit=100: [])
    assert measure_load(lines["acc1"], force=True) == (0, "cloud")
    assert len(infos) == 1


def test_load_source_exposed_for_ui(lines, monkeypatch):
    assert mgr.load_source("acc1") is None          # 还没查过
    monkeypatch.setattr(mgr, "list_jobs", lambda base, limit=100: [])
    measure_load(lines["acc1"])
    assert mgr.load_source("acc1") == "cloud"


# ====================================================================
# 3. 选线：平局必须散开
# ====================================================================
def test_equal_load_is_spread_rather_than_dumped_on_first(lines, monkeypatch):
    """7 条线一样空：1000 次选线要散布到每条线（旧实现 100% 落在 acc1）"""
    monkeypatch.setattr(mgr, "list_jobs", lambda base, limit=100: [])
    hits = dict.fromkeys(lines, 0)
    for _ in range(1000):
        hits[pick_account().name] += 1
    assert all(v > 0 for v in hits.values()), hits
    assert hits["acc1"] < 400, hits


def test_clearly_loaded_line_is_avoided(lines, monkeypatch):
    def fake_list(base, limit=100):
        queued = ["queued"] * 9 if base.startswith("http://acc1.") else ["completed"]
        return [_job(s, i) for i, s in enumerate(queued)]

    monkeypatch.setattr(mgr, "list_jobs", fake_list)
    picks = {pick_account().name for _ in range(50)}
    assert "acc1" not in picks


def test_exclude_spares_the_line_just_failed(lines, monkeypatch):
    """换线重试不能又回到刚失败的那条；只剩它时才沿用原账号"""
    monkeypatch.setattr(mgr, "list_jobs", lambda base, limit=100: [])
    assert pick_account(exclude=lines["acc1"]) is not lines["acc1"]

    for a in lines.values():
        a.healthy = a.name == "acc1"
    assert pick_account(exclude=lines["acc1"]) is lines["acc1"]


# ====================================================================
# 4. 在途闸门：concurrency 第一次真正生效
# ====================================================================
def test_gate_waits_until_any_line_frees_up(lines, monkeypatch):
    ticks = {"n": 0}

    def fake_list(base, limit=100):
        return [_job("running")] if ticks["n"] < 2 else []

    def fake_sleep(_seconds):
        ticks["n"] += 1
        mgr.invalidate_load_cache()      # 真实等待时云端会变，缓存必须作废

    monkeypatch.setattr(mgr, "list_jobs", fake_list)
    acc, waited, got = pick_and_wait(_sleep=fake_sleep)
    assert got and ticks["n"] >= 2 and acc.name in LINES_SET


def test_prefers_line_with_free_slot_over_one_that_is_full(lines, monkeypatch):
    """并发数配得不一样时：acc1 并发 1 已排 1 条（满），acc2 并发 3 已排 2 条
    （还空）——该去 acc2，而不是死等负载数字更小的 acc1"""
    lines["acc2"].concurrency = 3
    for a in lines.values():
        a.healthy = a.name in ("acc1", "acc2")

    def fake_list(base, limit=100):
        n = 1 if base.startswith("http://acc1.") else 2
        return [_job("queued", str(i)) for i in range(n)]

    monkeypatch.setattr(mgr, "list_jobs", fake_list)
    assert pick_and_wait(_sleep=lambda s: None)[0] is lines["acc2"]


def test_gate_reports_timeout_but_does_not_lose_the_task(lines, monkeypatch):
    """等不到空位时仍要提交（宁超发不丢任务），但要把超时留痕成 False"""
    monkeypatch.setattr(mgr, "list_jobs",
                        lambda base, limit=100: [_job("queued")])
    _acc, waited, got = pick_and_wait(timeout=0.01, _sleep=lambda s: None)
    assert got is False and waited >= 0


def test_gate_can_be_switched_off(lines, monkeypatch):
    """submit_gate=false：即使全满也立刻放行，不进等待循环"""
    monkeypatch.setattr(mgr, "SUBMIT_GATE", False)
    monkeypatch.setattr(mgr, "list_jobs",
                        lambda base, limit=100: [_job("queued")])
    slept = []
    _acc, waited, got = pick_and_wait(_sleep=slept.append)
    assert got and waited == 0 and slept == []


def test_first_task_never_waits(lines, monkeypatch):
    monkeypatch.setattr(mgr, "list_jobs", lambda base, limit=100: [])
    _acc, waited, got = pick_and_wait(_sleep=lambda s: None)
    assert got and waited == 0


# ====================================================================
# 5. 全线路探活失败：候选是「最少那一档」，不是一条
# ====================================================================
def _all_red(lines, fail_count=5):
    for a in lines.values():
        a.healthy = False
        a.fail_count = fail_count


def test_all_lines_red_still_spreads(lines, monkeypatch):
    """旧实现 `sorted(ACCOUNTS, key=fail_count)[:1]` 只留一条：整批（多选强制
    重跑/抽卡）全灌 acc1、其余线全程空转——就是同事反馈的“只跑一个线路”"""
    monkeypatch.setattr(mgr, "list_jobs", lambda base, limit=100: [])
    _all_red(lines)
    hits = dict.fromkeys(lines, 0)
    for _ in range(300):
        hits[pick_account().name] += 1
    assert sum(1 for v in hits.values() if v) >= 2, hits


def test_all_red_pool_is_the_least_failed_group(lines):
    _all_red(lines, fail_count=4)
    assert len(mgr.schedulable_accounts()) == len(lines)   # 同档全部留下平摊
    lines["acc3"].fail_count = 1        # 只有一条最先恢复：先信它
    assert [a.name for a in mgr.schedulable_accounts()] == ["acc3"]


def test_all_red_warned_once_per_episode(lines, monkeypatch):
    """全红要告知（不然只看到任务全上一根线），但逐条提交不能刷屏"""
    warned = []
    monkeypatch.setattr(mgr, "raw_warning", warned.append)
    monkeypatch.setattr(mgr, "list_jobs", lambda base, limit=100: [])
    _all_red(lines)
    for _ in range(3):
        mgr.pick_account()
    assert len(warned) == 1 and "只灌一条" in warned[0]

    lines["acc1"].healthy = True          # 有线路回来了 → 告警位复位
    mgr.pick_account()
    _all_red(lines)
    mgr.pick_account()
    assert len(warned) == 2               # 下次再全红还要说一次
