"""
test_publish_base.py —— 适配器契约与注册表

导入 publish 包即触发四平台注册。校验：
- 四个平台都能 get_adapter 命中；
- 每种鉴权下适配器实现了契约方法；
- 未知平台返回 None；supported_auth 反映已注册鉴权。
"""
import inspect

from video_text_tools import publish as pub


def test_all_platforms_registered():
    for key, _name in pub.PLATFORMS:
        ad = pub.get_adapter(key)
        assert ad is not None, f"{key} 未注册适配器"
        assert ad.key == key


def test_adapter_contract_methods():
    ad = pub.get_adapter("douyin")
    for m in ("check_auth", "upload", "publish", "publish_one"):
        assert callable(getattr(ad, m)), f"缺方法 {m}"
        assert m in {name for name, _ in inspect.getmembers(ad, predicate=callable)}


def test_douyin_is_cookie_only_stub_others_both():
    assert pub.supported_auth("douyin") == ["cookie"]
    for p in ("kuaishou", "xiaohongshu", "tencent"):
        assert set(pub.supported_auth(p)) == {"cookie", "oauth"}


def test_unknown_platform_returns_none():
    assert pub.get_adapter("bilibili") is None
    assert pub.get_adapter("douyin", "oauth") is None   # 抖音只注册了 cookie
