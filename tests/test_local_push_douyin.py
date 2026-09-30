"""
test_local_push_douyin.py —— 抖音本地推 OpenAPI 参考实现的标准序列

零真实网络：把 douyin 模块里的 requests 换成假客户端，验证
- check_auth 在线校验/跳过校验两条路径；
- 素材上传 → 创建项目 → 创建营销 跑通，回项目/营销 ID；
- 端点从配置（账号 extra）读取（占位/空则明确"未配置"，不假成功）；
- 网关 code!=0 时把人话带进失败信息；凭证不出现在失败信息里。
"""
import pytest

from video_text_tools.local_push import douyin
from video_text_tools.local_push import get_adapter
from video_text_tools.local_push.models import LocalAccount, PlanItem

INFO = "https://lp.test/advertiser/info"
UPLOAD = "https://lp.test/asset/upload"
PROJECT = "https://lp.test/project/create"
MARKETING = "https://lp.test/marketing/create"
TOKEN = "ACCESS_TOKEN_SUPER_SECRET_123"


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeRequests:
    """只暴露 douyin 适配器用到的符号（get/post/RequestException），
    替换整个模块内的 requests 引用；调用轨迹记在 calls 里。"""
    RequestException = __import__("requests").RequestException

    def __init__(self, project_resp=None):
        self.calls = []
        self.project_resp = project_resp

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("get", url))
        return FakeResp(200, {"code": 0, "data": {"id": 1}})

    def post(self, url, params=None, json=None, files=None, timeout=None, headers=None):
        self.calls.append(("post", url))
        if url == UPLOAD:
            return FakeResp(200, {"code": 0, "data": {"video_id": "vid_1"}})
        if url == PROJECT:
            return self.project_resp or FakeResp(200, {"code": 0, "data": {"project_id": "proj_9"}})
        if url == MARKETING:
            return FakeResp(200, {"code": 0, "data": {"promotion_id": "mk_8"}})
        return FakeResp(500, text="unexpected post " + url)


@pytest.fixture
def fake_req(monkeypatch):
    fr = FakeRequests()
    monkeypatch.setattr(douyin, "requests", fr)
    return fr


def _acct(extra=None, token=TOKEN, advertiser_id="1001"):
    base = {"advertiser_info_url": INFO, "asset_upload_url": UPLOAD,
            "project_create_url": PROJECT, "marketing_create_url": MARKETING}
    base.update(extra or {})
    return LocalAccount(id="a1", platform="douyin", label="门店A-主户",
                        advertiser_id=advertiser_id, auth_type="oauth",
                        secret={"access_token": token}, extra=base)


def _plan(videos):
    return PlanItem(id="p1", name="门店A方案", promo_type="store", goal="lead",
                    budget=300.0, bid=30.0, region="杭州,宁波",
                    target_id="shop_1", videos=videos, titles=["标题1"])


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "out.mp4"
    p.write_bytes(b"0" * 128)
    return str(p)


def test_check_auth_ok(fake_req):
    ad = get_adapter("douyin")
    ok, msg = ad.check_auth(_acct())
    assert ok is True
    assert "有效" in msg


def test_check_auth_skips_without_probe_endpoint(fake_req):
    # 没配探测端点不等于授权无效：凭证非空即通过、不阻断搭建
    ad = get_adapter("douyin")
    ok, msg = ad.check_auth(_acct(extra={"advertiser_info_url": ""}))
    assert ok is True
    assert "跳过在线校验" in msg


def test_build_flow_creates_project_and_marketing(fake_req, video):
    ad = get_adapter("douyin")
    rec = ad.run_one(_acct(), _plan([video]))
    assert rec.ok is True, rec.message
    assert rec.project_id == "proj_9"
    assert rec.marketing_id == "mk_8"
    assert rec.asset_count == 1
    # 按 上传 → 项目 → 营销 顺序被调用
    urls = [c[1] for c in fake_req.calls]
    assert urls.index(UPLOAD) < urls.index(PROJECT) < urls.index(MARKETING)


def test_endpoint_not_configured_is_explicit(fake_req, video):
    ad = get_adapter("douyin")
    acct = _acct(extra={"asset_upload_url": ""})   # 清空上传端点
    rec = ad.run_one(acct, _plan([video]))
    assert rec.ok is False
    assert "未配置" in rec.message
    assert TOKEN not in rec.message                # 凭证不外泄


def test_missing_credential_guard(fake_req, video):
    ad = get_adapter("douyin")
    rec = ad.run_one(_acct(token=""), _plan([video]))
    assert rec.ok is False
    assert "凭证" in rec.message


def test_missing_advertiser_id_guard(fake_req, video):
    ad = get_adapter("douyin")
    rec = ad.run_one(_acct(advertiser_id=""), _plan([video]))
    assert rec.ok is False
    assert "advertiser_id" in rec.message


def test_empty_videos_rejected(fake_req):
    ad = get_adapter("douyin")
    rec = ad.run_one(_acct(), _plan([]))
    assert rec.ok is False
    assert "素材" in rec.message


def test_gateway_error_surfaces_message(fake_req, video, monkeypatch):
    # 项目创建被平台拒绝（code!=0）：把平台的人话带进失败行
    bad = FakeRequests(project_resp=FakeResp(
        200, {"code": 40001, "message": "预算低于最低限额"}))
    monkeypatch.setattr(douyin, "requests", bad)
    ad = get_adapter("douyin")
    rec = ad.run_one(_acct(), _plan([video]))
    assert rec.ok is False
    assert "预算低于最低限额" in rec.message
