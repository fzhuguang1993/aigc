"""
store/local_org_store.py —— 批量基建三级组织：客户 / 执照 / 本地推账户 + 数据权限

设计口径（与 org_store 的角色语义共用一套）：
- 严格树 客户 → 执照/主体 → 本地推账户，逐表 SQLite；本地推账户凭证沿用 config 的
  encrypt_value(盐=USER_NAME) 加密存 secret_enc，明文只在内存/回读时出现，盘上不留明文；
- owner（负责成员名）三层都可设，解析「就近继承」：账户自带优先，否则回退所属执照、
  再回退客户 —— 于是既能「层级授权」（在客户/执照上批量下发）也能「逐个授权」（单账户覆盖）；
- 可见性走 org_store.visible_owners()：admin/单机/未登录＝None（不限），member＝[自己]、
  manager＝本部门 active 成员；effective_owner 不在允许集合的账户被过滤，其所在客户/执照
  若无任何可见账户且自身 owner 也未命中则整节点隐藏（未授权节点仅 admin 可见）；
- 首次访问把 config.json 遗留的 local_accounts 一次性迁入（幂等，app_state 记标记）。

写操作返回约定与 publish 一致：save_xxx 返回最终 id（int），delete_xxx 返回 bool。
"""
import json
from datetime import datetime

from store import db


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_MIG_KEY = "local_push_migrated_v1"


# ==================================================================
# 加密小工具（凭证只在 core.config 之外这一处加解密，盐=USER_NAME）
# ==================================================================
def _encrypt_secret(secret):
    from core.config import USER_NAME
    from video_text_tools.material_extract import encrypt_value
    return encrypt_value(json.dumps(secret or {}, ensure_ascii=False), USER_NAME)


def _decrypt_secret(enc):
    from core.config import USER_NAME
    from video_text_tools.material_extract import decrypt_value
    if not enc:
        return {}
    plain = decrypt_value(str(enc), USER_NAME)
    try:
        val = json.loads(plain) if plain else {}
    except ValueError:
        val = {}
    return val if isinstance(val, dict) else {}


def _parse_extra(s):
    try:
        val = json.loads(s) if s else {}
    except ValueError:
        val = {}
    return val if isinstance(val, dict) else {}


# ==================================================================
# 客户（顶层）
# ==================================================================
def list_customers():
    return db.query("SELECT * FROM local_customers ORDER BY id")


def get_customer(cid):
    rows = db.query("SELECT * FROM local_customers WHERE id=?", (int(cid),))
    return rows[0] if rows else None


def save_customer(data):
    """按 id upsert 一个客户；返回最终 id（int）。"""
    name = str(data.get("name") or "").strip()
    owner = str(data.get("owner") or "").strip()
    remark = str(data.get("remark") or "").strip()
    cid = data.get("id")
    if cid:
        db.execute("UPDATE local_customers SET name=?,owner=?,remark=?,updated_at=? WHERE id=?",
                   (name, owner, remark, _now(), int(cid)))
        return int(cid)
    return db.execute(
        "INSERT INTO local_customers(name,owner,remark,created_at,updated_at)"
        " VALUES(?,?,?,?,?)", (name, owner, remark, _now(), _now()))


def delete_customer(cid):
    """删除客户并级联删其下执照与账户（严格树，不留孤儿）。返回是否删了。"""
    cid = int(cid)
    if not db.query("SELECT 1 FROM local_customers WHERE id=?", (cid,)):
        return False
    lic_ids = [r["id"] for r in db.query(
        "SELECT id FROM local_licenses WHERE customer_id=?", (cid,))]
    _delete_accounts_by_licenses(lic_ids)
    db.execute("DELETE FROM local_licenses WHERE customer_id=?", (cid,))
    db.execute("DELETE FROM local_customers WHERE id=?", (cid,))
    return True


# ==================================================================
# 执照 / 主体（中层）
# ==================================================================
def list_licenses(customer_id=None):
    if customer_id is None:
        return db.query("SELECT * FROM local_licenses ORDER BY id")
    return db.query("SELECT * FROM local_licenses WHERE customer_id=? ORDER BY id",
                    (int(customer_id),))


def get_license(lid):
    rows = db.query("SELECT * FROM local_licenses WHERE id=?", (int(lid),))
    return rows[0] if rows else None


