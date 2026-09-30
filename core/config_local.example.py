# ============================================================
# config_local.example.py —— 本地敏感配置模板
# 首次使用：复制本文件为 config_local.py，并填入真实信息
#   cp core/config_local.example.py core/config_local.py
# ============================================================

# 账号列表（API 服务地址，支持多账号负载均衡与故障转移）
ACCOUNTS = [
    {"name": "acc1", "base": "http://127.0.0.1:7860/api/v1", "concurrency": 1},
    # {"name": "acc2", "base": "http://<服务地址>:7860/api/v1", "concurrency": 1},
]

# 素材/文案提取接口（工具中心「素材提取」，聚客 API）
# 这三行故意不写进仓库：只填在本机这份 config_local.py 里（已被 .gitignore 忽略），
# 打包时会被编译进 exe。不填也能跑：工具界面里保存过的 uid/key 会落在
# material/api_text/api_config.json，优先级比这里高。
MATERIAL_API_BASE = "https://<提取接口地址>/home/api"
MATERIAL_API_UID = "<你的 UID>"
MATERIAL_API_KEY = "<你的 Key>"

# 机器翻译接口（火山引擎「文本翻译」）：任务弹窗里 Alt+W 唤出的
# 「提示词中文对照 / 改完中文改回英文」。不填也能开软件，点翻译时才提示未配置。
# 开通与申请：https://console.volcengine.com/ （对象存储同账号的访问密钥即可）
TRANSLATE = {
    "ak": "<你的 AccessKeyID>",
    "sk": "<你的 SecretAccessKey>",
}

# 商用网关模式总开关（打包前由维护机写入真实值）：
# 配了就整体切到「激活 + 网关」形态——本地 ACCOUNTS 作废、上游地址/密钥全部
# 收进服务端；买家首启需输卡密激活，之后请求都经网关中转。
# 留空/不写 = 开发/内网直连模式，一切维持旧行为。
# 示例：GATEWAY_BASE = "https://gate.你的域名.com"   # 只写站点根即可，/api/v1 会自动补
GATEWAY_BASE = ""
# 可选：网关模式下客户端并发闸门（默认 999 = 不在客户端拦，排队交网关统一调度）
# GATEWAY_CONCURRENCY = 999

# 试用版限时锁定（打「给别人试用的特别版」前由维护机写入真实值）：
# 填了 = 自首次运行起 N 小时后弹窗退出；不填 / 0 / 注释掉 = 正式版行为，完全不锁。
# 判定与落盘逻辑见 core/trial.py（trial.json + 注册表双锚点、防手改、防改系统时间）。
# 想给某人 72 小时试用：取消下一行注释即可，然后单独打一份 exe 给他。
# TRIAL_HOURS = 72
