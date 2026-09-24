"""
core/config_package.py —— 全量配置的加密导出/导入（.aigccfg 配置迁移包 v2）

为什么要加密：包里是线路地址、接口 key、SMB 共享盘账号、溯源 MySQL 账密、
产品与风控业务数据——明文发给同事，等于谁捡到文件都能刷我们的付费接口。
口令内置在软件里（同事导入零输入）；防的是「传输/落盘途中被人看到」，
不是防拿到 exe 逆向的人（内置口令本来也扛不住，别自欺）。

文件格式（.aigccfg）：
    b"AIGCCFG2" + salt(16B 随机) + Fernet(整个 zip)
    密钥 = PBKDF2-HMAC-SHA256(口令, salt, 210000 轮)
zip 内部：
    manifest.json          {"version":2, "items":[{"id","title"}…], …}
    data/<item_id>/<路径>   各条目自己的文件（文本按 utf-8 存、图片按原始字节存）
（v1 = 明文 JSON 的旧包，只含 3 个文本文件；导入侧兼容读，导出不再产生。）

为什么做成「注册表」：使用者要搬的东西只会越来越多（产品/规范卡/风控/
界面字段/SMB/溯源/命名规则/产品图片…），如果收集逻辑写死在 export 里，
每次加功能都得改迁移代码。现在一个配置面就是一个 Item：
    ITEMS.append(Item(id, title, collect, apply))
collect 返回 {zip 内相对路径: 内容}，apply 负责落盘/写库；
导入按 manifest 逐条处理——本机没登记的条目跳过（新包给老软件用不炸），
包里缺的条目不动（老包给新软件导入不清掉新功能的数据）。
"""
import base64
import io
import json
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.fernet import Fernet

from core.config import CONFIG_DIR, MATERIAL_DIR, RUNTIME_DIR

# 内置口令：导出/导入两端都是本软件，使用者无感。改这行 = 新老包互相打不开
PASSPHRASE = "whosyourdaddy"

MAGIC = b"AIGCCFG2"
MAGIC_V1 = b"AIGCCFG1"          # 旧格式只读兼容
SALT_LEN = 16
KDF_ROUNDS = 210_000
SUFFIX = ".aigccfg"
VERSION = 2

# 产品素材里进包的图片类型（视频/音频素材动辄几百 MB，不随配置包搬）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


# ============================================================
# 加解密信封（v1/v2 共用同一套口令派生）
# ============================================================

def _key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=salt, iterations=KDF_ROUNDS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_bytes(data: bytes, passphrase: str = PASSPHRASE) -> bytes:
    salt = os.urandom(SALT_LEN)
    return MAGIC + salt + Fernet(_key(passphrase, salt)).encrypt(data)


def _decrypt_envelope(blob: bytes, magic: bytes, passphrase: str) -> bytes:
    if not blob.startswith(magic):
        raise ValueError("不是 AIGC 配置包（文件头不对）")
    body = blob[len(magic):]
    if len(body) <= SALT_LEN:
        raise ValueError("配置包不完整（文件可能被截断）")
    try:
        return Fernet(_key(passphrase, body[:SALT_LEN])).decrypt(body[SALT_LEN:])
    except Exception:
        # Fernet 不区分「口令错」和「内容被改」，都报同一个错
        raise ValueError("口令不对或文件已损坏，无法解密")


def decrypt_bytes(blob: bytes, passphrase: str = PASSPHRASE) -> bytes:
    return _decrypt_envelope(blob, MAGIC, passphrase)


# ============================================================
# 条目注册表：加一类配置 = 在这里多登记一条
# ============================================================

@dataclass
class Item:
    id: str                       # zip 内 data/<id>/ 前缀，定死别乱改（新老包要对上）
    title: str                    # 给人看的名字（导入确认框列清单用）
    collect: Callable             # () -> {相对路径: str|bytes}
    apply: Callable               # (files: dict) -> int（写入的对象数）


def _read(path: Path):
    return path.read_text(encoding="utf-8") if path.is_file() else None


# ---------- 条目 1：接口与基础设置（config.json，含线路/命名/检测/翻译/SMB/溯源） ----------

def _collect_config():
    txt = _read(Path(CONFIG_DIR) / "config.json")
    if txt is None:
        raise ValueError("本机还没有 config.json，没有可导出的配置")
    return {"config.json": txt}


def _apply_config(files):
    _write_text(Path(CONFIG_DIR) / "config.json", files["config.json"])
    return 1


# ---------- 条目 2：界面字段与偏好（ui_state.json） ----------

def _collect_ui_state():
    txt = _read(Path(CONFIG_DIR) / "ui_state.json")
    return {"ui_state.json": txt} if txt is not None else {}


def _apply_ui_state(files):
    if "ui_state.json" in files:
        _write_text(Path(CONFIG_DIR) / "ui_state.json", files["ui_state.json"])
        return 1
    return 0


# ---------- 条目 3：素材提取接口凭证与直链白名单（api_text/） ----------

