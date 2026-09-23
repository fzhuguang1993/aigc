"""
tests/test_submit.py —— 提交主链路回归测试

覆盖：成功登记（REG/任务表/执行记录/payload 时长、步数透传）、
失败换线重试、指数退避节奏、重试耗尽、信号量不泄漏、
云端拒绝参数（422）时不换线白烧重试并能摘掉被拒字段自愈。
所有外部依赖（HTTP、账号选择）通过 monkeypatch 注入，无网络、无真实服务。
"""
import threading

import pytest

import workers.submit as sub
from core.api_client import ApiError
from core.config import MAX_RETRY, DEFAULT_STEPS
from registry.manager import REG
from store import task_store
from workers.submit import do_submit, SubmitOptions

# 云端真实的参数拒绝报文（线上事故原文，字段名 steps 不被 JobParameters 接受）
REAL_422 = ('HTTP 422: {"error":{"code":"validation_error",'
            '"message":"The request parameters are invalid.",'
            '"details":{"errors":[{"type":"extra_forbidden",'
            '"loc":["body","parameters","steps"],'
            '"msg":"Extra inputs are not permitted","input":8}]},'
            '"request_id":"71bd6247"}}')


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
    # 隔离外部副作用：扫描标记 / 负载缓存 / 参考图上传
    monkeypatch.setattr(sub, "mark_submitted", lambda row_idx: None)
    monkeypatch.setattr(sub, "invalidate_load_cache", lambda name=None: None)
    monkeypatch.setattr(sub, "upload_asset",
                        lambda base, p, t="image": "a" * 32)
    # 选线与在途闸门不走网络（真实行为在 tests/test_registry_load.py 里测）
    def fake_pick(exclude=None):
        """按健康状态选线，acc1 优先；exclude 供换线重试用"""
        healthy = [a for a in accounts.values() if a.healthy]
        pool = [a for a in healthy if a is not exclude] or healthy or [accounts["acc1"]]
        return pool[0]

    monkeypatch.setattr(sub, "pick_account", fake_pick)
    monkeypatch.setattr(sub, "pick_and_wait",
                        lambda **kw: (fake_pick(), 0, True))
    monkeypatch.setattr(sub, "measure_load", lambda acc, force=False: (0, "cloud"))
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
            return "job-42"

        monkeypatch.setattr(sub, "submit_job", fake_submit)
        jid, err, acc_name = do_submit(tid, "品名A", "提示词X",
                                       SubmitOptions(duration=8))
        assert (jid, err, acc_name) == ("job-42", None, "acc1")

        task = task_store.get_task(tid)
        assert task["status"] == "submitted"
        assert task["job_id"] == "job-42"
        assert int(task["runs"]) == 1

    def test_options_flow_into_payload_and_registry(self, accs, monkeypatch):
        """时长/步数/KOL 必须走显式参数：payload 与 REG 里都应是提交时的快照"""
        captured = {}
        monkeypatch.setattr(sub, "submit_job",
                            lambda base, payload: captured.update(payload) or "j1")
        tid = task_store.add_task("002", "品名B", "提示词Y")
        do_submit(tid, "品名B", "提示词Y", SubmitOptions(duration=12, steps=42))

        assert captured["parameters"]["duration"] == 12
        assert captured["parameters"]["inference_steps"] == 42
        # 回归锁：云端 JobParameters 是 additionalProperties=False，
        # 写成 steps 会让每一条提交都 422 失败（线上真实踩过）
        assert "steps" not in captured["parameters"]
        reg = REG.get_by_row(tid)
        assert reg and reg["duration"] == 12

    def test_steps_default_and_clamp(self):
        """步数：缺省用 DEFAULT_STEPS，越界自动限幅到 1-50"""
        assert SubmitOptions().steps == DEFAULT_STEPS
        assert SubmitOptions(steps=99).steps == 50
        assert SubmitOptions(steps=0).steps == 1

    def test_run_record_created(self, accs, monkeypatch):
        monkeypatch.setattr(sub, "submit_job", lambda base, payload: "j9")
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
            return "job-77"

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
        """全部失败：返回可读原因，任务表/注册表不被污染"""
        monkeypatch.setattr(sub, "submit_job",
                            lambda base, payload: (_ for _ in ()).throw(ApiError("接口不可达")))
        sleeps = []
        tid = task_store.add_task("005", "品名E", "提示词V")
        jid, err, _ = do_submit(tid, "品名E", "提示词V", SubmitOptions(),
                                _sleep=sleeps.append)

        assert jid is None
        assert "接口不可达" in err              # 不再是 ":all retries failed" 这种看不出原因的话
        assert len(sleeps) == MAX_RETRY                # 每次重试前各退避一次
        # 指数退避且有上限
        assert sleeps == [min(sub.RETRY_BACKOFF_BASE * (2 ** i), sub.RETRY_BACKOFF_CAP)
                          for i in range(MAX_RETRY)]
        assert task_store.get_task(tid)["status"] != "submitted"
        assert REG.get_by_row(tid) is None
        assert _sem_available(accs["acc1"])

    def test_retry_switches_by_load_not_config_order(self, accs, monkeypatch):
        """回归：旧换线实现取配置里第一条健康账号，等于永远换到 acc1"""
        calls = []

        def spy_pick(exclude=None):
            calls.append(exclude)
            return accs["acc2"]

        monkeypatch.setattr(sub, "pick_account", spy_pick)
        assert sub._next_account(accs["acc1"]) is accs["acc2"]
        assert calls == [accs["acc1"]]

    def test_no_healthy_alternative_retries_same_account(self, accs, monkeypatch):
        """只有一个健康账号时不换线，原账号退避重试"""
        accs["acc2"].healthy = False
        used = []

        def only_log(base, payload):
            used.append(base)
            if len(used) == 1:
                raise ApiError("HTTP 500", 500)
            return "j5"

        monkeypatch.setattr(sub, "submit_job", only_log)
        tid = task_store.add_task("006", "品名F", "提示词U")
        jid, err, acc_name = do_submit(tid, "品名F", "提示词U", SubmitOptions(),
                                       _sleep=lambda s: None)
        assert (jid, acc_name) == ("j5", "acc1")
        assert len(used) == 2


