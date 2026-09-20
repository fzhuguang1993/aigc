# ============================================================
# config.py —— AIGC 视频生成全局配置
# ============================================================
from pathlib import Path

# ============================================================
# 0. 运行时目录（程序启动所在目录，所有数据文件均保存在这里）
#    打包分发给其他人时，无需修改任何路径，自动落在运行目录下
# ============================================================
RUNTIME_DIR = Path.cwd()

# ============================================================
# 1. Excel 文件位置
# ============================================================
EXCEL_PATH = str(RUNTIME_DIR / "AIGC辅助excel.xlsx")

# Excel 里的 sheet 名
SHEET_TASK = "Sheet"
SHEET_NAME_RULE = "命名规则"

# ---------- 任务表列名 ----------
COL_ID = "编号"
COL_PRODUCT = "品名"
COL_PROMPT = "提示词"
COL_SCRIPT = "脚本"
COL_STATUS = "状态"
COL_ACCOUNT = "账号"
COL_JOB_ID = "job_id"
COL_OUTPUT = "输出"
COL_URL = "URL"
COL_RUNS = "运行次数"
COL_SUCCESS = "成功次数"
COL_CANCEL = "取消次数"
COL_SCRIPT_TEXT = "口播文案"

# ---------- 命名规则表列名 ----------
COL_NAME = "姓名"

# ============================================================
# 2. 素材目录
# ============================================================
ASSET_DIR = str(RUNTIME_DIR)

# ============================================================
# 3. 视频下载目录（运行目录下自动创建 outputs/）
# ============================================================
DOWNLOAD_DIR = str(RUNTIME_DIR / "outputs")

# ============================================================
# 5. 日志（运行目录下自动创建 logs/）
# ============================================================
LOG_DIR = str(RUNTIME_DIR / "logs")
LOG_LEVEL_FILE = "DEBUG"
LOG_LEVEL_CONSOLE = "INFO"
LOG_RETENTION_DAYS = 7

# ============================================================
# 6. 本地用户配置
#    分发模式：运行目录下的 config.json（首次运行向导自动生成，含姓名与账号接口）
#    开发模式：无 config.json 时回退到 core/config_local.py
# ============================================================
CONFIG_JSON = RUNTIME_DIR / "config.json"


def _load_local_config():
    """返回 (accounts, user_name)"""
    if CONFIG_JSON.exists():
        import json
        try:
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            return data.get("accounts", []), data.get("user_name", "")
        except Exception as e:
            print(f"⚠ config.json 解析失败（{e}），将重新进入配置向导")
            return [], ""
    try:
        from core.config_local import ACCOUNTS as _A
        return list(_A), ""
    except ImportError:
        return [], ""


ACCOUNTS, USER_NAME = _load_local_config()

# ============================================================
# 6.1 口播脚本 AI 检测接口（可选，规范卡配套功能用）
#     不配置也能用：本地规则检测（禁用词/价格口径/必含话术）。
#     配置后「AI 智能检测」会把 脚本+规范卡+风控政策 发给大模型做语义级审查。
#     写法一：config.json 加 "script_check": {"enabled": true, "url": "...", "api_key": "...", "model": "..."}
#     写法二：core/config_local.py 定义 SCRIPT_CHECK = {...}
# ============================================================

def _load_script_check():
    if CONFIG_JSON.exists():
        import json
        try:
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            cfg = data.get("script_check") or {}
            if cfg:
                return cfg
        except Exception:
            pass
    try:
        from core.config_local import SCRIPT_CHECK
        return dict(SCRIPT_CHECK)
    except Exception:
        return {}


SCRIPT_CHECK = {"enabled": False, "url": "", "api_key": "", "model": "",
                **_load_script_check()}

# ============================================================
# 7. 脚本行为
# ============================================================
POLL_INTERVAL = 5
SCAN_INTERVAL = 10
JOB_TIMEOUT = 3600
MAX_RETRY = 1
DOWNLOAD_RETRY = 3
DOWNLOAD_RETRY_DELAY = 5

# ============================================================
# 8. 健康检查
# ============================================================
HEALTH_CHECK_INTERVAL = 30
HEALTH_FAIL_THRESHOLD = 3

# ============================================================
# 9. 负载均衡
# ============================================================
LOAD_CACHE_TTL = 5
JOBS_LIMIT = 100

# ============================================================
# 10. 视频生成参数
# ============================================================
MODE = "r2v"

# 视频时长（秒）：2-15
# 可以通过命令切换：5 秒模式 / 15 秒模式
DEFAULT_DURATION = 5

WIDTH = 768
HEIGHT = 1376
SEED = -1

# 参考图片配置
MATERIAL_DIR = str(RUNTIME_DIR / "material")
REFERENCE_IMAGES = [
    f"{MATERIAL_DIR}/诺特兰德益生菌.png",
]

# KOL 配置
KOL_DIR = f"{MATERIAL_DIR}/KOL"
KOL_OPTIONS = [
    "贾乃亮 1",
    "贾乃亮 2",
    "贾乃亮 3",
    # 可以添加更多 KOL
]

LORA_BY_MODE = {
    "t2v": "",
    "i2v": "minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors",
    "r2v": "minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors",
}

# 当前视频时长（秒）- 这个变量在 main.py 中定义
# 不要在这里定义，避免循环导入

# ============================================================
# 12. 脚本提取配置
# ============================================================
# 是否提取口播文案
EXTRACT_SCRIPT_ENABLED = True

# 最小视频时长才提取口播（秒）
# 短于这个时长的视频不提取口播
MIN_DURATION_FOR_SCRIPT = 10

# ============================================================
# 13. 品名和 KOL 选择配置
# ============================================================
# 上次使用的品名和 KOL 组合（用于快速沿用）
LAST_PRODUCT = "诺特兰德益生菌"
LAST_KOL = None  # None 表示不使用 KOL

# ============================================================
# 11. 视频文件命名
# ============================================================
DEFAULT_PRODUCT = "未知品名"
