"""
store/product_store.py —— 产品中心：品名/KOL 及其素材（图片/视频/音频）
素材文件统一复制到 material/products/<名称>/ 下管理；
提交任务时按品名自动取该产品参考图，KOL 按名称取形象图。
"""
import re
import shutil
from datetime import datetime
from pathlib import Path

from core.config import RUNTIME_DIR, MATERIAL_DIR, KOL_DIR, REFERENCE_IMAGES, KOL_OPTIONS
from store import db

TYPE_PRODUCT = "product"
TYPE_KOL = "kol"
_KIND_FIELDS = {"image": "images", "video": "videos", "audio": "audios"}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _split(s):
    return [p for p in (s or "").split(";") if p.strip()]


def product_dir(name):
    safe = re.sub(r'[\\/:*?"<>|]', "_", name or "未命名").strip() or "未命名"
    d = Path(RUNTIME_DIR) / MATERIAL_DIR / "products" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------- 查询 ----------------

def list_products(ptype=None):
    if ptype:
        return db.query("SELECT * FROM products WHERE type=? ORDER BY name", (ptype,))
    return db.query("SELECT * FROM products ORDER BY type, name")


def get_product(pid):
    rows = db.query("SELECT * FROM products WHERE id=?", (pid,))
    return rows[0] if rows else None


def get_by_name(name, ptype=TYPE_PRODUCT):
    rows = db.query("SELECT * FROM products WHERE type=? AND name=?", (ptype, name))
    return rows[0] if rows else None


def product_names():
    return [r["name"] for r in list_products(TYPE_PRODUCT)]


def kol_names():
    """KOL 下拉选项：产品中心优先，兼容旧 config.KOL_OPTIONS 硬编码"""
    names = [r["name"] for r in list_products(TYPE_KOL)]
    for n in KOL_OPTIONS:
        if n not in names:
            names.append(n)
    return names


# ---------------- 增删改 ----------------

def add_product(name, ptype=TYPE_PRODUCT, note=""):
    return db.execute(
        "INSERT INTO products(type,name,note,updated_at) VALUES(?,?,?,?)",
        (ptype, name, note, _now()))


def delete_product(pid):
    db.execute("DELETE FROM products WHERE id=?", (pid,))


def set_note(pid, note):
    db.execute("UPDATE products SET note=?, updated_at=? WHERE id=?", (note, _now(), pid))


# ---------------- 规范卡 ----------------
# 模板字段：新建产品时展示占位提示，填写后存 JSON 到 products.spec
SPEC_FIELDS = [
    ("卖什么套餐", "例：3盒疗程装 + 送1盒体验装；单盒/双盒/疗程装分别什么内容"),
    ("价格口径", "例：日常价89元/盒，活动到手价59元/盒；口播中只能出现这几个价"),
    ("活动口径", "例：仅限直播间下单，前100名加赠；活动截止 x月x日；不支持无理由退款"),
    ("核心卖点", "例：专利菌株、活菌数450亿、0蔗糖；只允许提这些卖点"),
    ("必含话术", "每行一条，口播脚本必须出现的内容，例：点击下方链接、以页面价格为准"),
    ("禁用词", "逗号或换行分隔，出现即高风险，例：根治,100%有效,无副作用"),
    ("资质/备案号", "例：食健备J20xxxxxx / 生产许可证 SCxxxxxxxx"),
    ("其他注意事项", "例：不得出现医生/医院形象；不得对比药品；未成年人场景禁用"),
]


def get_spec(pid):
    """返回规范卡 dict（字段名 -> 文本），未填过的字段为空串"""
    import json
    p = get_product(pid)
    try:
        data = json.loads((p or {}).get("spec") or "{}")
    except Exception:
        data = {}
    return {k: str(data.get(k, "")) for k, _ in SPEC_FIELDS}


def set_spec(pid, spec):
    import json
    db.execute("UPDATE products SET spec=?, updated_at=? WHERE id=?",
               (json.dumps(spec, ensure_ascii=False), _now(), pid))


def add_files(pid, kind, src_paths):
    """把素材文件复制进产品目录并登记，返回登记后的路径列表"""
    field = _KIND_FIELDS[kind]
    p = get_product(pid)
    if not p:
        return []
    dest_dir = product_dir(p["name"])
    saved = []
    for src in src_paths:
        src = Path(src)
        if not src.exists():
            continue
        dest = dest_dir / src.name
        try:
            shutil.copy2(src, dest)
        except Exception:
            continue
        saved.append(str(dest))
    old = _split(p[field])
    merged = old + [s for s in saved if s not in old]
    db.execute(f"UPDATE products SET {field}=?, updated_at=? WHERE id=?",
               (";".join(merged), _now(), pid))
    return saved


def remove_file(pid, kind, path):
    field = _KIND_FIELDS[kind]
    p = get_product(pid)
    if not p:
        return
    left = [x for x in _split(p[field]) if x != path]
    db.execute(f"UPDATE products SET {field}=?, updated_at=? WHERE id=?",
               (";".join(left), _now(), pid))


# ---------------- 提交链路取素材 ----------------

def images_for_product(name):
    """任务提交用参考图：产品中心该品名的图片；没有则回退旧全局配置"""
    p = get_by_name(name, TYPE_PRODUCT) if name else None
    if p:
        files = [f for f in _split(p["images"]) if Path(f).exists()]
        if files:
            return files
    return list(REFERENCE_IMAGES)


def kol_image(name):
    """KOL 形象图：产品中心登记的优先，回退 material/KOL/<name>.png"""
    p = get_by_name(name, TYPE_KOL)
    if p:
        for f in _split(p["images"]):
            if Path(f).exists():
                return f
    legacy = str(Path(RUNTIME_DIR) / KOL_DIR / f"{name}.png")
    return legacy if Path(legacy).exists() else ""
