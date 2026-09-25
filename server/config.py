"""server/config.py —— 网关服务端配置

凭证与调度参数落在 server_config.json（不进仓库，模板见 server_config.example.json），
环境变量（AIGC_GATE_*）优先级更高，方便 systemd 里直接覆盖。
真实上游线路/提取/翻译凭证在 upstreams.json（见 server/upstream.py），两者都不许进 git。
"""
import json
import os
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent


def _data_dir():
    """运行数据目录：SQLite、上传暂存、日志。可用 AIGC_GATE_DATA 指到别处。"""
    env = os.environ.get("AIGC_GATE_DATA")
    return Path(env) if env else SERVER_DIR / "data"


_FILE = {}
_cfg_path = os.environ.get("AIGC_GATE_CONFIG")
_CONFIG_FILE = Path(_cfg_path) if _cfg_path else SERVER_DIR / "server_config.json"
if _CONFIG_FILE.exists():
    try:
        _loaded = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(_loaded, dict):
            _FILE = _loaded
    except Exception:
        print(f"⚠ {_CONFIG_FILE} 解析失败，按空配置启动（密钥缺失会拒绝签发）")


def _get(key, default=""):
    """env AIGC_GATE_<KEY 大写> > server_config.json > 默认值"""
    env = os.environ.get(f"AIGC_GATE_{key.upper()}")
    if env is not None and env != "":
        return env
    v = _FILE.get(key)
    return default if v is None or v == "" else v


#: HMAC 令牌密钥：没有它一切鉴权都无意义，缺失时激活接口直接 500 拒绝服务
SECRET = str(_get("secret"))

DATA_DIR = _data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "gate.db")

#: 上游线路池 / 素材提取 / 翻译凭证（真实地址只在这份文件里，绝不进仓库）
UPSTREAMS_FILE = str(_get("upstreams_file", str(SERVER_DIR / "upstreams.json")))

#: 管理端口令：留空 = /admin/* 整体关闭
ADMIN_TOKEN = str(_get("admin_token"))

# ---------------- 令牌与防重放 ----------------
REPLAY_WINDOW = 300          # 请求时间戳允许偏差（秒）
NONCE_TTL = 600              # nonce 记忆时长（秒），必须 >= REPLAY_WINDOW
OFFLINE_GRACE_DAYS = 3       # 客户端离线宽限（随激活响应下发，客户端照此执行）

# ---------------- 激活防刷 ----------------
ACTIVATE_RATE_PER_MIN = 10   # 单 IP 每分钟激活尝试上限
CARD_FAIL_LIMIT = 5          # 单卡密连续失败次数上限
CARD_FAIL_WINDOW = 3600      # 失败计数窗口（秒）

# ---------------- 上游探活与调度 ----------------
HEALTH_CHECK_INTERVAL = 30
HEALTH_FAIL_THRESHOLD = 3
JOBS_LIMIT = 100

# ---------------- 上传暂存 ----------------
ASSET_SPOOL_DIR = str(DATA_DIR / "spool")
ASSET_TTL = 3600             # 暂存文件保留时长（秒）：提交前参考图的存活窗口

# ---------------- 工具类接口频控（按机器码，防单用户刷爆） ----------------
TOOL_RATE_PER_MIN = 30       # 素材提取/翻译：每机器码每分钟请求上限

Path(ASSET_SPOOL_DIR).mkdir(parents=True, exist_ok=True)