def save_license(data):
    lid = data.get("id")
    customer_id = int(data.get("customer_id") or 0)
    name = str(data.get("name") or "").strip()
    subject = str(data.get("subject") or "").strip()
    owner = str(data.get("owner") or "").strip()
    remark = str(data.get("remark") or "").strip()
    if lid:
        db.execute("UPDATE local_licenses SET customer_id=?,name=?,subject=?,owner=?,"
                   "remark=?,updated_at=? WHERE id=?",
                   (customer_id, name, subject, owner, remark, _now(), int(lid)))
        return int(lid)
    return db.execute(
        "INSERT INTO local_licenses(customer_id,name,subject,owner,remark,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (customer_id, name, subject, owner, remark, _now(), _now()))


def delete_license(lid):
    """删除执照并级联删其下账户。"""
    lid = int(lid)
    if not db.query("SELECT 1 FROM local_licenses WHERE id=?", (lid,)):
        return False
    _delete_accounts_by_licenses([lid])
    db.execute("DELETE FROM local_licenses WHERE id=?", (lid,))
    return True


# ==================================================================
# 本地推账户（叶子，凭证加密）
# ==================================================================
def _delete_accounts_by_licenses(lic_ids):
    if not lic_ids:
        return
    ph = ",".join("?" * len(lic_ids))
    db.execute(f"DELETE FROM local_ad_accounts WHERE license_id IN ({ph})", lic_ids)


def _account_rows(license_id=None, customer_id=None):
    """联表取账户（带出所属执照/客户的 owner 与名称），供内部组装与过滤。"""
    sql = ("SELECT a.*, l.owner AS _lic_owner, l.name AS _lic_name, l.customer_id AS _cust,"
           " c.owner AS _cust_name_owner, c.name AS _cust_name"
           " FROM local_ad_accounts a"
           " LEFT JOIN local_licenses l ON a.license_id=l.id"
           " LEFT JOIN local_customers c ON l.customer_id=c.id")
    conds, args = [], []
    if license_id is not None:
        conds.append("a.license_id=?"); args.append(int(license_id))
    if customer_id is not None:
        conds.append("l.customer_id=?"); args.append(int(customer_id))
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY a.id"
    return db.query(sql, args)


def _effective_owner(row):
    """就近继承：账户 owner → 执照 owner → 客户 owner。"""
    return (row.get("owner") or "").strip() \
        or (row.get("_lic_owner") or "").strip() \
        or (row.get("_cust_name_owner") or "").strip()


def _row_to_account(row, include_secret=True):
    d = {
        "id": str(row["id"]),
        "platform": row.get("platform") or "douyin",
        "label": row.get("label") or "",
        "advertiser_id": row.get("advertiser_id") or "",
        "auth_type": row.get("auth_type") or "oauth",
        "secret": _decrypt_secret(row.get("secret_enc")) if include_secret else {},
        "extra": _parse_extra(row.get("extra")),
        "license_id": int(row.get("license_id") or 0),
        "customer_id": int(row.get("_cust") or 0),
        "owner": row.get("owner") or "",
        "license_name": row.get("_lic_name") or "",
        "customer_name": row.get("_cust_name") or "",
    }
    return d


def list_accounts(license_id=None, customer_id=None, apply_permission=True):
    """账户字典列表（secret 已解密），默认按当前会话权限过滤。"""
    rows = _account_rows(license_id=license_id, customer_id=customer_id)
    allowed = _allowed_owners() if apply_permission else None
    out = []
    for r in rows:
        if allowed is not None and _effective_owner(r) not in allowed:
            continue
        out.append(_row_to_account(r))
    return out


def get_account(aid, include_secret=True):
    rows = db.query("SELECT * FROM local_ad_accounts WHERE id=?", (int(aid),))
    if not rows:
        return None
    row = rows[0]
    lic = get_license(row["license_id"]) if row.get("license_id") else None
    cust = get_customer(lic["customer_id"]) if lic and lic.get("customer_id") else None
    merged = dict(row)
    merged["_lic_name"] = lic["name"] if lic else ""
    merged["_cust"] = lic["customer_id"] if lic else 0
    merged["_cust_name"] = cust["name"] if cust else ""
    merged["_lic_owner"] = lic["owner"] if lic else ""
    merged["_cust_name_owner"] = cust["owner"] if cust else ""
    return _row_to_account(merged, include_secret=include_secret)


def save_account(acct):
    """按 id upsert 一个本地推账户；secret 非空则重新加密，为空则保留原密文。返回 id。"""
    license_id = int(acct.get("license_id") or 0)
    platform = str(acct.get("platform") or "douyin")
    label = str(acct.get("label") or "").strip()
    advertiser_id = str(acct.get("advertiser_id") or "").strip()
    auth_type = str(acct.get("auth_type") or "oauth")
    owner = str(acct.get("owner") or "").strip()
    extra = acct.get("extra") if isinstance(acct.get("extra"), dict) else {}
    extra_str = json.dumps(extra, ensure_ascii=False)
    secret = acct.get("secret") or {}

    aid = acct.get("id")
    if aid:
        enc = _encrypt_secret(secret) if secret else \
            (db.query("SELECT secret_enc FROM local_ad_accounts WHERE id=?", (int(aid),))
             or [{"secret_enc": ""}])[0]["secret_enc"]
        db.execute("UPDATE local_ad_accounts SET license_id=?,platform=?,label=?,"
                   "advertiser_id=?,auth_type=?,owner=?,extra=?,secret_enc=?,updated_at=?"
                   " WHERE id=?",
                   (license_id, platform, label, advertiser_id, auth_type, owner,
                    extra_str, enc, _now(), int(aid)))
        return int(aid)
    enc = _encrypt_secret(secret) if secret else ""
    return db.execute(
        "INSERT INTO local_ad_accounts(license_id,platform,label,advertiser_id,"
        "auth_type,secret_enc,owner,extra,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (license_id, platform, label, advertiser_id, auth_type, enc, owner,
         extra_str, _now(), _now()))


