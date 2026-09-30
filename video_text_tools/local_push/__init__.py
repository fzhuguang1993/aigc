"""
video_text_tools/local_push —— 批量基建纯逻辑层（抖音本地推 · 账户搭计划）

无 Qt：gui.dialogs_build.BuildPanel 通过 ToolWorker 调 runner。架构：
  models    数据结构（LocalAccount/PlanItem/BuildRecord）+ 异常 + 平台/口径常量
  base      BuildAdapter 契约 + 注册表（register/get_adapter）
  douyin    抖音本地推参考实现（官方 OpenAPI/OAuth2.0，端点全部走配置）
  accounts  本地推账户读写门面（加解密在 core.config）
  plans     计划方案模板读写门面（明文 config 段）
  runner    方案×账户 批量编排，单格失败不中断、逐条不静默

导入本包即触发适配模块加载 → 适配器登记进注册表。
"""
from .models import (  # noqa: F401
    PLATFORM_DOUYIN_LOCAL, LOCAL_PLATFORMS, PLATFORM_LABELS, platform_label,
    AUTH_OAUTH, PROMO_STORE, PROMO_GOODS, PROMO_TYPES, PROMO_LABELS,
    GOAL_LEAD, GOAL_MESSAGE, GOAL_COUPON, GOALS, GOAL_LABELS,
    LocalAccount, PlanItem, BuildRecord,
    LocalBuildError, BuildAuthError, BuildRemoteError, BuildNotConfiguredError,
)
from .base import (  # noqa: F401
    BuildAdapter, register, get_adapter, is_configured,
)
from .accounts import (  # noqa: F401
    list_accounts, save_account, delete_account,
)
from .plans import (  # noqa: F401
    list_plans, save_plan, delete_plan,
)
from .runner import build_batch  # noqa: F401

# 触发适配器注册（导入即把平台键登记进注册表）
from . import douyin        # noqa: F401,E402  参考实现
