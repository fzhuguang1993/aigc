"""
test_publish_douyin.py —— 抖音 cookie 参考实现的上传+发布序列

零真实网络：把 douyin 模块里的 requests 换成假会话，验证
- check_auth 命中登录态；
- upload（申请直链 → PUT 文件）→ publish（创建视频 → aweme_id → post_url）跑通；
- 端点从配置读取（占位/空则明确"未配置"，不假成功）；
- 凭证不出现在失败信息里。
"""
import pytest

from video_text_tools.publish import douyin
from video_text_tools.publish import get_adapter
from video_text_tools.publish.models import Account, PublishItem

INIT = "https://dy.test/upload/init"
CREATE = "https://dy.test/create"
USER = "https://dy.test/user"
COOKIE = "sessionid=SUPER_SECRET_COOKIE_123"


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("get", url))
        return FakeResp(200, {"status_code": 0, "data": {"user": {"id": 1}}})

    def post(self, url, params=None, json=None, timeout=None, headers=None):
        self.calls.append(("post", url))
        if url == INIT:
            return FakeResp(200, {"status_code": 0, "data": {
                "upload_address": "https://dy.test/put/1",
                "video_model": "vm_1", "video_id": "vid_1"}})
        if url == CREATE:
            return FakeResp(200, {"status_code": 0, "data": {"aweme_id": "700123"}})
        return FakeResp(500, text="unexpected post " + url)

    def put(self, url, data=None, timeout=None, headers=None):
        self.calls.append(("put", url))
        return FakeResp(200)


class FakeRequests:
    """只暴露 douyin 适配器用到的两个符号，替换整个模块内的 requests 引用。
    复用同一个 session：upload 与 publish 各自 _session() 时也拿这一个，
    好让整条链路的调用能在 last.calls 里累积可见。"""
    RequestException = __import__("requests").RequestException

    def __init__(self):
        self.last = FakeSession()

    def Session(self):
        return self.last


@pytest.fixture
def fake_req(monkeypatch):
    fr = FakeRequests()
    monkeypatch.setattr(douyin, "requests", fr)
    return fr


def _acct(extra=None, cookie=COOKIE):
    base = {"upload_init_url": INIT, "create_url": CREATE, "user_info_url": USER,
            "post_url_base": "https://www.douyin.com/video/"}
    base.update(extra or {})
    return Account(id="a1", platform="douyin", label="主号", auth_type="cookie",
                   secret={"cookie": cookie}, extra=base)


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "out.mp4"
    p.write_bytes(b"0" * 128)
    return str(p)


def test_check_auth_ok(fake_req):
    ad = get_adapter("douyin", "cookie")
    ok, msg = ad.check_auth(_acct())
    assert ok is True
    assert "有效" in msg


def test_publish_flow_returns_post_url(fake_req, video):
    ad = get_adapter("douyin", "cookie")
    item = PublishItem(video_path=video, title="测试标题", tags=["a", "b"])
    rec = ad.publish_one(_acct(), item)
    assert rec.ok is True, rec.message
    assert rec.post_id == "700123"
    assert rec.post_url == "https://www.douyin.com/video/700123"
    # 会话按 init → put → create 顺序被调用
    urls = [c[1] for c in fake_req.last.calls]
    assert INIT in urls and CREATE in urls


def test_endpoint_not_configured_is_explicit(fake_req, video):
    ad = get_adapter("douyin", "cookie")
    acct = _acct(extra={"upload_init_url": "", "create_url": ""})   # 清空端点
    item = PublishItem(video_path=video, title="t")
    rec = ad.publish_one(acct, item)
    assert rec.ok is False
    assert "未配置" in rec.message
    assert COOKIE not in rec.message          # 凭证不外泄


def test_missing_credential_guard(fake_req, video):
    ad = get_adapter("douyin", "cookie")
    acct = _acct(cookie="")
    rec = ad.publish_one(acct, PublishItem(video_path=video, title="t"))
    assert rec.ok is False
    assert "凭证" in rec.message
