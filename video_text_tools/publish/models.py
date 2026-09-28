"""
video_text_tools/publish/models.py —— 一键发布的数据结构与异常

纯数据 + 异常，无 Qt、无网络。各平台适配器（cookie / OAuth）共用这里的口径：
- Account：一个平台账号（凭证在 config.json 里加密存，读取时解密回填 secret）
- PublishItem：一条要发布的视频 + 元数据（标题/简介/标签/封面/定时/可见性）
- PublishRecord：一次「视频 × 账号」的发布结果行，是 runner 的产出、也是历史入库
  与 GUI 预览的唯一数据源；ok=False 时 message 必须是人能看懂的原因，绝不静默。
"""
from dataclasses import dataclass, field
from datetime import datetime


# ---------------- 平台清单与鉴权方式（UI/适配器注册表共用同一份常量） ----------------
PLATFORM_DOUYIN = "douyin"
PLATFORM_KUAISHOU = "kuaishou"
PLATFORM_XHS = "xiaohongshu"
PLATFORM_TENCENT = "tencent"
# (键, 中文名)：GUI 下拉、账号列表、runner 分支都以此为准
PLATFORMS = [
    (PLATFORM_DOUYIN, "抖音"),
    (PLATFORM_KUAISHOU, "快手"),
    (PLATFORM_XHS, "小红书"),
    (PLATFORM_TENCENT, "视频号"),
]
PLATFORM_LABELS = dict(PLATFORMS)

AUTH_COOKIE = "cookie"    # 网页会话 cookie（本期抖音参考实现走这条）
AUTH_OAUTH = "oauth"      # 官方开放平台 OAuth access_token
AUTH_TYPES = [AUTH_COOKIE, AUTH_OAUTH]
AUTH_TYPE_LABELS = {AUTH_COOKIE: "网页 Cookie", AUTH_OAUTH: "官方 OAuth"}


def platform_label(key):
    return PLATFORM_LABELS.get(key, key)


class PublishError(Exception):
    """发布链路通用异常基类：消息给人看懂，直接进 PublishRecord.message。"""


class AuthError(PublishError):
    """登录态失效 / 凭证缺失 / 未配置：据此提示重新配置账号，不重试。"""


class UploadError(PublishError):
    """媒体上传失败（申请直链、PUT 上传、回执解析出错）。"""


class RemoteRejectError(PublishError):
    """平台侧拒绝发布（审核不通过、参数非法、频控等）。"""


class NotConfiguredError(PublishError):
    """该平台接口尚未配置（一期的快手/小红书/视频号脚手架）。"""


@dataclass
class Account:
    """一个平台账号。

    secret 是明文凭证字典（cookie 串或 access_token 等），仅在内存里流转；
    落盘由 config 段用 encrypt_value 加密成 secret_enc。extra 放平台私有可变项
    （端点覆盖、签名参数名等），不敏感。"""
    id: str = ""
    platform: str = ""
    label: str = ""                              # 用户自定义别名（如"主号-抖音"）
    auth_type: str = AUTH_COOKIE
    secret: dict = field(default_factory=dict)   # {"cookie": "..."} / {"access_token": "..."}
    extra: dict = field(default_factory=dict)

    def has_credential(self):
        return any(str(v or "").strip() for v in (self.secret or {}).values())


@dataclass
class PublishItem:
    """一条待发布视频及其元数据。"""
    video_path: str = ""
    title: str = ""
    desc: str = ""
    tags: list = field(default_factory=list)     # ["标签1", "标签2"]
    cover_path: str = ""
    publish_at: str = ""                         # 定时发布（ISO 字符串），空=立即
    visibility: str = "public"                   # public / private / friends（平台各取支持项）

    def display_title(self):
        return self.title or ""


@dataclass
class PublishRecord:
    """一次「视频 × 账号」的发布结果行。"""
    platform: str = ""
    account_label: str = ""
    title: str = ""
    video_path: str = ""
    ok: bool = False
    post_id: str = ""
    post_url: str = ""
    message: str = ""
    published_at: str = ""

    @property
    def platform_label(self):
        return platform_label(self.platform)

    def summary(self):
        head = f"[{self.platform_label}/{self.account_label}] {self.title}"
        if self.ok:
            return f"✓ {head} → {self.post_url or self.post_id or '已发布'}"
        return f"✗ {head} 失败：{self.message or '未提供原因'}"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
