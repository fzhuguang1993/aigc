"""
video_text_tools/publish/kuaishou.py —— 快手发布适配器（一期：接口待配置）

真实实现时，参照 douyin.py 在本文件内定义并 @register 一个（或 cookie/oauth 两个）
PlatformAdapter 子类：check_auth / upload / publish。当前先用脚手架占位，保证四平台
在 UI 与 runner 里走同一套契约，发布该平台的账号会得到明确的「接口待配置」失败行。
"""
from .models import PLATFORM_KUAISHOU, platform_label
from ._stub import register_stub

register_stub(PLATFORM_KUAISHOU, platform_label(PLATFORM_KUAISHOU))
