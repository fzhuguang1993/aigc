# ============================================================
# config.py —— AIGC 视频生成全局配置
# ============================================================
from pathlib import Path

# ============================================================
# 0. 运行时目录（所有数据文件均保存在这里）
#    打包分发时无需改任何路径，数据/配置/日志/输出都落在软件自己旁边。
#    可用环境变量 AIGC_HOME 显式指定（自动化测试/多实例场景）。
#    口径单实在 core/paths.py：首次运行向导要赶在 config.json 生成前用同一把尺子。
# ============================================================
from core.paths import RUNTIME_DIR, CONFIG_DIR  # noqa: E402  (re-export，供历史 `from core.config import RUNTIME_DIR` 使用)

# ============================================================
# 0.1 配置家与 config.json 底座（先于目录常量，供各段共用）
#     凭证类配置落在隐藏目录 CONFIG_DIR（默认 %APPDATA%\AIGC视频助手）。
# ============================================================
import json as _json

CONFIG_JSON = CONFIG_DIR / "config.json"
_JSON_CACHE = None


def _json_data():
    """一次性读取并缓存 config.json 原始 dict，供账号/脚本检测/调度参数/输出目录共用。"""
    global _JSON_CACHE
    if _JSON_CACHE is None:
        data = {}
        if CONFIG_JSON.exists():
            try:
                loaded = _json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as e:
                print(f"⚠ config.json 解析失败（{e}），将重新进入配置向导")
                data = {}
        _JSON_CACHE = data
    return _JSON_CACHE


def _resolve_dir(key, default):
    """输出目录 = config.json 的 paths.<key> 覆盖；留空/缺省回退运行目录默认。"""
    paths = _json_data().get("paths")
    v = str((paths or {}).get(key, "") if isinstance(paths, dict) else "").strip()
    return v or default

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
# 3. 视频下载 / 模板导出目录（默认在运行目录下，可在设置里改到别处）
# ============================================================
DOWNLOAD_DIR = _resolve_dir("output", str(RUNTIME_DIR / "outputs"))
EXPORT_DIR = _resolve_dir("export", str(RUNTIME_DIR / "exports"))

# ============================================================
# 5. 日志（运行目录下自动创建 logs/）
# ============================================================
LOG_DIR = str(RUNTIME_DIR / "logs")
LOG_LEVEL_FILE = "DEBUG"
LOG_LEVEL_CONSOLE = "INFO"
LOG_RETENTION_DAYS = 7

# ============================================================
# 6. 本地用户配置
#    分发模式：隐藏配置家 CONFIG_DIR 下的 config.json（见 0.1，含姓名与账号接口）
#    开发模式：无 config.json 时回退到 core/config_local.py
#    （CONFIG_JSON 已在 0.1 定义，此处只保留分段说明）
# ============================================================


def _normalize_account_base(url):
    """账号 base 兜底归一化：去尾斜杠、补 /api/v1 后缀。
    手写/旧版 config.json 常漏后缀，导致 POST 打到服务根路径返回 405。"""
    url = str(url).strip().rstrip("/")
    if url and not url.endswith("/api/v1"):
        url += "/api/v1"
    return url


def _normalized_accounts(accounts):
    """线路条目归一化：补 /api/v1、补 name/concurrency、容许写成纯字符串。

    `AccountState` 是裸取 `cfg["name"]/["base"]/["concurrency"]` 的，手写
    config.json 或内置项少一个键就会在 import 阶段 KeyError，整个软件起不来；
    base 为空的条目本身就没法用，直接丢掉（与 gui.pages_settings.parse_lines 同口径）。"""
    out = []
    for i, a in enumerate(accounts or []):
        if isinstance(a, str):                      # 允许只写一个地址
            a = {"base": a}
        if not isinstance(a, dict):
            continue
        base = str(a.get("base") or "").strip()
        if not base:
            continue
        try:
            conc = int(a.get("concurrency") or 1)
        except (TypeError, ValueError):
            conc = 1
        out.append({**a, "name": str(a.get("name") or f"acc{i + 1}"),
                    "base": _normalize_account_base(base),
                    "concurrency": max(conc, 1)})
    return out


