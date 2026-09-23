"""
tests/test_balance.py —— 注水式负载均衡分配回归测试

锁死两件事：
  1) plan_allocation 纯函数的注水分配结果（信息隔离下按快照把 n 条摊平）；
  2) BatchBalancer 的逐步 argmin（快照 + 本批投影）与该纯函数等价，且能重扫纠偏。
"""
import registry.manager as rm
from registry.manager import plan_allocation


# ------------------------------------------------------------------ plan_allocation
def test_plan_allocation_balances_to_target():
    # 你的例子：负载 [10,25,15,5] 投 20 → [7,0,2,11]，过载线(25)自动出局
    assert plan_allocation([10, 25, 15, 5], 20) == [7, 0, 2, 11]
    totals = [l + a for l, a in zip([10, 25, 15, 5], plan_allocation([10, 25, 15, 5], 20))]
    assert sorted(totals) == [16, 17, 17, 25]      # 可控制的三条被拉平


def test_plan_allocation_all_equal_spreads_evenly():
    assert plan_allocation([0, 0, 0, 0], 8) == [2, 2, 2, 2]
    # 奇数条：靠前索引多拿 1（确定性）
    assert plan_allocation([0, 0], 5) == [3, 2]


def test_plan_allocation_only_adds_never_subtracts():
    # 一条远超其他：缺口补齐前不给它分
    assert plan_allocation([0, 100], 5) == [5, 0]
    # n 大于缺口：先把低线抬到最高线，再均分
    adds = plan_allocation([0, 10], 15)
    assert sum(adds) == 15 and adds[1] >= 1        # 追平后过载线也会分到


def test_plan_allocation_edges():
    assert plan_allocation([], 5) == []
    assert plan_allocation([3, 3, 3], 0) == [0, 0, 0]
    assert plan_allocation([7], 4) == [4]          # 只有一条线全给它


# ------------------------------------------------------------------ BatchBalancer
class _FakeAcc:
    def __init__(self, name):
        self.name = name
        self.healthy = True
        self.fail_count = 0
        self.concurrency = 1


def _install_fake(monkeypatch, loads):
    accs = [_FakeAcc(n) for n in loads]
    monkeypatch.setattr(rm, "ACCOUNTS", accs)
    monkeypatch.setattr(rm, "invalidate_load_cache", lambda *a, **k: None)
    monkeypatch.setattr(rm, "cloud_load", lambda acc: loads[acc.name])
    monkeypatch.setattr(rm, "LOAD_TIE_BAND", 0)          # 关掉并列随机，取确定性 argmin
    monkeypatch.setattr(rm.random, "choice", lambda s: s[0])
    return accs


def test_batch_balancer_matches_plan_allocation(monkeypatch):
    loads = {"a": 10, "b": 25, "c": 15, "d": 5}
    _install_fake(monkeypatch, loads)
    b = rm.BatchBalancer(20, rescan_every=0)             # 不中途重扫 = 纯静态注水
    dist = {n: 0 for n in loads}
    for _ in range(20):
        dist[b.pick().name] += 1
    assert [dist[n] for n in ["a", "b", "c", "d"]] == plan_allocation([10, 25, 15, 5], 20)


def test_batch_balancer_robust_to_reflect_masking(monkeypatch):
    # 云端读数始终只含同事（本机任务因反映延迟从不入 cloud）：
    # base+ours 仍应给出正确均衡，不因遮罩而把最空线灌穿
    loads = {"a": 10, "b": 25, "c": 15, "d": 5}
    _install_fake(monkeypatch, loads)
    b = rm.BatchBalancer(20, rescan_every=3)             # 频繁重扫也不会丢基线
    dist = {n: 0 for n in loads}
    for _ in range(20):
        dist[b.pick().name] += 1
    assert [dist[n] for n in ["a", "b", "c", "d"]] == plan_allocation([10, 25, 15, 5], 20)


def test_batch_balancer_rescan_is_periodic(monkeypatch):
    loads = {"a": 0, "b": 0}
    _install_fake(monkeypatch, loads)
    b = rm.BatchBalancer(4, rescan_every=2)
    seq = [b.pick().name for _ in range(4)]
    assert seq == ["a", "b", "a", "b"]                   # 每 2 条重扫一次并重新摊平
    assert b.base["a"] == 0 and b.ours["a"] == 2         # base 保留、ours 精确累计


def test_batch_balancer_raises_base_on_colleague_growth(monkeypatch):
    loads = {"a": 0, "b": 0}
    _install_fake(monkeypatch, loads)
    b = rm.BatchBalancer(6, rescan_every=2)
    b.pick()                                             # -> a（ours a=1）
    b.pick()                                             # -> b, 触发前 _since>=2
    loads["b"] = 20                                      # 同事突然把 b 灌到 20
    # 下一次 pick 前会重扫，base[b] 上调 -> 应转投 a
    assert b.pick().name == "a" and b.ours["b"] == 1


def test_batch_balancer_skips_unhealthy(monkeypatch):
    loads = {"a": 0, "b": 0}
    accs = _install_fake(monkeypatch, loads)
    accs[0].healthy = False                              # a 挂了，只应选 b
    b = rm.BatchBalancer(3, rescan_every=0)
    assert [b.pick().name for _ in range(3)] == ["b", "b", "b"]


def test_batch_balancer_all_unhealthy_still_spreads(monkeypatch):
    """探活全军覆没时不能塌缩成一条线

    现场：云端没实现 GET /health（实测回 HTML 404，服务其实活着），三轮后所有线
    标红，旧兜底 `sorted(..., key=fail_count)[:1]` 只留 acc1 → 多选强制重跑的
    整批全灌一条线、其余全程空转（同事反馈）。"""
    loads = {"a": 0, "b": 0, "c": 0}
    accs = _install_fake(monkeypatch, loads)
    for a in accs:
        a.healthy = False
        a.fail_count = 3
    b = rm.BatchBalancer(9, rescan_every=0)
    seq = [b.pick().name for _ in range(9)]
    assert sorted(set(seq)) == ["a", "b", "c"], seq
