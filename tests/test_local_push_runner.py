"""
test_local_push_runner.py —— 批量基建编排矩阵与"单格失败不中断"

用假适配器替换 runner 里的 get_adapter，聚焦编排逻辑：
- M 个方案 × N 个账户 = M*N 条结果，逐格回报进度；
- 某格适配器抛异常/无适配器/账户缺凭证都不带走整批；
- should_stop 后剩余格标"已取消"、不再调用搭建。
"""
from video_text_tools.local_push import runner
from video_text_tools.local_push.base import BuildAdapter
from video_text_tools.local_push.models import (BuildRecord, LocalAccount,
                                                PlanItem)


class OkAdapter(BuildAdapter):
    """走真 run_one（含凭证检查），build_one 直接回成功记录。"""
    key = "douyin"

    def check_auth(self, acct, log=None):
        return True, "ok"

    def build_one(self, acct, plan, log=None, progress=None, should_stop=None):
        return BuildRecord(platform=self.key, account_label=acct.label,
                           advertiser_id=acct.advertiser_id,
                           plan_name=plan.display_name(), ok=True,
                           project_id="p-" + plan.name,
                           marketing_id="m-" + plan.name)


class BoomAdapter(OkAdapter):
    def build_one(self, acct, plan, log=None, progress=None, should_stop=None):
        raise RuntimeError("炸了")


def _plans(n):
    return [PlanItem(id=f"p{i}", name=f"plan{i}", videos=["a.mp4"]) for i in range(n)]


def _accts(n, token="x"):
    return [LocalAccount(id=f"a{i}", platform="douyin", label=f"acc{i}",
                         advertiser_id=str(1000 + i),
                         secret={"access_token": token}) for i in range(n)]


def test_matrix_counts_and_progress(monkeypatch):
    monkeypatch.setattr(runner, "get_adapter", lambda p: OkAdapter())
    seen = []
    recs = runner.build_batch(_plans(2), _accts(3),
                              progress=lambda c, t, name: seen.append((c, t)))
    assert len(recs) == 6
    assert all(r.ok for r in recs)
    assert seen[-1] == (6, 6)                      # 进度铺到满
    assert all(r.project_id and r.marketing_id for r in recs)


def test_one_failure_does_not_stop_batch(monkeypatch):
    # 第一个账户的适配器炸，其余正常：整批仍有结果，失败的落 ok=False
    state = {"n": 0}

    def spy_get(p):
        ad = BoomAdapter() if state["n"] == 0 else OkAdapter()
        state["n"] += 1
        return ad
    monkeypatch.setattr(runner, "get_adapter", spy_get)
    recs = runner.build_batch(_plans(1), _accts(3))
    assert len(recs) == 3
    assert sum(1 for r in recs if r.ok) == 2       # 一个失败，两个仍建出来
    assert any((not r.ok) and "炸了" in r.message for r in recs)


def test_no_adapter_records_failure(monkeypatch):
    monkeypatch.setattr(runner, "get_adapter", lambda p: None)
    recs = runner.build_batch(_plans(1), _accts(1))
    assert recs[0].ok is False
    assert "无可用适配器" in recs[0].message


def test_missing_credential_records_failure(monkeypatch):
    # run_one 的凭证护栏：token 为空 → 失败行，不进 build_one
    monkeypatch.setattr(runner, "get_adapter", lambda p: OkAdapter())
    recs = runner.build_batch(_plans(1), _accts(1, token=""))
    assert recs[0].ok is False
    assert "凭证" in recs[0].message


def test_stop_marks_remaining_cancelled(monkeypatch):
    monkeypatch.setattr(runner, "get_adapter", lambda p: OkAdapter())
    n = {"i": 0}
    calls = {"stop": False}
    orig = OkAdapter.build_one

    def counting(self, acct, plan, **kw):
        n["i"] += 1
        calls["stop"] = n["i"] >= 2      # 第二次之后要求停止
        return orig(self, acct, plan, **kw)
    monkeypatch.setattr(OkAdapter, "build_one", counting)
    recs = runner.build_batch(_plans(1), _accts(4),
                              should_stop=lambda: calls["stop"])
    assert len(recs) == 4
    assert any((not r.ok) and r.message == "已取消" for r in recs)
    assert sum(1 for r in recs if r.ok) == 2         # 停止前最后一格跑完才停


def test_empty_inputs_short_circuit(monkeypatch):
    monkeypatch.setattr(runner, "get_adapter", lambda p: OkAdapter())
    assert runner.build_batch([], _accts(2)) == []
    assert runner.build_batch(_plans(2), []) == []


def test_org_fields_backfilled_into_records(monkeypatch):
    """账户上的组织维度经 run_one 回填到结果行（build_one 负责回新 rec）。"""
    monkeypatch.setattr(runner, "get_adapter", lambda p: OkAdapter())
    acct = LocalAccount(id="88", platform="douyin", label="门店A-主户",
                        advertiser_id="1001", secret={"access_token": "x"},
                        license_id=7, customer_id=3,
                        license_name="主体A1", customer_name="连锁A")
    recs = runner.build_batch(_plans(1), [acct])
    assert len(recs) == 1
    r = recs[0]
    assert r.ok
    assert (r.account_id, r.license_id, r.customer_id) == (88, 7, 3)
    assert r.customer_name == "连锁A" and r.license_name == "主体A1"
