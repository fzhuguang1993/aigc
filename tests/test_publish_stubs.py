"""
test_publish_stubs.py —— 快手/小红书/视频号脚手架的"不静默"行为

一期这三个平台没有真实端点实现，但必须：
- check_auth 回 (False, 含"待配置")；
- publish_one 绝不抛异常，而是落 ok=False、message 含"待配置"的失败行。
"""
import pytest

from video_text_tools.publish import get_adapter
from video_text_tools.publish.models import Account, PublishItem


@pytest.mark.parametrize("platform", ["kuaishou", "xiaohongshu", "tencent"])
@pytest.mark.parametrize("auth", ["cookie", "oauth"])
def test_stub_check_auth_and_publish_not_configured(platform, auth):
    ad = get_adapter(platform, auth)
    assert ad is not None
    acct = Account(id="a", platform=platform, label="别名", auth_type=auth,
                   secret={"cookie": "x=y"})
    ok, msg = ad.check_auth(acct)
    assert ok is False
    assert "待配置" in msg

    item = PublishItem(video_path="whatever.mp4", title="标题")
    rec = ad.publish_one(acct, item)
    assert rec.ok is False
    assert "待配置" in rec.message
    assert rec.platform == platform
    assert rec.account_label == "别名"
