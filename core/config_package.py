"""
core/config_package.py —— 全量配置的加密导出/导入（.aigccfg 配置迁移包）

为什么要加密：包里是 config.json（线路地址、各接口 key、命名规则）和
api_text/（素材提取接口凭证、直链白名单）——明文发给同事，等于谁捡到文件
都能刷我们的付费接口。口令内置在软件里，同事导入时不用输任何东西；
我们防的是「传输/落盘途中被人看到」，不是防拿到 exe 逆向的人（那是另一个量级
的对抗，内置口令本来也扛不住，别自欺）。

文件格式（.aigccfg）：
    b"AIGCCFG1" + salt(16B 随机) + Fernet 密文
    密钥 = PBKDF2-HMAC-SHA256(口令, salt, 210000 轮)
每包独立随机 salt：同一个口令导出的两个文件密文不同；21 万轮把离线
爆破单包的成本顶到秒级以下别想——当然口令本身别太弱。

导入安全：包内文件名走白名单（只认 config.json / ui_state.json /
api_text/*.json|txt，拒绝路径穿越），不能做一个包把你家任意路径写花。
覆盖前旧配置逐个备份成 *.bak-时间戳，导入翻车还能手动滚回去。
"""
import base64
import json
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.fernet import Fernet

from core.config import CONFIG_DIR

# 内置口令：导出/导入两端都是本软件，使用者无感。改这行 = 新老包互相打不开
PASSPHRASE = "whosyourdaddy"

MAGIC = b"AIGCCFG1"
SALT_LEN = 16
KDF_ROUNDS = 210_000
SUFFIX = ".aigccfg"

# 允许进包/落盘的文件（相对 CONFIG_DIR）：精确名单 + 目录前缀
ALLOWED_FILES = {"config.json", "ui_state.json"}
ALLOWED_DIRS = {          # 前缀 → 允许的扩展名
    "api_text/": {".json", ".txt"},
}


def _key(passphrase: str, salt: bytes) -> bytes:
    """口令 → Fernet 密钥（PBKDF2 拉伸，输出 32B 做 urlsafe_b64）"""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=salt, iterations=KDF_ROUNDS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_bytes(data: bytes, passphrase: str = PASSPHRASE) -> bytes:
    salt = os.urandom(SALT_LEN)
    token = Fernet(_key(passphrase, salt)).encrypt(data)
    return MAGIC + salt + token


def decrypt_bytes(blob: bytes, passphrase: str = PASSPHRASE) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("不是 AIGC 配置包（文件头不对）")
    body = blob[len(MAGIC):]
    if len(body) <= SALT_LEN:
        raise ValueError("配置包不完整（文件可能被截断）")
    salt, token = body[:SALT_LEN], body[SALT_LEN:]
    try:
        return Fernet(_key(passphrase, salt)).decrypt(token)
    except Exception:
        # Fernet 的 InvalidToken 不区分「口令错」和「内容被改」，都报同一个错
        raise ValueError("口令不对或文件已损坏，无法解密")


def _allowed(name: str) -> bool:
    """包内路径白名单：防恶意包往任意路径写东西"""
    if name.startswith(("/", "\\")) or ".." in Path(name).parts:
        return False
    if name in ALLOWED_FILES:
        return True
    for prefix, exts in ALLOWED_DIRS.items():
        if name.startswith(prefix) and Path(name).suffix.lower() in exts:
            # 只许 api_text/ 下一层，别收纳子目录里的东西
            if "/" in name[len(prefix):]:
                continue
            return True
    return False


def collect_files(config_dir: Path = None) -> dict:
    """收集本机全部配置 → {相对路径: 文本内容}（没写的键跳过）"""
    root = Path(config_dir or CONFIG_DIR)
    files = {}
    for name in sorted(ALLOWED_FILES):
        p = root / name
        if p.is_file():
            files[name] = p.read_text(encoding="utf-8")
    for prefix, exts in ALLOWED_DIRS.items():
        d = root / prefix.rstrip("/")
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file() and p.suffix.lower() in exts:
                    files[f"{prefix}{p.name}"] = p.read_text(encoding="utf-8")
    if "config.json" not in files:
        raise ValueError("本机还没有 config.json，没有可导出的配置")
    return files


def export_package(dest: str, config_dir: Path = None) -> int:
    """把本机全部配置加密写到 dest，返回装入的文件数"""
    payload = {
        "app": "aigc",
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": collect_files(config_dir),
    }
    blob = encrypt_bytes(json.dumps(payload, ensure_ascii=False)
                         .encode("utf-8"))
    Path(dest).write_bytes(blob)
    return len(payload["files"])


def read_package(src: str, passphrase: str = PASSPHRASE) -> dict:
    """解密配置包，返回 {相对路径: 文本内容}；口令错/坏文件抛 ValueError"""
    blob = Path(src).read_bytes()
    try:
        payload = json.loads(decrypt_bytes(blob, passphrase).decode("utf-8"))
    except ValueError:
        raise                  # decrypt_bytes 的人话错误原样递
    except Exception:
        raise ValueError("配置包内容不是合法的 AIGC 配置数据")
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict) or "config.json" not in files:
        raise ValueError("配置包是空的（或版本太老，缺少 config.json）")
    bad = [k for k in files if not _allowed(str(k))]
    if bad:
        raise ValueError(f"配置包里有不允许的文件名，已拒绝导入：{bad[:3]}")
    return files


def apply_package(files: dict, config_dir: Path = None) -> int:
    """把包内文件落到 CONFIG_DIR，旧文件先备份成 *.bak-时间戳；返回写入数"""
    root = Path(config_dir or CONFIG_DIR)
    stamp = time.strftime("%Y%m%d%H%M%S")
    written = 0
    for name, content in files.items():
        if not _allowed(str(name)):        # 双保险：就算调用方直接喂也拦
            continue
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            target.replace(target.with_name(f"{target.name}.bak-{stamp}"))
        # 与 _save/atom 写法同口径：整文件一次写入，半截状态别留在盘上
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
        written += 1
    return written


def import_package(src: str, passphrase: str = PASSPHRASE,
                   config_dir: Path = None) -> int:
    """一步到位：解密 + 落盘，返回写入文件数（异常由调用方弹给用户）"""
    return apply_package(read_package(src, passphrase), config_dir)
