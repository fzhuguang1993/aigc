"""
video_text_tools/publish/base.py —— 平台适配器契约 + 注册表

设计目标：四个平台（抖音/快手/小红书/视频号）与两种鉴权（cookie / OAuth）共用
一套接口，UI 与 runner 只认 key，不关心具体平台怎么发。新增/替换平台只改对应
适配文件，不动这里的契约。

契约要点：
- 一个适配器 = 一个 (platform, auth_type) 组合的发布能力；
- 任何一步失败都应落到 PublishRecord(ok=False, message=人话)，
  而不是抛到 runner 顶层——发布是批量动作，一条失败不能带走其它条。
  publish_one 已把 upload/publish 的异常统一兜成失败行，子类一般无需再管。
"""
from abc import ABC, abstractmethod

from .models import (PublishRecord, PublishError, AuthError, UploadError,
                     RemoteRejectError, NotConfiguredError, platform_label)


class PlatformAdapter(ABC):
    """单个平台的一种鉴权方式的发布器。

    子类需声明：key / auth_type / display_name，并实现 check_auth/upload/publish。"""

    key = ""                       # 平台键（douyin/…）
    auth_type = ""                 # cookie / oauth
    display_name = ""              # 注册名，仅日志/调试用

    # ---- 契约方法 ----
    @abstractmethod
    def check_auth(self, acct, log=None):
        """探测登录态：返回 (ok: bool, msg: str)。凭证缺失应直接 (False, 提示)。"""
        raise NotImplementedError

    @abstractmethod
    def upload(self, acct, item, log=None, progress=None, should_stop=None):
        """上传媒体，成功返回一个 handle(dict，供 publish 用)，失败抛 PublishError。"""
        raise NotImplementedError

    @abstractmethod
    def publish(self, acct, item, handle, log=None):
        """用 upload 的 handle 创建发布，返回 PublishRecord（ok 由本方法判定）。"""
        raise NotImplementedError

    # ---- 通用编排：upload → publish，异常兜成失败行 ----
    def publish_one(self, acct, item, log=None, progress=None, should_stop=None):
        rec = PublishRecord(platform=self.key, account_label=acct.label,
                            title=item.display_title(), video_path=item.video_path)
        try:
            if not acct.has_credential():
                raise AuthError("账号未配置凭证（cookie/token 为空）")
            if should_stop and should_stop():
                rec.message = "已取消"
                return rec
            handle = self.upload(acct, item, log=log, progress=progress,
                                 should_stop=should_stop)
            if should_stop and should_stop():
                rec.message = "已取消"
                return rec
            rec = self.publish(acct, item, handle, log=log)
            if rec.platform != self.key:      # 兜底补全，防子类忘了填
                rec.platform = self.key
            if not rec.account_label:
                rec.account_label = acct.label
            return rec
        except (PublishError, NotImplementedError) as e:
            rec.ok = False
            rec.message = str(e) or type(e).__name__
            return rec
        except Exception as e:                # 未预期异常也不外抛，标注来源类型
            rec.ok = False
            rec.message = f"{type(e).__name__}: {e}"
            return rec


# ---------------- 注册表 ----------------
# 结构：{platform_key: {auth_type: adapter_instance}}
_REGISTRY = {}


def register(cls):
    """类装饰器：按 (key, auth_type) 把适配器实例登记进注册表。"""
    if not cls.key or not cls.auth_type:
        raise ValueError(f"适配器 {cls!r} 必须声明 key 与 auth_type")
    _REGISTRY.setdefault(cls.key, {})[cls.auth_type] = cls()
    return cls


def get_adapter(platform, auth_type=None):
    """取适配器：给了 auth_type 精确取，否则取该平台第一个已注册的。无则 None。"""
    by_auth = _REGISTRY.get(platform) or {}
    if auth_type:
        return by_auth.get(auth_type)
    for _t, ad in by_auth.items():
        return ad
    return None


def supported_auth(platform):
    """该平台已注册的鉴权方式列表（UI 下拉据此过滤）。"""
    return list((_REGISTRY.get(platform) or {}).keys())


def is_configured(platform):
    """是否已有可用适配器实现（脚手架平台也算注册，靠 publish 抛 NotConfigured 区分）。"""
    return bool(_REGISTRY.get(platform))


def platform_display(platform):
    return platform_label(platform)


__all__ = ["PlatformAdapter", "register", "get_adapter", "supported_auth",
           "is_configured", "platform_display",
           "PublishRecord", "PublishError", "AuthError", "UploadError",
           "RemoteRejectError", "NotConfiguredError"]
