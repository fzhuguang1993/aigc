"""
video_text_tools/publish/xiaohongshu.py —— 小红书发布适配器（一期：接口待配置）

真实实现时，参照 douyin.py 在本文件内定义并 @register 适配器。当前用脚手架占位，
保证四平台契约一致，发布该平台账号会得到明确的「接口待配置」失败行。
"""
from .models import PLATFORM_XHS, platform_label
from ._stub import register_stub

register_stub(PLATFORM_XHS, platform_label(PLATFORM_XHS))