def _is_placeholder(entry):
    """模板占位（从 config_local.example.py 抄来的 `<服务地址>` 之类）不算真线路"""
    base = str((entry.get("base") if isinstance(entry, dict) else entry)
               or "").strip()
    return not base or base.startswith("<") or "服务地址" in base


def defaults_accounts():
    """打包内置的默认线路（维护机的 core/config_local.py 编进 exe）。
    有内置值时，首次配置只问姓名，不再让同事填地址。"""
    try:
        from core.config_local import ACCOUNTS as _A
    except (ImportError, AttributeError):
        # ImportError：没这份文件（仓库克隆）；AttributeError：文件在但没定义
        # ACCOUNTS（只配了提取接口凭证）——两种都退回“让使用者自己填地址”，
        # 不能在这里抛异常把整个软件卡在 import 阶段。
        return []
    return _normalized_accounts([a for a in list(_A) if not _is_placeholder(a)])


def _load_local_config():
    """返回 (accounts, user_name)：config.json 优先，没线路时回退内置默认。

    “有 config.json 但里面没 accounts”是正常状态：开发机首配只存姓名
    （线路每次启动现读 config_local.py，改内置地址立刻生效），
    所以空列表不能当成“用户把线路删光了”，设置页本来就拦着不许保存空线路。"""
    if CONFIG_JSON.exists():
        data = _json_data()
        accounts = data.get("accounts") or defaults_accounts()
        return accounts, data.get("user_name", "")
    return defaults_accounts(), ""


_accounts, USER_NAME = _load_local_config()
ACCOUNTS = _normalized_accounts(_accounts)

# ============================================================
# 6.1 口播脚本 AI 检测接口（可选，规范卡配套功能用）
#     不配置也能用：本地规则检测（禁用词/价格口径/必含话术）。
#     配置后「AI 智能检测」会把 脚本+规范卡+风控政策 发给大模型做语义级审查。
#     写法一：config.json 加 "script_check": {"enabled": true, "url": "...", "api_key": "...", "model": "..."}
#     写法二：core/config_local.py 定义 SCRIPT_CHECK = {...}
# ============================================================

def _load_script_check():
    if CONFIG_JSON.exists():
        cfg = _json_data().get("script_check") or {}
        if cfg:
            return cfg
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

# 负载相差 <= 该值的线路视为「一样空」，随机挑一条。三台电脑配置完全相同时，
# 没有随机化就会在同一秒做出同一个决定，一起把同一条线灌爆。
LOAD_TIE_BAND = 1

# 提交前的在途闸门：一条线路云端队列已满（在途 >= concurrency）时，先等它空出来
# 再提交，而不是无脑往里灌。GATE_WAIT_TIMEOUT 秒内等不到就按现状提交（宁可超发
# 也不丢任务），并留一条告警日志。设成 False 可整体退回旧的「提交完即放手」行为。
SUBMIT_GATE = True
GATE_WAIT_TIMEOUT = 900
GATE_POLL_INTERVAL = 5

# 批量提交时两条任务之间的随机间隔（秒）：把多台电脑的同时选线错开
SUBMIT_JITTER = (0.4, 1.2)

# 注水式批量分配：每往各线推 BALANCE_RESCAN_EVERY 条就重扫一次全局快照，
# 把同事新增、以及已被 /jobs 反映出来的本机任务并入（0=整批只在开头扫一次）。
BALANCE_RESCAN_EVERY = 6
# 批量推送时两条之间的固定小间隔（秒）：排队模型下不再靠大抖动错峰，仅防手抖连发。
SUBMIT_PACING = 0.2


