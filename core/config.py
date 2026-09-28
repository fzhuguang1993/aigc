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

# ---------- 爆款拆解回写列（工具中心「爆款拆解」写入任务表新增列） ----------
COL_BK_LINK = "参考链接"
COL_BK_HOOK = "拆解钩子"
COL_BK_SHOTS = "拆解分镜"
COL_BK_SCRIPT = "拆解口播"
COL_BK_PROMPT_VISUAL = "画面提示词"
COL_BK_PROMPT_COPY = "文案提示词"
COL_BK_PROMPT_SHOOT = "复刻提示词"
COL_BK_HOOK_SCORE = "钩子评分"
COL_BK_FACTORS = "爆点因素"
COL_BK_EMOTION = "情绪曲线"
COL_BK_FORMULA = "内容公式"
COL_BK_BLUEPRINT = "复刻蓝图"

# 爆款拆解全部回写列（excel_utils 补列 / 写回共用这一份清单）
BREAKDOWN_COLUMNS = [
    COL_BK_LINK, COL_BK_HOOK, COL_BK_SHOTS, COL_BK_SCRIPT,
    COL_BK_PROMPT_VISUAL, COL_BK_PROMPT_COPY, COL_BK_PROMPT_SHOOT,
    COL_BK_HOOK_SCORE, COL_BK_FACTORS, COL_BK_EMOTION,
    COL_BK_FORMULA, COL_BK_BLUEPRINT,
]

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
# 3.1 爆款拆解：Whisper 模型目录与临时工作目录
#     模型不打包进 exe，首次运行下到隐藏配置家（%APPDATA%\AIGC视频助手\models）；
#     拆解中间产物（下载的视频/关键帧/音频）落在运行目录 outputs/爆款拆解，用完即清。
# ============================================================
MODELS_DIR = str(CONFIG_DIR / "models")
BREAKDOWN_TMP = str(RUNTIME_DIR / "outputs" / "爆款拆解")
# 拆解任务库：图集/封面持久落盘的家（跨会话回看，不随 BREAKDOWN_TMP 用完即清）。
# 每条任务一个 task_<id>/ 子目录，内含 cover.jpg + gallery/f*.jpg；DB 只存路径与 payload。
BREAKDOWN_LIBRARY = str(RUNTIME_DIR / "outputs" / "爆款拆解" / "任务库")

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
    base 为空的条目本身就没法用，直接丢掉（与 gui.pages_api.parse_lines 同口径）。"""
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


def _load_gateway_base():
    """网关根地址：core/config_local.py 的 GATEWAY_BASE（打包进 exe，不进仓库）。
    配了就补 /api/v1 尾巴；没配返回空串 = 开发/内网直连模式。"""
    try:
        from core.config_local import GATEWAY_BASE as _G
    except (ImportError, AttributeError):
        return ""
    g = str(_G or "").strip().rstrip("/")
    if g and not g.endswith("/api/v1"):
        g += "/api/v1"
    return g


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
    accounts = _normalized_accounts([a for a in list(_A) if not _is_placeholder(a)])
    if not accounts:
        # 商用网关包：内置没配直连线路、但配了 GATEWAY_BASE → 回退单条“云端网关”，
        # 首配向导只需问姓名（与 gui.pages_api / setup_wizard 同口径）。
        g = _load_gateway_base()
        if g:
            accounts = _normalized_accounts(
                [{"name": "云端网关", "base": g, "concurrency": 1}])
    return accounts


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
# 6.2 商用网关模式（打包内置：core/config_local.py 写 GATEWAY_BASE）
#     配了就切到「激活 + 网关」形态：本地线路表整体作废，只剩一条指向网关的线；
#     上游地址/密钥从此不出服务器，选线/排队/探活全部由网关完成。
#     不配 = 开发/内网直连模式，一切维持旧行为（维护机自用、本地测试）。
# ============================================================
APP_VERSION = "2.0.0"


GATEWAY_BASE = _load_gateway_base()
GATEWAY_MODE = bool(GATEWAY_BASE)

if GATEWAY_MODE:
    try:
        from core.config_local import GATEWAY_CONCURRENCY as _GC
    except (ImportError, AttributeError):
        _GC = 999          # 客户端不再拦在途闸门：排队调度由网关统一负责
    ACCOUNTS = _normalized_accounts(
        [{"name": "云端网关", "base": GATEWAY_BASE, "concurrency": int(_GC)}])

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

# ============================================================
# 16. 通用「段」读写：config.json 的一个顶层键 = 一个功能的配置
#     SMB 上传、视频溯源 MySQL 等功能用它把界面填的连接信息落盘；
#     后续新功能要进「配置包」，配置存进 config.json 的自己的段里、
#     用这两个函数读写即可——配置迁移包（core/config_package.py）
#     整体带着 config.json 走，不用再单独登记文件。
#     写回是「读旧 dict → 只换自己那段 → 整份合并写」，不会伤及
#     accounts/filename 等其它段；写完刷新启动缓存，面板存完即生效。
# ============================================================

def read_section(key, defaults=None):
    """读 config.json 的某段，用内置默认值兜底（空段/缺段=全默认）"""
    val = _json_data().get(key)
    if not isinstance(val, dict):
        val = {}
    return {**(defaults or {}), **val}


def write_section(key, value):
    """把某段合并写回 config.json（原子写 + 刷新缓存）"""
    data = dict(_json_data())
    data[key] = value
    CONFIG_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_JSON.with_name(CONFIG_JSON.name + ".tmp")
    tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(CONFIG_JSON)           # 同盘 rename：不留半截文件
    global _JSON_CACHE
    _JSON_CACHE = data                 # 启动缓存跟着换，别读旧值


# ============================================================
# 17. 豆包（火山方舟 Vision）接入点配置：爆款拆解专用
#     与提取接口/翻译同一套规矩：api_key 以使用人姓名(USER_NAME)为盐 Fernet
#     加密后写进 config.json 的 doubao_vision 段（复用 material_extract 的加解密），
#     盘上不留明文；未配姓名时退回明文兜底。占位值（<…>）视为未配置。
# ============================================================
DOUBAO_VISION_DEFAULTS = {
    "api_key": "", "endpoint": "",
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
}


def _is_doubao_placeholder(v):
    v = str(v or "").strip()
    return not v or v.startswith("<")


def _strip_bearer(v):
    """剥掉误粘进 key 的 "Bearer " 前缀（大小写不敏感、允许多个）。

    请求头由代码自己拼 'Authorization: Bearer {key}'；用户把整段头
    ＂Bearer ark-xxx＂粘进输入框会变成 Bearer Bearer ark-xxx，服务端回
    401 "The API key format is incorrect"——在读写两侧统一清洗。
    """
    s = str(v or "").strip()
    while s[:7].lower() == "bearer ":
        s = s[7:].strip()
    return s


def doubao_vision_config():
    """生效的豆包接入点配置：api_key 密文用 USER_NAME 解密；占位/未配一律置空。"""
    from video_text_tools.material_extract import decrypt_value
    sec = read_section("doubao_vision", DOUBAO_VISION_DEFAULTS)
    key = str(sec.get("api_key") or "").strip()
    if key:
        key = decrypt_value(key, USER_NAME) if key.startswith("enc:") else key
    key = _strip_bearer(key)
    base = str(sec.get("base_url") or DOUBAO_VISION_DEFAULTS["base_url"]).strip()
    return {
        "api_key": key if not _is_doubao_placeholder(key) else "",
        "endpoint": "" if _is_doubao_placeholder(sec.get("endpoint")) else str(sec["endpoint"]).strip(),
        "base_url": base.rstrip("/") or DOUBAO_VISION_DEFAULTS["base_url"],
    }


def doubao_vision_ready():
    """是否已配齐（api_key + endpoint 都在）：GUI 首次引导据此提示。"""
    c = doubao_vision_config()
    return bool(c["api_key"] and c["endpoint"])


def save_doubao_vision(api_key, endpoint, base_url=None):
    """写回 doubao_vision 段：api_key 以 USER_NAME 为盐加密落盘（无姓名则明文兜底）。"""
    from video_text_tools.material_extract import encrypt_value
    sec = dict(DOUBAO_VISION_DEFAULTS)
    sec.update({
        "api_key": encrypt_value(_strip_bearer(api_key), USER_NAME),
        "endpoint": (endpoint or "").strip(),
    })
    if base_url:
        sec["base_url"] = base_url.strip().rstrip("/")
    write_section("doubao_vision", sec)


# ============================================================
# 17.2 爆款拆解面板默认项（config.json 的 breakdown 段）
#     vision_mode：video_url 直连（默认，整条视频一次调用、规避逐帧 429）
#                 / frames 本地抽帧逐帧上传（传统兜底）。
#     fps：video_url 直连时模型内部抽帧频率（方舟范围 [0.2,5]，默认 0.5）。
#     GUI 面板初值从这里读；要改直接编辑 config.json 的 breakdown 段。
# ============================================================
BREAKDOWN_DEFAULTS = {"vision_mode": "video_url", "fps": 0.5}


def breakdown_config():
    """拆解面板默认项（清洗：模式只认 video_url/frames，fps 钳在 [0.2,5]）。"""
    sec = read_section("breakdown", BREAKDOWN_DEFAULTS)
    mode = str(sec.get("vision_mode") or "video_url").strip()
    try:
        fps = float(sec.get("fps", 0.5))
    except (TypeError, ValueError):
        fps = 0.5
    return {"vision_mode": mode if mode in ("video_url", "frames") else "video_url",
            "fps": min(5.0, max(0.2, fps))}


# ============================================================
# 17.5 语音纠错（DeepSeek 等 OpenAI 兼容 chat 端点）+ 领域词库
#     asr_fix 段：识别后送大模型按语义改错别字/同音字。规矩与豆包完全一致：
#       api_key 以 USER_NAME 为盐 Fernet 加密写盘（复用 material_extract 加解密，
#       盘上不留明文；无姓名时明文兜底），占位值（<…>）视为未配置。
#     asr_glossary 段：领域正确写法词表（字符串列表）。一份两用——实时识别喂
#       whisper initial_prompt、纠错时作为“这些词的正确写法”清单送进 prompt。
# ============================================================
ASR_FIX_DEFAULTS = {
    "api_key": "", "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
}


def asr_fix_config():
    """生效的语音纠错端点配置：api_key 密文用 USER_NAME 解密；占位/未配一律置空。"""
    from video_text_tools.material_extract import decrypt_value
    sec = read_section("asr_fix", ASR_FIX_DEFAULTS)
    key = str(sec.get("api_key") or "").strip()
    if key:
        key = decrypt_value(key, USER_NAME) if key.startswith("enc:") else key
    key = _strip_bearer(key)
    base = str(sec.get("base_url") or ASR_FIX_DEFAULTS["base_url"]).strip()
    return {
        "api_key": key if not _is_doubao_placeholder(key) else "",
        "model": "" if _is_doubao_placeholder(sec.get("model")) else str(sec["model"]).strip(),
        "base_url": base.rstrip("/") or ASR_FIX_DEFAULTS["base_url"],
    }


def asr_fix_ready():
    """是否已配齐（api_key + model 都在）：GUI 据此决定“纠错”能否发请求。"""
    c = asr_fix_config()
    return bool(c["api_key"] and c["model"])


def save_asr_fix(api_key, model=None, base_url=None):
    """写回 asr_fix 段：api_key 以 USER_NAME 为盐加密落盘（无姓名则明文兜底）。"""
    from video_text_tools.material_extract import encrypt_value
    sec = dict(ASR_FIX_DEFAULTS)
    sec["api_key"] = encrypt_value(_strip_bearer(api_key), USER_NAME)
    if model:
        sec["model"] = model.strip()
    if base_url:
        sec["base_url"] = base_url.strip().rstrip("/")
    write_section("asr_fix", sec)


def _normalize_glossary(terms):
    """词库清洗：去首尾空白、去空、去重、保序。"""
    out, seen = [], set()
    for t in terms or []:
        s = str(t or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def asr_glossary_load():
    """读 config.json 的 asr_glossary 段（每次现解析，不吃启动缓存）；返回拷贝。"""
    v = _json_data().get("asr_glossary")
    return _normalize_glossary(v if isinstance(v, list) else [])


def asr_glossary_save(terms):
    """整体替换词库（去重清洗后写回）；返回清洗后的列表。"""
    terms = _normalize_glossary(terms)
    write_section("asr_glossary", terms)
    return terms


def _iter_string_leaves(obj):
    """递归收集 JSON 结构里的所有字符串叶子（list[str] / list[dict] / dict 都能吃到词）。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_string_leaves(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _iter_string_leaves(v)


def glossary_from_file(path):
    """从外部词表文件读词库（Excel / CSV / JSON 三格式），返回清洗去重保序后的列表。

    - Excel(.xlsx/.xls) / CSV：整表逐格取非空值——单列清单＝逐行一个词，多列也一并收下；
    - JSON：递归收集所有字符串叶子（["钙片",...]、[{"词":"钙片"}]、{"terms":[...]} 都吃）。
    pandas 只在用到本函数时才现引入（精简包不装也不影响其它功能）。
    """
    ext = Path(path).suffix.lower()
    raw = []
    if ext == ".json":
        data = _json.loads(Path(path).read_text(encoding="utf-8-sig"))
        raw = list(_iter_string_leaves(data))
    elif ext in (".xlsx", ".xls", ".csv"):
        import pandas as pd
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(path, header=None, dtype=str)
        else:
            df = None
            # 中文单列清单下嗅探（sep=None）实测必炸「bad delimiter value」（pandas 2.3）：
            # 显式分隔符优先，嗅探只给奇葩分隔符的文件兜底；编码仍 UTF-8/GBK 两轮。
            for enc in ("utf-8-sig", "gbk", None):    # 中文 CSV 常见 UTF-8/GBK 两种
                for sep in (",", ";", "\t", None):
                    kw = {"header": None, "sep": sep, "engine": "python",
                          "dtype": str}
                    try:
                        df = (pd.read_csv(path, encoding=enc, **kw) if enc
                              else pd.read_csv(path, **kw))
                        break
                    except (UnicodeDecodeError, LookupError, ValueError):
                        continue
                if df is not None:
                    break
            if df is None:
                raise ValueError("CSV 无法解析（试过 UTF-8/GBK 与逗号/分号/Tab 分隔），"
                                 "请另存为 UTF-8 或改用 Excel/JSON")
        for col in df.columns:
            for v in df[col]:
                s = "" if v is None else str(v).strip()
                if s and s.lower() != "nan":
                    raw.append(s)
    else:
        raise ValueError(f"不支持的词库文件格式：{ext or '(无扩展名)'}（请用 .xlsx/.xls/.csv/.json）")
    return _normalize_glossary(raw)


def glossary_to_file(path, terms):
    """把词库写成外部文件（.xlsx / .csv / .json）；单列、逐行一个词（不带表头，
    免导回来时把表头当成一个词）。返回清洗后的列表。"""
    terms = _normalize_glossary(terms)
    ext = Path(path).suffix.lower()
    if ext == ".json":
        Path(path).write_text(_json.dumps(terms, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    elif ext in (".xlsx", ".xls", ".csv"):
        import pandas as pd
        df = pd.DataFrame({"词库": terms})
        if ext == ".csv":
            df.to_csv(path, index=False, header=False, encoding="utf-8-sig")
        else:
            df.to_excel(path, index=False, header=False)
    else:
        raise ValueError(f"不支持的导出格式：{ext or '(无扩展名)'}（请用 .xlsx/.csv/.json）")
    return terms


# ============================================================
# 18. 一键发布：平台端点可变配置 + 各平台账号凭证
#     publish 段：抖音等平台的端点地址/UA/签名参数名等**可变项**（web 内部端点会随
#       平台改版变动，改配置不改代码）。一期只铺抖音，其余平台端点后期补。
#     publish_accounts 段：一个列表，每条一个平台账号；凭证（cookie 串 / OAuth token
#       字典）以使用人姓名(USER_NAME)为盐 Fernet 加密后存 secret_enc（复用 material_extract
#       的加解密，盘上不留明文；无姓名时明文兜底）。读写走 publish/accounts.py 门面。
# ============================================================
PUBLISH_DEFAULTS = {
    "douyin": {
        # 创作者中心 web 内部端点：真实值后期填（占位 <...> 会被适配器判为"未配置"）
        "upload_init_url": "",   # 申请上传（POST 视频元信息 → 返回直链 + video_uri）
        "create_url": "",        # 创建发布（POST 标题/简介/标签/封面 + video_uri → aweme_id）
        "user_info_url": "",     # 登录态探测（可留空：留空则跳过在线校验）
        "post_url_base": "https://www.douyin.com/video/",
        "user_agent": "",
        "referer": "https://creator.douyin.com/",
        "sign_params": {},       # a_bogus / msToken / verifyFp 等反爬参数名→值来源
    },
}


def publish_config():
    """一键发布的平台可变配置（config.json 的 publish 段，缺省回退内置默认）。"""
    sec = read_section("publish", {})
    out = {k: dict(v) for k, v in PUBLISH_DEFAULTS.items()}
    for plat, vals in (sec or {}).items():
        if isinstance(vals, dict):
            out.setdefault(plat, {}).update(vals)
    return out


def _publish_accounts_raw():
    v = _json_data().get("publish_accounts")
    return v if isinstance(v, list) else []


def list_publish_accounts():
    """全部发布账号，secret 已解密回明文 dict（供 publish.accounts 组装 Account）。

    每条：{id, platform, label, auth_type, secret(dict), extra(dict)}。
    secret_enc 解不开（姓名不符/损坏）时该条 secret 置空，按未配置处理，不抛。"""
    from video_text_tools.material_extract import decrypt_value
    out = []
    for e in _publish_accounts_raw():
        if not isinstance(e, dict):
            continue
        enc = str(e.get("secret_enc") or "")
        plain = decrypt_value(enc, USER_NAME) if enc else ""
        try:
            secret = _json.loads(plain) if plain else {}
        except ValueError:
            secret = {}
        if not isinstance(secret, dict):
            secret = {}
        out.append({
            "id": str(e.get("id") or ""),
            "platform": str(e.get("platform") or ""),
            "label": str(e.get("label") or ""),
            "auth_type": str(e.get("auth_type") or "cookie"),
            "secret": secret,
            "extra": e.get("extra") if isinstance(e.get("extra"), dict) else {},
        })
    return out


def save_publish_account(acct):
    """按 id upsert 一个账号（acct 为 dict：id/platform/label/auth_type/secret/extra）。

    secret（dict）序列化后以 USER_NAME 为盐加密写 secret_enc；无姓名则明文兜底。
    id 为空时自动生成。返回最终 id。"""
    from video_text_tools.material_extract import encrypt_value
    import uuid
    acct_id = str(acct.get("id") or "") or uuid.uuid4().hex[:12]
    secret = acct.get("secret") or {}
    blob = _json.dumps(secret, ensure_ascii=False)
    entry = {
        "id": acct_id,
        "platform": str(acct.get("platform") or ""),
        "label": str(acct.get("label") or ""),
        "auth_type": str(acct.get("auth_type") or "cookie"),
        "secret_enc": encrypt_value(blob, USER_NAME),
        "extra": acct.get("extra") if isinstance(acct.get("extra"), dict) else {},
    }
    items = [e for e in _publish_accounts_raw() if isinstance(e, dict)]
    items = [e for e in items if str(e.get("id") or "") != acct_id]
    items.append(entry)
    write_section("publish_accounts", items)
    return acct_id


def delete_publish_account(acc_id):
    """按 id 删除一个账号；返回是否删掉了。"""
    items = [e for e in _publish_accounts_raw() if isinstance(e, dict)]
    kept = [e for e in items if str(e.get("id") or "") != str(acc_id)]
    if len(kept) == len(items):
        return False
    write_section("publish_accounts", kept)
    return True


# ============================================================
# 19. 录屏字幕：默认样式与处理参数（无新增凭证）
#     subtitle 段：默认模型档位 + 字幕基础样式（前景/描边/位置/卖点强调色…）+
#       烧录产物后缀。Whisper 模型目录复用 MODELS_DIR，卖点高亮/字幕检测复用
#       doubao_vision 段——都不在这里另存密钥。可变项走界面/本段配置，改配不改代码。
# ============================================================
SUBTITLE_DEFAULTS = {
    "model_size": "medium",
    "output_suffix": "_字幕",
    "highlight_enabled": False,
    "default_style": {
        "font_size": 16, "primary": "#FFFFFF", "outline": "#000000",
        "back": "#00000000", "align": "bottom", "margin_v": 40,
        "highlight": "#FFD400", "max_chars": 18,
    },
}


def subtitle_config():
    """生效的录屏字幕配置（config.json 的 subtitle 段，缺省回退内置默认）。

    default_style 做一层深合并：用户只改某几个样式键时，其余仍走默认。"""
    sec = read_section("subtitle", {})
    out = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in SUBTITLE_DEFAULTS.items()}
    for k, v in (sec or {}).items():
        if k == "default_style" and isinstance(v, dict):
            out["default_style"].update(v)
        else:
            out[k] = v
    return out
