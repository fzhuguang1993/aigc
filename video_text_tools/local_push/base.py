"""
video_text_tools/local_push/base.py —— 本地推搭建适配器契约 + 注册表

与一键发布的 base.py 同构（设计取向见 video_text_tools/publish/base.py）：
- 一个适配器 = 一个平台的批量搭建能力（先只铺抖音本地推）；
- 任何一步失败都应落到 BuildRecord(ok=False, message=人话)，而不是抛到
  runner 顶层——批量动作里一条失败不能带走其它条。run_one 已把内部异常
  统一兜成失败行，子类里只管抛 LocalBuildError 子类。
"""
from abc import ABC, abstractmethod

from .models import (BuildRecord, LocalBuildError, BuildAuthError,
                     BuildRemoteError, BuildNotConfiguredError,
                     platform_label, apply_org_fields)


class BuildAdapter(ABC):
    """单个平台的批量搭建器。

    子类需声明：key / display_name，并实现 check_auth / build_one。"""

    key = ""                       # 平台键（douyin）
    display_name = ""              # 注册名，仅日志/调试用

    # ---- 契约方法 ----
    @abstractmethod
    def check_auth(self, acct, log=None):
        """探测授权态：返回 (ok: bool, msg: str)。凭证缺失应直接 (False, 提示)。"""
        raise NotImplementedError

    @abstractmethod
    def build_one(self, acct, plan, log=None, progress=None, should_stop=None):
        """在账户下按方案搭建「项目 → 营销」（含素材上传）→ 返回 BuildRecord。

        子类内部实现可自由拆步，但不能把异常抛到 runner 顶层。"""
        raise NotImplementedError

    # ---- 通用编排：兜异常 → 失败行 ----
    def run_one(self, acct, plan, log=None, progress=None, should_stop=None):
        rec = BuildRecord(platform=self.key, account_label=acct.label,
                          advertiser_id=acct.advertiser_id,
                          plan_name=plan.display_name())
        apply_org_fields(rec, acct)          # 失败/取消行也带上组织维度
        try:
            if not acct.has_credential():
                raise BuildAuthError("账户未配置凭证（access_token 为空）")
            if should_stop and should_stop():
                rec.message = "已取消"
                return rec
            rec = self.build_one(acct, plan, log=log, progress=progress,
                                 should_stop=should_stop)
            if rec.platform != self.key:      # 兜底补全，防子类忘了填
                rec.platform = self.key
            if not rec.account_label:
                rec.account_label = acct.label
            if not rec.advertiser_id:
                rec.advertiser_id = acct.advertiser_id
            apply_org_fields(rec, acct)       # build_one 返回新 rec，再回填一次
            return rec
        except (LocalBuildError, NotImplementedError) as e:
            rec.ok = False
            rec.message = str(e) or type(e).__name__
            return rec
        except Exception as e:                # 未预期异常也不外抛，标注来源类型
            rec.ok = False
            rec.message = f"{type(e).__name__}: {e}"
            return rec


# ---------------- 注册表 ----------------
# 结构：{platform_key: adapter_instance}
_REGISTRY = {}


def register(cls):
    """类装饰器：把适配器实例按平台键登记进注册表。"""
    if not cls.key:
        raise ValueError(f"适配器 {cls!r} 必须声明 key")
    _REGISTRY[cls.key] = cls()
    return cls


def get_adapter(platform):
    """取该平台的适配器；未注册返回 None。"""
    return _REGISTRY.get(platform)


def is_configured(platform):
    """是否已有可用适配器实现。"""
    return platform in _REGISTRY


def platform_display(platform):
    return platform_label(platform)


__all__ = ["BuildAdapter", "register", "get_adapter", "is_configured",
           "platform_display",
           "BuildRecord", "LocalBuildError", "BuildAuthError",
           "BuildRemoteError", "BuildNotConfiguredError"]