_API_TEXT_ALLOWED = {".json", ".txt"}


def _collect_api_text():
    d = Path(CONFIG_DIR) / "api_text"
    out = {}
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in _API_TEXT_ALLOWED:
                out[f"api_text/{p.name}"] = p.read_text(encoding="utf-8")
    return out


def _apply_api_text(files):
    n = 0
    for name, content in files.items():
        rel = Path(name)
        if rel.parts and rel.parts[0] == "api_text" and len(rel.parts) == 2 \
                and rel.suffix.lower() in _API_TEXT_ALLOWED:
            _write_text(Path(CONFIG_DIR) / name, content)
            n += 1
    return n


# ---------- 条目 4：产品（含规范卡）+ 产品图片 ----------

def _material_root() -> Path:
    return Path(RUNTIME_DIR) / MATERIAL_DIR


def _image_rels(row):
    """把行里位于素材库内且是图片的路径换成相对路径，返回待打包清单"""
    root = _material_root()
    out = []
    for field in ("images", "videos", "audios"):
        parts = [p for p in str(row[field] or "").split(";") if p.strip()]
        kept = []
        for p in parts:
            fp = Path(p)
            try:
                rel = fp.resolve().relative_to(root.resolve())
            except (ValueError, OSError):
                rel = None                   # 素材库外的路径：原样保留，不进包
            if (rel is not None and fp.is_file()
                    and fp.suffix.lower() in IMAGE_EXTS):
                r = str(rel).replace("\\", "/")
                out.append((r, fp))
                kept.append(r)               # 导出行里记相对路径，导入时还原
            else:
                kept.append(p)               # 非图片/已丢失：原字符串留着，不崩图
        row[field] = ";".join(kept)
    return out


def _collect_products():
    from store import db
    rows = [dict(r) for r in db.query("SELECT * FROM products ORDER BY id")]
    packed = []
    for row in rows:
        packed += _image_rels(row)      # 先把行里的图片改写成相对路径再序列化
    files = {"products.json": json.dumps(
        {"rows": rows}, ensure_ascii=False, indent=1).encode("utf-8")}
    for rel, fp in packed:
        files[f"images/{rel}"] = fp.read_bytes()
    return files


