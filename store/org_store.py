"""
store/org_store.py —— 组织结构与登录：部门 / 角色 / 线路归属 / 可见范围

设计口径（和组织结构页、task_store 过滤共用一套语义）：
- 数据归属走「线路 → 成员」映射（org_line_owners）：runs/tasks 的 account 列
  本来就是线路名，历史数据零改动就能按权限切分；
- 三级角色：admin 看全部＋管组织；manager 看本部门（成员名下的线路并集）；
  member 只看自己名下的线路；线路没绑定主人的＝只有 admin 可见；
- visible_accounts() 返 None＝不限制（组织未启用的单机模式、admin、
  以及 console 等不走登录的进程），返 list＝只许看这些线路（[]=什么都看不到）；
- 密码 pbkdf2 盐哈希入库，防的是"同事越权看数"，不防拷走 db 文件离线打开；
- 登录失败按成员名计数，连错 5 次锁 60 秒（进程内计数，重启即清，够用）。

写操作的返回约定统一：None＝成功，字符串＝给人看的错误原因。
"""
import hashlib
import hmac
import secrets
import time

from store import db

_ITER = 120_000
ROLES = {"admin": "管理员", "manager": "主管", "member": "成员"}
_FAIL_LIMIT = 5
_FAIL_LOCK_SEC = 60

_MEMBER_COLS = "id,name,role,dept,active,created_at,last_login"


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 密码哈希 ----------------

def hash_password(pwd):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(pwd).encode("utf-8"),
                             bytes.fromhex(salt), _ITER)
    return f"pbkdf2${_ITER}${salt}${dk.hex()}"


def verify_password(pwd, stored):
    """定长比较；格式不认识一律算错（防手改库造出万能哈希）"""
    try:
        scheme, iters, salt, hexhash = str(stored or "").split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", str(pwd or "").encode("utf-8"),
                                 bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), hexhash)
    except ValueError:
        return False


# ---------------- 会话 / 登录 ----------------

_CURRENT = None          # {"name","role","dept"}；None＝没登录（未启用组织或 console 进程）
_FAILS = {}              # name -> [连错次数, 解锁时间戳]


def login(name, pwd):
    global _CURRENT
    name = str(name or "").strip()
    rec = _FAILS.get(name)
    if rec and rec[1] > time.time():
        return f"失败次数过多，请 {int(rec[1] - time.time()) + 1} 秒后再试"
    rows = db.query("SELECT * FROM org_members WHERE name=?", (name,))
    if not rows:
        return "成员不存在"
    m = rows[0]
    if not m["active"]:
        return "该成员已被停用"
    if not verify_password(pwd, m["pass_hash"]):
        rec = _FAILS.setdefault(name, [0, 0.0])
        rec[0] += 1
        if rec[0] >= _FAIL_LIMIT:
            rec[0] = 0
            rec[1] = time.time() + _FAIL_LOCK_SEC
            return f"密码连续错误 {_FAIL_LIMIT} 次，已锁定 {int(_FAIL_LOCK_SEC / 60)} 分钟"
        return f"密码错误（还可试 {_FAIL_LIMIT - rec[0]} 次）"
    _FAILS.pop(name, None)
    db.execute("UPDATE org_members SET last_login=? WHERE name=?", (_now(), name))
    _CURRENT = {"name": m["name"], "role": m["role"], "dept": m["dept"]}
    return None


def logout():
    global _CURRENT
    _CURRENT = None


def current():
    return _CURRENT


def org_enabled():
    """组织是否启用：建了第一个成员就启用，下次启动要登录"""
    return bool(db.query("SELECT 1 FROM org_members LIMIT 1"))


def is_admin():
    """未启用组织＝单机全权（要能进组织结构页创建首个成员，不然自锁）"""
    if not org_enabled():
        return True
    return bool(_CURRENT) and _CURRENT["role"] == "admin"


def visible_owners():
    """当前会话允许的「负责人名字」集合；None＝不限制（admin/单机/未登录），
    list＝只有这些成员负责的才可见。

    批量基建三级组织（客户/执照/账户）与线路数据权限共用同一套角色语义：
    member 只看自己、manager 看本部门 active 成员并集、admin/单机不限。"""
    if not org_enabled() or _CURRENT is None:
        return None
    role = _CURRENT["role"]
    if role == "admin":
        return None
    who = [_CURRENT["name"]]
    if role == "manager" and _CURRENT.get("dept"):
        # 主管＝本部门全部 active 成员（含自己）
        rows = db.query(
            "SELECT name FROM org_members WHERE dept=? AND active=1",
            (_CURRENT["dept"],))
        who = list(dict.fromkeys([r["name"] for r in rows] + who))
    return who


def visible_accounts():
    """当前会话可看的线路名列表；None＝不限制，[]=一条都不许看。"""
    who = visible_owners()
    if who is None:
        return None
    ph = ",".join("?" * len(who))
    rows = db.query(
        f"SELECT account FROM org_line_owners WHERE owner IN ({ph})", who)
    return sorted({r["account"] for r in rows if r["account"]})


# ---------------- 成员 ----------------

def list_members():
    return db.query(f"SELECT {_MEMBER_COLS} FROM org_members ORDER BY id")


def get_member(name):
    rows = db.query(f"SELECT {_MEMBER_COLS} FROM org_members WHERE name=?",
                    (name,))
    return rows[0] if rows else None


def _active_admin_count(exclude_name=None):
    sql = "SELECT COUNT(*) c FROM org_members WHERE role='admin' AND active=1"
    args = []
    if exclude_name:
        sql += " AND name<>?"
        args = [exclude_name]
    return db.query(sql, args)[0]["c"]


