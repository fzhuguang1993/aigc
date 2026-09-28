"""
video_text_tools/publish/tencent.py —— 微信视频号发布适配器（一期：接口待配置）

真实实现时，参照 douyin.py 在本文件内定义并登记适配器（视频号可走 cookie 会话或
官方接口）。当前用脚手架占位，保证四平台契约一致，发布该平台账号会得到明确的
「接口待配置」失败行。
"""
from .models import PLATFORM_TENCENT, platform_label
from ._stub import register_stub

register_stub(PLATFORM_TENCENT, platform_label(PLATFORM_TENCENT))