def _apply_products(files):
    from store import db
    rows = json.loads(files["products.json"])["rows"]
    root = _material_root()
    # 先把图片落回素材库（zip 里只记「materials/products/x/a.jpg」，装到本机路径）
    for name, content in files.items():
        if name.startswith("images/"):
            target = root / name[len("images/"):]
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                content = content.encode("utf-8")
            target.write_bytes(content)
    # 图片字段的相对路径还原成本机绝对路径（导进包时记的是相对素材库根的路径）
    for row in rows:
        for field in ("images", "videos", "audios"):
            parts = [p for p in str(row.get(field) or "").split(";") if p.strip()]
            row[field] = ";".join(
                str(root / p) if "/" in p and not Path(p).is_absolute()
                else p for p in parts)
    cols = ("id", "type", "name", "images", "videos", "audios", "note", "spec",
            "updated_at")
    conn = db._new_conn()
    try:
        conn.execute("DELETE FROM products")
        for row in rows:
            conn.execute(
                "INSERT INTO products(id,type,name,images,videos,audios,note,spec,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                tuple(row.get(c) for c in cols))
        conn.commit()
    finally:
        conn.close()
    return len(rows)


# ---------- 条目 5：风控政策（平台/产品） ----------

def _collect_risk():
    from store import db
    rows = [dict(r) for r in db.query("SELECT * FROM risk_rules ORDER BY id")]
    return {"risk_rules.json": json.dumps(
        {"rows": rows}, ensure_ascii=False, indent=1).encode("utf-8")}


def _apply_risk(files):
    from store import db
    rows = json.loads(files["risk_rules.json"])["rows"]
    cols = ("id", "scope", "title", "applies", "content", "banned", "active",
            "updated_at")
    conn = db._new_conn()
    try:
        conn.execute("DELETE FROM risk_rules")
        for row in rows:
            conn.execute(
                "INSERT INTO risk_rules(id,scope,title,applies,content,banned,"
                "active,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                tuple(row.get(c) for c in cols))
        conn.commit()
    finally:
        conn.close()
    return len(rows)


ITEMS = [
    Item("text_config", "接口线路 / 命名规则 / 各接口设置（config.json）",
         _collect_config, _apply_config),
    Item("text_ui_state", "任务界面字段与界面偏好（ui_state.json）",
         _collect_ui_state, _apply_ui_state),
    Item("text_api_text", "素材提取接口凭证与直链白名单",
         _collect_api_text, _apply_api_text),
    Item("db_products", "产品与商品规范卡（含产品图片）",
         _collect_products, _apply_products),
    Item("db_risk", "风控政策（平台/产品）", _collect_risk, _apply_risk),
]

ITEMS_BY_ID = {it.id: it for it in ITEMS}


def _write_text(target: Path, content):
    """整文件一次写入（tmp+replace），半截状态别留在盘上；旧文件先备份"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        stamp = time.strftime("%Y%m%d%H%M%S")
        target.replace(target.with_name(f"{target.name}.bak-{stamp}"))
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


# ============================================================
# 导出
# ============================================================

def collect_items():
    """跑一遍注册表 → [(item, files)]；单条 collect 失败不拖垮整包"""
    out = []
    for it in ITEMS:
        try:
            files = it.collect()
        except ValueError:
            raise                       # 「本机还没 config.json」这类要递给人看
        except Exception:
            files = {}                  # 某个条目抽风（库被占用等）：跳过它
        if files:
            out.append((it, files))
    return out


def export_package(dest: str, passphrase: str = PASSPHRASE) -> dict:
    """加密写盘，返回 {"items": [标题…], "files": 文件数}"""
    collected = collect_items()
    buf = io.BytesIO()
    n_files = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {"app": "aigc", "version": VERSION,
                    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "items": [{"id": it.id, "title": it.title} for it, _ in collected]}
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for it, files in collected:
            for name, content in files.items():
                if isinstance(content, str):
                    content = content.encode("utf-8")
                zf.writestr(f"data/{it.id}/{name}", content)
                n_files += 1
    Path(dest).write_bytes(encrypt_bytes(buf.getvalue(), passphrase))
    return {"items": [it.title for it, _ in collected], "files": n_files}


# ============================================================
# 导入
# ============================================================

def read_package(src: str, passphrase: str = PASSPHRASE) -> dict:
    """解密并解析 → {"manifest":…, "items": {id: {name: content}}}；错则抛 ValueError"""
    blob = Path(src).read_bytes()
    if blob.startswith(MAGIC_V1):
        return _read_v1(blob, passphrase)
    try:
        raw = _decrypt_envelope(blob, MAGIC, passphrase)
    except ValueError:
        raise
    except Exception:
        raise ValueError("口令不对或文件已损坏，无法解密")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("配置包内容已损坏（zip 解不开）")
    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except KeyError:
            raise ValueError("配置包缺少 manifest，无法识别内容")
        items = {}
        for info in zf.infolist():
            rel = info.filename
            if not rel.startswith("data/") or rel.endswith("/"):
                continue
            parts = rel.split("/", 2)       # data/<item>/<name…>
            if len(parts) != 3:
                continue
            items.setdefault(parts[1], {})[parts[2]] = zf.read(rel)
    return {"manifest": manifest, "items": items}


def _read_v1(blob, passphrase):
    """v1 旧包（只含文本文件）：折算成 v2 的 text_config 等条目，别让旧文件成砖"""
    try:
        payload = json.loads(_decrypt_envelope(blob, MAGIC_V1, passphrase).decode("utf-8"))
    except ValueError:
        raise
    files = payload.get("files") or {}
    if "config.json" not in files:
        raise ValueError("旧配置包是空的或版本太老")
    grouped = {}
    for name, content in files.items():
        if name == "config.json":
            grouped.setdefault("text_config", {})[name] = content
        elif name == "ui_state.json":
            grouped.setdefault("text_ui_state", {})[name] = content
        elif name.startswith("api_text/"):
            grouped.setdefault("text_api_text", {})[name] = content
    return {"manifest": {"version": 1, "items": [
                {"id": k, "title": ITEMS_BY_ID[k].title} for k in grouped],
                "exported_at": payload.get("exported_at", "（旧版包）")},
        "items": grouped}


def plan_import(pkg: dict):
    """把包里的条目按本机注册表分成：能导入的 / 本机不认识的（跳过）"""
    known, unknown = [], []
    for meta in pkg["manifest"].get("items") or []:
        iid = str(meta.get("id"))
        if iid in ITEMS_BY_ID and iid in pkg["items"]:
            known.append((ITEMS_BY_ID[iid], meta.get("title") or iid))
        else:
            unknown.append(meta.get("title") or iid)
    return known, unknown


def apply_package(pkg: dict) -> dict:
    """逐条目落盘/写库 → {"applied": [标题…], "skipped": […], "counts": {id: n}}"""
    known, unknown = plan_import(pkg)
    applied, counts = [], {}
    for it, _title in known:
        files = {k: (v.decode("utf-8") if isinstance(v, bytes) and
                     _is_text(it.id, k) else v)
                 for k, v in pkg["items"][it.id].items()}
        counts[it.id] = it.apply(files)
        applied.append(it.title)
    return {"applied": applied, "skipped": unknown, "counts": counts}


def _is_text(item_id: str, name: str) -> bool:
    """哪些文件按文本解码回 str（DB 行/图片按 bytes 处理，各自协议自己清楚）"""
    return item_id in ("text_config", "text_ui_state", "text_api_text")


def import_package(src: str, passphrase: str = PASSPHRASE) -> dict:
    return apply_package(read_package(src, passphrase))
