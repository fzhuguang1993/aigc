"""
test_publish_runner.py —— 批量编排矩阵与"单格失败不中断"

用假适配器替换 runner 里的 get_adapter，聚焦编排逻辑：
- M 条视频 × N 个账号 = M*N 条结果，逐条回报进度；
- 某格适配器抛异常/无适配器都不带走整批；
- should_stop 后剩余格标"已取消"、不再调用发布。
"""
from video_text_tools.publish import runner
from video_text_tools.publish.models import Account, PublishItem, PublishRecord


class OkAdapter:
    def publish_one(self, acct, item, log=None, progress=None, should_stop=None):
        return PublishRecord(platform=acct.platform, account_label=acct.label,
                             title=item.title, video_path=item.video_path,
                             ok=True, post_url="u/" + item.title)


class BoomAdapter:
    def publish_one(self, acct, item, log=None, progress=None, should_stop=None):
        raise RuntimeError("炸了")


def _items(n):
    return [PublishItem(video_path=f"{i}.mp4", title=f"t{i}") for i in range(n)]


def _accts(n):
    return [Account(id=f"a{i}", platform="douyin", label=f"acc{i}",
                    auth_type="cookie", secret={"cookie": "x"}) for i in range(n)]


def test_matrix_counts_and_progress(monkeypatch):
    monkeypatch.setattr(runner, "get_adapter", lambda p, a=None: OkAdapter())
    seen = []
    recs = runner.publish_batch(_items(2), _accts(3),
                                progress=lambda c, t, name: seen.append((c, t)))
    assert len(recs) == 6
    assert all(r.ok for r in recs)
    assert seen[-1] == (6, 6)                      # 进度铺到满


def test_one_failure_does_not_stop_batch(monkeypatch):
    # 第一个账号的适配器炸，其余正常：整批仍有结果，失败的落 ok=False
    state = {"n": 0}

    def pick(p, a=None):
        return BoomAdapter() if state["n"] == 0 else OkAdapter()

    def spy_get(p, a=None):
        ad = pick(p, a)
        state["n"] += 1
        return ad
    monkeypatch.setattr(runner, "get_adapter", spy_get)
    recs = runner.publish_batch(_items(1), _accts(3))
    assert len(recs) == 3
    assert sum(1 for r in recs if r.ok) == 2       # 一个失败，两个仍发出去
    assert any((not r.ok) and "炸了" in r.message for r in recs)


def test_no_adapter_records_failure(monkeypatch):
    monkeypatch.setattr(runner, "get_adapter", lambda p, a=None: None)
    recs = runner.publish_batch(_items(1), _accts(1))
    assert recs[0].ok is False
    assert "无可用适配器" in recs[0].message


def test_stop_marks_remaining_cancelled(monkeypatch):
    monkeypatch.setattr(runner, "get_adapter", lambda p, a=None: OkAdapter())
    calls = {"stop": False}
    # 第一条正常跑完后置 stop：后续格标已取消、不再走 publish_one
    def ad_factory(p, a=None):
        return OkAdapter()
    monkeypatch.setattr(runner, "get_adapter", ad_factory)
    n = {"i": 0}
    orig = OkAdapter.publish_one

    def counting(self, acct, item, **kw):
        n["i"] += 1
        calls["stop"] = n["i"] >= 2      # 第二次之后要求停止
        return orig(self, acct, item, **kw)
    monkeypatch.setattr(OkAdapter, "publish_one", counting)
    recs = runner.publish_batch(_items(1), _accts(4),
                                should_stop=lambda: calls["stop"])
    assert len(recs) == 4
    assert any((not r.ok) and r.message == "已取消" for r in recs)


def test_empty_inputs_short_circuit(monkeypatch):
    monkeypatch.setattr(runner, "get_adapter", lambda p, a=None: OkAdapter())
    assert runner.publish_batch([], _accts(2)) == []
    assert runner.publish_batch(_items(2), []) == []
