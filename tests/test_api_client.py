"""
tests/test_api_client.py —— api_client 统一异常约定的回归测试

锁死约定：成功返回解析结果；HTTP >= 400 / 网络异常一律抛 ApiError。
"""
import pytest

import core.api_client as api


class FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def test_submit_job_success_returns_json(monkeypatch):
    monkeypatch.setattr(api.requests, "request",
                        lambda m, u, **kw: FakeResp(200, {"job_id": "j1"}))
    assert api.submit_job("http://x/api/v1", {}) == {"job_id": "j1"}


def test_http_error_raises_apierror_with_status(monkeypatch):
    monkeypatch.setattr(api.requests, "request",
                        lambda m, u, **kw: FakeResp(429, text="too many"))
    with pytest.raises(api.ApiError) as e:
        api.submit_job("http://x/api/v1", {})
    assert e.value.status_code == 429
    assert "429" in str(e.value)


def test_network_error_wrapped_into_apierror(monkeypatch):
    def boom(*a, **kw):
        raise api.requests.ConnectionError("conn refused")
    monkeypatch.setattr(api.requests, "request", boom)
    with pytest.raises(api.ApiError, match="conn refused"):
        api.health("http://x/api/v1")


def test_query_job_returns_dict(monkeypatch):
    monkeypatch.setattr(api.requests, "request",
                        lambda m, u, **kw: FakeResp(200, {"status": "running", "progress": 42}))
    s = api.query_job("http://x/api/v1", "j1")
    assert s["status"] == "running" and s["progress"] == 42


def test_list_jobs_unwraps_items(monkeypatch):
    monkeypatch.setattr(api.requests, "request",
                        lambda m, u, **kw: FakeResp(200, {"items": [{"job_id": "j1"}]}))
    assert api.list_jobs("http://x/api/v1") == [{"job_id": "j1"}]


def test_extract_script_returns_plain_text(monkeypatch):
    monkeypatch.setattr(api.requests, "request",
                        lambda m, u, **kw: FakeResp(200, {"script": "口播文案"}))
    assert api.extract_script("http://x/api/v1", "p") == "口播文案"


def test_extract_script_missing_field_returns_empty(monkeypatch):
    monkeypatch.setattr(api.requests, "request",
                        lambda m, u, **kw: FakeResp(200, {}))
    assert api.extract_script("http://x/api/v1", "p") == ""
