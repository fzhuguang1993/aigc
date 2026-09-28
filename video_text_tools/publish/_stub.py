"""
video_text_tools/publish/_stub.py —— 「接口待配置」脚手架适配器基类

一期的快手/小红书/视频号只有契约、没有真实端点实现：
- check_auth 明确回 (False, 待配置提示)；
- upload/publish 抛 NotConfiguredError，由 publish_one 兜成 ok=False 的失败行；
让 UI 账号列表、runner 分支、发布结果对四个平台保持同一套走法，后期把某平台的
真实实现替换进各自文件即可，不动注册表与上层。
"""
from .base import PlatformAdapter, register
from .models import (NotConfiguredError, AUTH_COOKIE, AUTH_OAUTH)


class StubAdapter(PlatformAdapter):
    """未实现平台的占位适配器。子类只需声明 key / display_name。"""

    auth_type = AUTH_COOKIE
    hint = "该平台接口待配置"

    def check_auth(self, acct, log=None):
        return False, self.hint

    def upload(self, acct, item, log=None, progress=None, should_stop=None):
        raise NotConfiguredError(self.hint)

    def publish(self, acct, item, handle, log=None):
        raise NotConfiguredError(self.hint)


def register_stub(platform, display_name):
    """为一个待配置平台同时登记 cookie / oauth 两种鉴权的占位适配器，
    使 UI 下拉与 get_adapter 都能解析到（并给出统一的「待配置」结果）。"""
    for auth in (AUTH_COOKIE, AUTH_OAUTH):
        cls = type(f"{platform}_{auth}_Stub",
                   (StubAdapter,),
                   {"key": platform, "auth_type": auth, "display_name": display_name,
                    "hint": f"{display_name}接口待配置（{auth}）"})
        register(cls)