def delete_account(aid):
    aid = int(aid)
    if not db.query("SELECT 1 FROM local_ad_accounts WHERE id=?", (aid,)):
        return False
    db.execute("DELETE FROM local_ad_accounts WHERE id=?", (aid,))
    return True


# ==================================================================
# 数据权限（与 org_store 共用角色语义）
# ==================================================================
def _allowed_owners():
    from store import org_store
    return org_store.visible_owners()          # None＝不限


def _customer_visible_ids(allowed):
    """可见客户集合：自身 owner 命中，或其下有任一可见账户/可见执照。"""
    vis_lic = set()
    for r in _account_rows():
        if _effective_owner(r) in allowed:
            vis_lic.add(int(r.get("license_id") or 0))
    vis_cust = set()
    for l in db.query("SELECT id,customer_id,owner FROM local_licenses"):
        if l["owner"] and l["owner"] in allowed:
            vis_lic.add(l["id"])
            vis_cust.add(int(l["customer_id"] or 0))
        elif l["id"] in vis_lic:
            vis_cust.add(int(l["customer_id"] or 0))
    for c in db.query("SELECT id,owner FROM local_customers"):
        if c["owner"] and c["owner"] in allowed:
            vis_cust.add(c["id"])
    return vis_cust, vis_lic


def list_customers_visible():
    """按当前会话权限过滤的客户（admin/单机＝全部）。"""
    allowed = _allowed_owners()
    rows = list_customers()
    if allowed is None:
        return rows
    vis_cust, _ = _customer_visible_ids(allowed)
    return [r for r in rows if r["id"] in vis_cust]


def list_licenses_visible(customer_id=None):
    allowed = _allowed_owners()
    rows = list_licenses(customer_id=customer_id)
    if allowed is None:
        return rows
    _, vis_lic = _customer_visible_ids(allowed)
    return [r for r in rows if r["id"] in vis_lic]


def can_view_account(aid):
    allowed = _allowed_owners()
    if allowed is None:
        return True
    row = db.query("SELECT * FROM local_ad_accounts WHERE id=?", (int(aid),))
    if not row:
        return False
    lic = get_license(row[0].get("license_id")) if row[0].get("license_id") else None
    cust = get_customer(lic["customer_id"]) if lic and lic.get("customer_id") else None
    eff = (row[0].get("owner") or "").strip() \
        or ((lic or {}).get("owner") or "").strip() \
        or ((cust or {}).get("owner") or "").strip()
    return eff in allowed


# ==================================================================
# config.json 一次性迁移
# ==================================================================
def migrate_from_config():
    """把旧版 config.local_accounts 原样搬进 DB（secret_enc 不重加密）。幂等。

    仅当账户表为空且检测到遗留数据时执行；老账户统一收纳进一个
    「默认客户（迁移） / 默认主体」。返回是否真正发生了迁移。"""
    from store import app_state
    if app_state.get(_MIG_KEY):
        return False
    if db.query("SELECT 1 FROM local_ad_accounts LIMIT 1"):
        app_state.set_value(_MIG_KEY, True)       # 已有数据，无需迁移
        return False
    try:
        import core.config as cfg
        raws = [e for e in cfg._local_accounts_raw() if isinstance(e, dict)]
    except Exception:
        raws = []
    if not raws:
        app_state.set_value(_MIG_KEY, True)
        return False

    cust_id = save_customer({"id": "", "name": "默认客户（迁移）",
                             "owner": "", "remark": "升级时自动收纳的遗留本地推账户"})
    lic_id = save_license({"id": "", "customer_id": cust_id, "name": "默认主体",
                           "subject": "", "owner": "", "remark": ""})
    for e in raws:
        extra = e.get("extra") if isinstance(e.get("extra"), dict) else {}
        db.execute(
            "INSERT INTO local_ad_accounts(license_id,platform,label,advertiser_id,"
            "auth_type,secret_enc,owner,extra,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (lic_id, str(e.get("platform") or "douyin"), str(e.get("label") or ""),
             str(e.get("advertiser_id") or ""), str(e.get("auth_type") or "oauth"),
             str(e.get("secret_enc") or ""), "", json.dumps(extra, ensure_ascii=False),
             _now(), _now()))
    app_state.set_value(_MIG_KEY, True)
    return True
