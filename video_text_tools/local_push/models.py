"""
video_text_tools/local_push/models.py —— 批量基建的数据结构与异常

纯数据 + 异常，无 Qt、无网络。口径与一键发布（publish）平行：
- LocalAccount：一个本地推账户（OAuth 凭证在 config.json 加密存，读取时解密回填）
- PlanItem：一个「计划方案」模板 —— 项目/营销层参数（预算/出价/定向/门店或商品）
  + 素材清单（视频/标题），执行时按 方案 × 账户 批量下发
- BuildRecord：一次「方案 × 账户」的搭建结果行，是 runner 的产出、也是历史
  入库的唯一数据源；ok=False 时 message 必须是人能看懂的原因，绝不静默。
"""
from dataclasses import dataclass, field
from datetime import datetime


# ---------------- 平台清单与鉴权方式（UI/适配器注册表共用同一份常量） ----------------
PLATFORM_DOUYIN_LOCAL = "douyin"
# (键, 中文名)：GUI 下拉、账号列表、runner 分支都以此为准（先只铺本地推）
LOCAL_PLATFORMS = [
    (PLATFORM_DOUYIN_LOCAL, "抖音本地推"),
]
PLATFORM_LABELS = dict(LOCAL_PLATFORMS)

AUTH_OAUTH = "oauth"          # 官方开放平台 OAuth access_token（本地推走这条）


def platform_label(key):
    return PLATFORM_LABELS.get(key, key)


# ---------------- 推广口径常量（GUI 下拉与适配器 payload 共用） ----------------
PROMO_STORE = "store"         # 推广门店
PROMO_GOODS = "goods"         # 推广商品
PROMO_TYPES = [(PROMO_STORE, "推广门店"), (PROMO_GOODS, "推广商品")]
PROMO_LABELS = dict(PROMO_TYPES)

GOAL_LEAD = "lead"            # 获取线索
GOAL_MESSAGE = "message"      # 私信消息
GOAL_COUPON = "coupon"        # 团购成交
GOALS = [(GOAL_LEAD, "获取线索"), (GOAL_MESSAGE, "私信消息"), (GOAL_COUPON, "团购成交")]
GOAL_LABELS = dict(GOALS)


class LocalBuildError(Exception):
    """批量基建链路通用异常基类：消息给人看懂，直接进 BuildRecord.message。"""


class BuildAuthError(LocalBuildError):
    """授权失效 / 凭证缺失 / 未配置：据此提示重新配置账户，不重试。"""


class BuildRemoteError(LocalBuildError):
    """平台侧拒绝创建（参数非法、余额不足、频控等）。"""


class BuildNotConfiguredError(LocalBuildError):
    """该平台端点尚未配置（留空或占位 <...>）。"""


@dataclass
class LocalAccount:
    """一个本地推账户（三级组织的叶子）。

    secret 是明文凭证字典（access_token / refresh_token），仅在内存里流转；
    落盘由 local_org_store 用 encrypt_value 加密成 secret_enc 存 DB。
    advertiser_id 是本地推广告主账户 ID（接口调用全程要带）；extra 放平台私有
    可变项（端点覆盖等）。id 承载 DB 主键（空=新建）。license_id/customer_id 是
    组织归属；owner 是负责成员名，为空则就近继承执照→客户（层级/逐个混合授权）；
    license_name/customer_name 只为展示与历史落库回填。"""
    id: str = ""
    platform: str = PLATFORM_DOUYIN_LOCAL
    label: str = ""                                # 用户自定义别名（如"门店A-主户"）
    advertiser_id: str = ""                        # 本地推广告主账户 ID
    auth_type: str = AUTH_OAUTH
    secret: dict = field(default_factory=dict)     # {"access_token": "..."}
    extra: dict = field(default_factory=dict)
    license_id: int = 0                            # 所属执照/主体
    customer_id: int = 0                           # 所属客户（经执照推导）
    owner: str = ""                                # 负责成员名；空=继承执照/客户
    license_name: str = ""
    customer_name: str = ""

    def has_credential(self):
        return bool(str((self.secret or {}).get("access_token") or "").strip())

    def org_display(self):
        """面包屑式的归属展示：客户 › 执照。"""
        path = " › ".join(x for x in (self.customer_name, self.license_name) if x)
        return path or "未分组"


@dataclass
class PlanItem:
    """一个「计划方案」模板：项目/营销层参数 + 素材清单。

    字段与本地推新版层级对齐（账户 → 项目 → 营销 → 素材）：
    - 项目层：promo_type / goal / budget / bid / region / age / gender / hours
    - 营销层：target_id（门店或商品 ID）、douyin_uid（投放抖音号）
    - 素材层：videos（视频文件）、titles（标题）
    真实请求 payload 由适配器组装（字段名以官方文档为准，端点走配置）。"""
    id: str = ""
    name: str = ""
    promo_type: str = PROMO_STORE
    goal: str = GOAL_LEAD
    budget: float = 0.0                            # 日预算（元）
    bid: float = 0.0                               # 出价（元）
    region: str = ""                               # 投放地域（逗号分隔地名/编码，透传）
    age: str = "all"                               # 年龄（all/18-23/24-30/…）
    gender: str = "all"                            # 性别（all/male/female）
    hours: str = ""                                # 投放时段（如 9-22，空=全时段）
    target_id: str = ""                            # 门店 ID / 商品 ID
    douyin_uid: str = ""                           # 投放抖音号（可空）
    videos: list = field(default_factory=list)     # 素材视频文件绝对路径
    titles: list = field(default_factory=list)     # 素材标题（可空）
    remark: str = ""

    def display_name(self):
        return self.name or "未命名方案"


@dataclass
class BuildRecord:
    """一次「方案 × 账户」的搭建结果行。"""
    platform: str = ""
    account_label: str = ""
    advertiser_id: str = ""
    plan_name: str = ""
    ok: bool = False
    project_id: str = ""
    marketing_id: str = ""
    asset_count: int = 0
    message: str = ""
    created_at: str = ""
    customer_id: int = 0                           # 组织维度（历史按层级归集/统计）
    license_id: int = 0
    account_id: int = 0
    customer_name: str = ""
    license_name: str = ""

    @property
    def platform_label(self):
        return platform_label(self.platform)

    def org_display(self):
        path = " › ".join(x for x in (self.customer_name, self.license_name) if x)
        return path or "未分组"

    def summary(self):
        head = (f"[{self.platform_label}/{self.org_display()}/{self.account_label}] "
                f"{self.plan_name}")
        if self.ok:
            return (f"✓ {head} → 项目 {self.project_id or '-'} / "
                    f"营销 {self.marketing_id or '-'}")
        return f"✗ {head} 失败：{self.message or '未提供原因'}"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def apply_org_fields(rec, acct):
    """把账户（LocalAccount）上的组织维度回填到结果行（BuildRecord）。

    源为账户（truth），已有值不覆盖。account_id 由账户 DB 主键（字符串）安全转
    int；非数字 id（假数据/测试）归 0，不抛。"""
    def _to_int(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    rec.account_id = rec.account_id or _to_int(getattr(acct, "id", 0))
    rec.license_id = rec.license_id or _to_int(getattr(acct, "license_id", 0))
    rec.customer_id = rec.customer_id or _to_int(getattr(acct, "customer_id", 0))
    rec.customer_name = rec.customer_name or (getattr(acct, "customer_name", "") or "")
    rec.license_name = rec.license_name or (getattr(acct, "license_name", "") or "")
    return rec
