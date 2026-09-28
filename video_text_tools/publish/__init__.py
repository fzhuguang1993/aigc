"""
video_text_tools/publish —— 一键发布纯逻辑层（对接抖音/快手/小红书/视频号）

无 Qt：gui.dialogs_publish.PublishPanel 通过 ToolWorker 调 runner。架构：
  models    数据结构（Account/PublishItem/PublishRecord）+ 异常 + 平台/鉴权常量
  base      平台适配器契约 PlatformAdapter + 注册表（register/get_adapter）
  douyin    抖音 cookie 参考实现（一期唯一尽量可跑通的平台，端点全部走配置）
  kuaishou / xiaohongshu / tencent   一期"接口待配置"脚手架（导入即注册占位）
  accounts  平台账号读写门面（加解密在 core.config）
  runner    视频×账号 批量编排，单格失败不中断、逐条不静默

导入本包即触发各平台适配模块加载 → 适配器登记进注册表。
"""
from .models import (  # noqa: F401
    PLATFORMS, PLATFORM_LABELS, PLATFORM_DOUYIN, PLATFORM_KUAISHOU,
    PLATFORM_XHS, PLATFORM_TENCENT, platform_label,
    AUTH_COOKIE, AUTH_OAUTH, AUTH_TYPES, AUTH_TYPE_LABELS,
    Account, PublishItem, PublishRecord,
    PublishError, AuthError, UploadError, RemoteRejectError, NotConfiguredError,
)
from .base import (  # noqa: F401
    PlatformAdapter, register, get_adapter, supported_auth, is_configured,
)
from .accounts import (  # noqa: F401
    list_accounts, accounts_by_platform, save_account, delete_account,
)
from .runner import publish_batch  # noqa: F401

# 触发适配器注册（顺序无要求；导入即把 (platform, auth_type) 登记进注册表）
from . import douyin        # noqa: F401,E402  参考实现
from . import kuaishou      # noqa: F401,E402  脚手架
from . import xiaohongshu   # noqa: F401,E402  脚手架
from . import tencent       # noqa: F401,E402  脚手架