def create_member(name, pwd, role="member", dept=""):
    name = str(name or "").strip()
    if not name:
        return "姓名不能为空"
    if get_member(name):
        return "成员已存在"
    if not pwd or len(str(pwd)) < 4:
        return "密码至少 4 位"
    role = role if role in ROLES else "member"
    # 首位成员强制 admin：不然创建者以普通人身份进门，谁都没法开组织
    if not org_enabled():
        role = "admin"
    db.execute(
        "INSERT INTO org_members(name,pass_hash,role,dept,active,created_at)"
        " VALUES(?,?,?,?,1,?)",
        (name, hash_password(str(pwd)), role, str(dept or "").strip(), _now()))
    return None


def update_member(name, role=None, dept=None, active=None):
    m = get_member(name)
    if not m:
        return "成员不存在"
    new_role = role if role in ROLES else m["role"]
    new_active = m["active"] if active is None else (1 if active else 0)
    # 降职/停用都可能把在职管理员清零——最后一名必须保住（不然没人能管组织）
    if m["role"] == "admin" and m["active"] and \
            not (new_role == "admin" and new_active) and \
            _active_admin_count(exclude_name=name) == 0:
        return "至少要保留一名启用状态的管理员"
    db.execute("UPDATE org_members SET role=?,dept=?,active=? WHERE name=?",
               (new_role, m["dept"] if dept is None else str(dept).strip(),
                new_active, name))
    if _CURRENT and _CURRENT["name"] == name:
        _CURRENT.update({"role": new_role,
                         "dept": m["dept"] if dept is None else str(dept).strip()})
    return None


def set_password(name, pwd):
    if not get_member(name):
        return "成员不存在"
    if not pwd or len(str(pwd)) < 4:
        return "密码至少 4 位"
    db.execute("UPDATE org_members SET pass_hash=? WHERE name=?",
               (hash_password(str(pwd)), name))
    _FAILS.pop(name, None)       # 重置密码后不该还背着锁定
    return None


def rename_member(old, new):
    """改名＝原地 UPDATE＋迁移线路归属（不重建，id 和最近登录都保住）"""
    m = get_member(old)
    if not m:
        return "成员不存在"
    new = str(new or "").strip()
    if not new:
        return "姓名不能为空"
    if new == old:
        return None
    if get_member(new):
        return "目标姓名已存在"
    db.execute("UPDATE org_members SET name=? WHERE name=?", (new, old))
    db.execute("UPDATE org_line_owners SET owner=? WHERE owner=?", (new, old))
    if _CURRENT and _CURRENT["name"] == old:
        _CURRENT["name"] = new
    return None


def delete_member(name):
    m = get_member(name)
    if not m:
        return "成员不存在"
    if m["role"] == "admin" and m["active"] and \
            _active_admin_count(exclude_name=name) == 0:
        return "不能删除最后一名管理员"
    db.execute("DELETE FROM org_members WHERE name=?", (name,))
    # 名下线路退回未绑定（只 admin 可见），不删行：历史归属信息留着有用
    db.execute("UPDATE org_line_owners SET owner='' WHERE owner=?", (name,))
    return None


# ---------------- 部门（单级） ----------------

def list_depts():
    return [r["name"] for r in db.query("SELECT name FROM org_depts ORDER BY name")]


def add_dept(name):
    name = str(name or "").strip()
    if not name:
        return "部门名不能为空"
    db.execute("INSERT OR IGNORE INTO org_depts(name,created_at) VALUES(?,?)",
               (name, _now()))
    return None


def rename_dept(old, new):
    old, new = str(old or "").strip(), str(new or "").strip()
    if not new:
        return "部门名不能为空"
    if new == old:
        return None
    if not db.query("SELECT 1 FROM org_depts WHERE name=?", (old,)):
        return "部门不存在"
    if db.query("SELECT 1 FROM org_depts WHERE name=?", (new,)):
        return "目标部门名已存在"
    db.execute("UPDATE org_depts SET name=? WHERE name=?", (new, old))
    db.execute("UPDATE org_members SET dept=? WHERE dept=?", (new, old))
    if _CURRENT and _CURRENT.get("dept") == old:
        _CURRENT["dept"] = new
    return None


def delete_dept(name):
    db.execute("DELETE FROM org_depts WHERE name=?", (name,))
    db.execute("UPDATE org_members SET dept='' WHERE dept=?", (name,))
    if _CURRENT and _CURRENT.get("dept") == name:
        _CURRENT["dept"] = ""
    return None


# ---------------- 线路归属 ----------------

def all_lines():
    """线路全集：当前配置的线路 ∪ 库里跑过的线路名（历史账号也得能归）"""
    try:
        from core.config import ACCOUNTS
        names = {str(a.get("name") or "").strip() for a in ACCOUNTS}
    except Exception:
        names = set()
    rows = db.query(
        "SELECT account FROM ("
        "  SELECT DISTINCT account FROM runs WHERE account<>''"
        "  UNION SELECT DISTINCT account FROM tasks WHERE account<>'')"
        " ORDER BY account")
    names |= {r["account"] for r in rows}
    return sorted(n for n in names if n)


def line_owners():
    return {r["account"]: (r["owner"] or "")
            for r in db.query("SELECT account,owner FROM org_line_owners")}


def set_line_owner(account, owner):
    account = str(account or "").strip()
    owner = str(owner or "").strip()
    if not account:
        return "线路名不能为空"
    if owner and not get_member(owner):
        return "归属成员不存在"
    if owner:
        db.execute(
            "INSERT INTO org_line_owners(account,owner,updated_at)"
            " VALUES(?,?,?)"
            " ON CONFLICT(account) DO UPDATE SET owner=excluded.owner,"
            " updated_at=excluded.updated_at",
            (account, owner, _now()))
    else:
        db.execute("DELETE FROM org_line_owners WHERE account=?", (account,))
    return None