class TestPayloadFieldNames:
    """云端 JobParameters / JobInputs 都是 additionalProperties=False 的严格模型，
    多一个字段整单 422。这里的白名单抓自真实云端
    `GET {base}/openapi.json`（核对方法：python test/check_payload_fields.py），
    目的不是复述文档，而是把「字段名写错 → 全库任务一条都发不出去」钉在测试里。
    """
    JOB_PARAMETERS = {"width", "height", "duration", "quality_mode", "inference_steps",
                      "model_mode", "main_model", "seed", "upscale_enabled",
                      "upscale_mode", "upscale_scale", "frame_interpolation_enabled",
                      "frame_interpolation_multiplier", "audio_drive_enabled",
                      "ref_image_size", "avatar_audio_mode", "avatar_style",
                      "fixed_character_shot", "loras"}
    JOB_INPUTS = {"prompt", "first_frame", "last_frame", "reference_images",
                  "reference_videos", "reference_audios", "storyboard_id",
                  "avatar_image", "driving_audio", "voice_reference_audio",
                  "dialogue_language", "dialogue_text"}
    TOP_LEVEL = {"feature", "mode", "inputs", "parameters"}

    def test_payload_only_uses_known_fields(self, accs, monkeypatch):
        captured = {}
        monkeypatch.setattr(sub, "submit_job",
                            lambda base, payload: captured.update(payload) or "j")
        tid = task_store.add_task("010", "品名J", "提示词Q")
        do_submit(tid, "品名J", "提示词Q", SubmitOptions(duration=15, steps=50))

        assert set(captured) <= self.TOP_LEVEL
        assert set(captured["inputs"]) <= self.JOB_INPUTS
        assert set(captured["parameters"]) <= self.JOB_PARAMETERS
        # 取值也要在云端允许的区间内（duration 2-15、inference_steps 1-50）
        p = captured["parameters"]
        assert 2 <= p["duration"] <= 15 and 1 <= p["inference_steps"] <= 50

    def test_lora_entries_only_use_known_fields(self, accs, monkeypatch):
        """loras 元素同样严格：只认 name / strength"""
        captured = {}
        monkeypatch.setattr(sub, "submit_job",
                            lambda base, payload: captured.update(payload) or "j")
        tid = task_store.add_task("011", "品名K", "提示词P")
        do_submit(tid, "品名K", "提示词P", SubmitOptions())
        for lora in captured["parameters"]["loras"]:
            assert set(lora) <= {"name", "strength"}