# 以上调度参数允许 config.json 同名（小写）覆盖：内测同事改 exe 旁边的
# config.json 就能调，不必改代码重新打包。
def _override_from_config_json():
    global SUBMIT_GATE, GATE_WAIT_TIMEOUT, GATE_POLL_INTERVAL
    global LOAD_TIE_BAND, LOAD_CACHE_TTL, JOBS_LIMIT
    global BALANCE_RESCAN_EVERY, SUBMIT_PACING
    data = _json_data()
    if not data:
        return
    _vars = {"SUBMIT_GATE": "submit_gate", "GATE_WAIT_TIMEOUT": "gate_wait_timeout",
             "GATE_POLL_INTERVAL": "gate_poll_interval", "LOAD_TIE_BAND": "load_tie_band",
             "LOAD_CACHE_TTL": "load_cache_ttl", "JOBS_LIMIT": "jobs_limit",
             "BALANCE_RESCAN_EVERY": "balance_rescan_every", "SUBMIT_PACING": "submit_pacing"}
    g = globals()
    for name, key in _vars.items():
        if key in data:
            g[name] = data[key]


_override_from_config_json()

# ============================================================
# 10. 视频生成参数
# ============================================================
MODE = "r2v"

# 视频时长（秒）：2-15
# 可以通过命令切换：5 秒模式 / 15 秒模式
DEFAULT_DURATION = 5

# AI 生成步数（1-50）：越大细节越好、耗时更长
DEFAULT_STEPS = 8

WIDTH = 768
HEIGHT = 1376
SEED = -1

# 参考图片配置（素材目录可在设置里改，默认运行目录 material/）
MATERIAL_DIR = _resolve_dir("material", str(RUNTIME_DIR / "material"))
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

# ============================================================
# 14. 素材提取接口（聚客 API，工具中心「素材提取」使用）
# ============================================================
# type=dsp 去水印解析 / type=wenan 文案提取。
# 接口地址不入库、不进界面：真实值只写在本机 core/config_local.py
# （已被 .gitignore 忽略，打包 exe 时会被编译进去），戒了“拷仓库就等于拿到接口”。
# 实际生效值优先读 CONFIG_DIR/api_text/api_config.json（工具界面“保存接口配置”写入）。
# 接口返回的直链域名白名单（video/image.txt）不随代码分发，
# 由使用者首次使用工具时导入（见 MaterialPanel 引导）。
MATERIAL_API_BASE = ""
MATERIAL_API_UID = ""
MATERIAL_API_KEY = ""
API_TEXT_DIR = str(CONFIG_DIR / "api_text")   # 接口凭证/白名单：随配置进隐藏目录，独立于可搬移的素材目录

# 允许 config_local 同名覆盖（接口地址/key 变更时不必改代码重打包）
try:
    from core import config_local as _local
    for _k in ("MATERIAL_API_BASE", "MATERIAL_API_UID", "MATERIAL_API_KEY"):
        if hasattr(_local, _k):
            globals()[_k] = getattr(_local, _k)
    del _local
except ImportError:
    pass

# ============================================================
# 15. 机器翻译（火山引擎 文本翻译 MT，任务弹窗「提示词中文对照」用）
#     与提取接口同一套规矩：真实密钥只写本机 core/config_local.py（已 gitignore，
#     打包时编译进 exe），仓库里只留 example 模板；不填也能跑，
#     界面点翻译时会明确提示「未配置翻译密钥」，不静默吞掉。
#     写法一：core/config_local.py 定义 TRANSLATE = {"ak": "...", "sk": "..."}
#     写法二：config.json 加 "translate": {"ak": "...", "sk": "..."}（同事本机自助配置）
# ============================================================

def _load_translate():
    if CONFIG_JSON.exists():
        cfg = _json_data().get("translate") or {}
        if isinstance(cfg, dict) and cfg:
            return cfg
    try:
        from core.config_local import TRANSLATE
        return dict(TRANSLATE)
    except Exception:
        return {}


TRANSLATE = {"ak": "", "sk": "", "region": "cn-north-1", "project": "default",
             **{k: v for k, v in _load_translate().items() if v}}
