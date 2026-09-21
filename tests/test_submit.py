"""
tests/test_submit.py —— 提交主链路回归测试

覆盖：成功登记（REG/任务表/执行记录/payload 时长透传）、
失败换线重试、指数退避节奏、重试耗尽、信号量不泄漏。
所有外部依赖（HTTP、账号选择）通过 monkeypatch 注入，无网络、无真实服务。
"""
import threading

import pytest

import workers.submit as sub
from core.api_client import ApiError
from core.config import MAX_RETRY
from registry.manager import REG
from store import task_store
from workers.submit import do_submit, SubmitOptions


class FakeAccount:
    def __init__(self, name, healthy=True, concurrency=1):
        self.name = name
        self.base = f"http://{name}.test/api/v1"
        self.concurrency = concurrency
        self.healthy = healthy
        self.sem = threading.Semaphore(concurrency)


@pytest.fixture()
def accs(monkeypatch):
    """两个假账号，接管 workers.submit 命名空间里的账号选择与外部副作用"""
    accounts = {"acc1": FakeAccount("acc1"), "acc2": FakeAccount("acc2")}
    monkeypatch.setattr(sub, "get_account", lambda n: accounts.get(n))
    monkeypatch.setattr(sub, "CONFIG_ACCOUNTS", [{"name": n} for n in accounts])
    # 隔离外部副作用：扫描标记 / 负载缓存 / 参考图上传
    monkeypatch.setattr(sub, "mark_submitted", lambda row_idx: None)
    monkeypatch.setattr(sub, "invalidate_load_cache", lambda name=None: None)
    monkeypatch.setattr(sub, "upload_asset",
                        lambda base, p, t="image": "a" * 32)
    monkeypatch.setattr(sub, "pick_account", lambda: accounts["acc1"])
    return accounts


def _sem_available(acc):
    ok = acc.sem.acquire(blocking=False)
    if ok:
        acc.sem.release()
    return ok


class TestSubmitSuccess:
    def test_returns_job_id_and_writes_task_row(self, accs, monkeypatch):
        tid = task_store.add_task("001", "品名A", "提示词X")

        def fake_submit(base, payload):
            assert base == accs["acc1"].base
            return {"job_id": "job-42"}

        monkeypatch.setattr(sub, "submit_job", fake_submit)
        jid, err, acc_name = do_submit(tid, "品名A", "提示词X",
                                       SubmitOptions(duration=8))
        assert (jid, err, acc_name) == ("job-42", None, "acc1")

        task = task_store.get_task(tid)
        assert task["status"] == "submitted"
        assert task["job_id"] == "job-42"
        assert int(task["runs"]) == 1

    def test_options_flow_into_payload_and_registry(self, accs, monkeypatch):
        """时长/KOL 必须走显式参数：payload 与 REG 里都应是提交时的快照"""
        captured = {}
        monkeypatch.setattr(sub, "submit_job",
                            lambda base, payload: captured.update(payload) or {"job_id": "j1"})
        tid = task_store.add_task("002", "品名B", "提示词Y")
        do_submit(tid, "品名B", "提示词Y", SubmitOptions(duration=12))

        assert captured["parameters"]["duration"] == 12
        reg = REG.get_by_row(tid)
        assert reg and reg["duration"] == 12

    def test_run_record_created(self, accs, monkeypatch):
        monkeypatch.setattr(sub, "submit_job", lambda base, payload: {"job_id": "j9"})
        tid = task_store.add_task("003", "品名C", "提示词Z")
        do_submit(tid, "品名C", "提示词Z", SubmitOptions())
        runs = task_store.list_runs()
        assert any(r["job_id"] == "j9" and r["status"] == "running" for r in runs)


class TestRetryAndFailover:
    def test_switches_account_with_backoff(self, accs, monkeypatch):
        """acc1 失败一次 → 退避 → 换 acc2 成功；旧账号信号量必须已释放"""
        calls = []

        def flaky(base, payload):
            calls.append(base)
            if "acc1" in base:
                raise ApiError("HTTP 502: bad gateway", 502)
            return {"job_id": "job-77"}

        monkeypatch.setattr(sub, "submit_job", flaky)
        sleeps = []
        tid = task_store.add_task("004", "品名D", "提示词W")
        jid, err, acc_name = do_submit(tid, "品名D", "提示词W",
                                       SubmitOptions(duration=5),
                                       _sleep=sleeps.append)

        assert (jid, err, acc_name) == ("job-77", None, "acc2")
        assert len(calls) == 2
        assert sleeps == [sub.RETRY_BACKOFF_BASE]      # 第一次重试退避 2 秒
        assert _sem_available(accs["acc1"])            # 换线后旧信号量已归还
        assert _sem_available(accs["acc2"])            # 新信号量也已归还

    def test_all_retries_exhausted(self, accs, monkeypatch):
        """全部失败：返回 all retries failed，任务表/注册表不被污染"""
        monkeypatch.setattr(sub, "submit_job",
                            lambda base, payload: (_ for _ in ()).throw(ApiError("down")))
        sleeps = []
        tid = task_store.add_task("005", "品名E", "提示词V")
        jid, err, _ = do_submit(tid, "品名E", "提示词V", SubmitOptions(),
                                _sleep=sleeps.append)

        assert jid is None and err == "all retries failed"
        assert len(sleeps) == MAX_RETRY                # 每次重试前各退避一次
        # 指数退避且有上限
        assert sleeps == [min(sub.RETRY_BACKOFF_BASE * (2 ** i), sub.RETRY_BACKOFF_CAP)
                          for i in range(MAX_RETRY)]
        assert task_store.get_task(tid)["status"] != "submitted"
        assert REG.get_by_row(tid) is None
        assert _sem_available(accs["acc1"])

    def test_no_healthy_alternative_retries_same_account(self, accs, monkeypatch):
        """只有一个健康账号时不换线，原账号退避重试"""
        accs["acc2"].healthy = False
        used = []

        def only_log(base, payload):
            used.append(base)
            if len(used) == 1:
                raise ApiError("HTTP 500", 500)
            return {"job_id": "j5"}

        monkeypatch.setattr(sub, "submit_job", only_log)
        tid = task_store.add_task("006", "品名F", "提示词U")
        jid, err, acc_name = do_submit(tid, "品名F", "提示词U", SubmitOptions(),
                                       _sleep=lambda s: None)
        assert (jid, acc_name) == ("j5", "acc1")
        assert len(used) == 2