class TestRejectedParams:
    """云端拒绝参数（4xx）：换线路不可能成功，必须快速失败并把原因递到界面"""

    def test_client_error_stops_at_first_line(self, accs, monkeypatch):
        """回归：旧实现对 422 也逐条线退避换线重试——本次事故里 7 条线
        全被同一个错误拒一遍，任务一条没发出去，界面只有一行小字"""
        used, sleeps = [], []

        def deny(base, payload):
            used.append(base)
            raise ApiError(REAL_422, 422)

        monkeypatch.setattr(sub, "submit_job", deny)
        tid = task_store.add_task("007", "品名G", "提示词T")
        jid, err, acc_name = do_submit(tid, "品名G", "提示词T", SubmitOptions(),
                                       _sleep=sleeps.append)

        assert jid is None
        assert used == [accs["acc1"].base]          # 不逐条线白烧
        assert sleeps == []                         # 不退避重试
        assert "parameters.steps" in err             # 原因里看得到被拒字段

    def test_rejected_field_is_stripped_then_resubmitted_same_line(self, accs, monkeypatch):
        """自愈：云端不认识的字段摘掉后原线立即重发，最多损失一个参数而不是全部任务"""
        seen, called = [], []

        def deny_once(base, payload):
            seen.append({k: v for k, v in payload["parameters"].items()})
            called.append(base)
            if len(called) == 1:
                raise ApiError('HTTP 422: {"details":{"errors":['
                               '{"type":"extra_forbidden",'
                               '"loc":["body","parameters","inference_steps"]}]}}', 422)
            return "j-healed"

        monkeypatch.setattr(sub, "submit_job", deny_once)
        tid = task_store.add_task("008", "品名H", "提示词S")
        jid, err, acc_name = do_submit(tid, "品名H", "提示词S", SubmitOptions(steps=20))

        assert (jid, err) == ("j-healed", None)
        assert "inference_steps" in seen[0] and "inference_steps" not in seen[1]
        assert called[0] == called[1]                # 同一条线，没换线也没重传参考图

    def test_unparseable_4xx_still_reports(self, accs, monkeypatch):
        """报文解析不出来也不能挂掉：至少把 HTTP 状态码递出去"""
        monkeypatch.setattr(sub, "submit_job",
                            lambda base, payload: (_ for _ in ()).throw(
                                ApiError("HTTP 400: <html>bad gateway page</html>", 400)))
        tid = task_store.add_task("009", "品名I", "提示词R")
        jid, err, _ = do_submit(tid, "品名I", "提示词R", SubmitOptions())
        assert jid is None and "HTTP 400" in err

    def test_unexpected_exception_reported_with_type(self, accs, monkeypatch):
        """非 ApiError 的意外异常（如 KeyError）：不能只报 'job_id' 这种
        看不出原因的一词，必须带异常类型；失败原因递到返回值"""
        def weird(base, payload):
            raise KeyError("job_id")

        monkeypatch.setattr(sub, "submit_job", weird)
        tid = task_store.add_task("012", "品名L", "提示词O")
        jid, err, _ = do_submit(tid, "品名L", "提示词O", SubmitOptions(),
                                _sleep=lambda s: None)
        assert jid is None
        assert "KeyError" in err and "job_id" in err


class TestRejectedParamParsing:
    def test_real_cloud_message(self):
        assert sub.parse_rejected(REAL_422) == [["body", "parameters", "steps"]]

    def test_fastapi_default_detail_shape(self):
        text = ('HTTP 422: {"detail":[{"type":"extra_forbidden",'
                '"loc":["body","inputs","foo"]}]}')
        assert sub.parse_rejected(text) == [["body", "inputs", "foo"]]

    def test_value_error_is_not_a_rejected_field(self):
        """字段存在但取值非法（如 duration=99）不能当“多余字段”摘掉"""
        text = ('HTTP 422: {"detail":[{"type":"less_than_equal",'
                '"loc":["body","parameters","duration"]}]}')
        assert sub.parse_rejected(text) == []

    def test_garbage_returns_empty(self):
        assert sub.parse_rejected("HTTP 422: 不是 JSON") == []
        assert sub.parse_rejected("") == []

    def test_drop_rejected_only_touches_named_key(self):
        payload = {"parameters": {"duration": 5, "steps": 8}}
        assert sub.drop_rejected(payload, [["body", "parameters", "steps"]]) \
            == ["parameters.steps"]
        assert payload == {"parameters": {"duration": 5}}
        # 路径不存在时不抛异常、不摘任何东西
        assert sub.drop_rejected(payload, [["body", "nope", "x"]]) == []
