"""
tests/test_org_store.py —— 组织结构：密码 / 登录锁定 / 成员 / 部门 / 线路归属 / 可见范围

org 三张表与会话（_CURRENT/_FAILS）的清理由 conftest autouse 夹具兜底，
用例里放心造数据。写操作约定：None＝成功、str＝错误原因。
"""
from datetime import date

from store import db, org_store


def _mk(name, role="member", dept="", lines=()):
    assert org_store.create_member(name, "1234", role, dept) is None
    for ln in lines:
        assert org_store.set_line_owner(ln, name) is None


# ---------------- 密码哈希 ----------------

def test_hash_roundtrip():
    h = org_store.hash_password("s3cret")
    assert h.startswith("pbkdf2$")
    assert org_store.verify_password("s3cret", h)
    assert not org_store.verify_password("S3cret", h)      # 大小写敏感
    assert not org_store.verify_password("s3creu", h[:-4] + "0000")


def test_verify_rejects_unknown_format():
    """手改库造出怪串一律算错，绝不当万能钥匙"""
    assert not org_store.verify_password("x", "")
    assert not org_store.verify_password("x", "md5$abc")
    assert not org_store.verify_password("x", None)
    assert not org_store.verify_password("x", "pbkdf2$1$zz$ff")   # 盐非 hex


# ---------------- 成员与引导 ----------------

def test_first_member_forced_admin():
    """首个成员强制 admin：不然创建者以普通人进门，谁都没法开组织"""
    assert org_store.create_member("老板", "1234", role="member") is None
    assert org_store.get_member("老板")["role"] == "admin"
    assert org_store.org_enabled()
    assert org_store.create_member("小张", "1234", "manager") is None
    assert org_store.get_member("小张")["role"] == "manager"      # 之后按传入


def test_create_member_validations():
    assert org_store.create_member("  ", "1234")          # 空名
    assert org_store.create_member("甲", "12")            # 密码 <4 位
    assert org_store.create_member("甲", "1234") is None
    assert org_store.create_member("甲", "1234") == "成员已存在"


def test_list_members_hides_pass_hash():
    org_store.create_member("老板", "1234")
    rows = org_store.list_members()
    assert "pass_hash" not in rows[0]


def test_last_admin_protection():
    """永远至少留一名 active admin：防降职/停用/删除自锁"""
    org_store.create_member("老板", "1234")
    assert "管理员" in org_store.delete_member("老板")
    assert "管理员" in org_store.update_member("老板", role="member")
    assert "管理员" in org_store.update_member("老板", active=False)
    _mk("二管", "admin")                     # 组织启用后按传入 role 建
    assert org_store.get_member("二管")["role"] == "admin"
    assert org_store.update_member("老板", role="member") is None
    assert org_store.delete_member("老板") is None


def test_rename_member_migrates_lines_and_session():
    org_store.create_member("老板", "1234")
    _mk("小张", lines=["线路A"])
    assert org_store.login("小张", "1234") is None
    assert org_store.rename_member("小张", "大章") is None
    assert org_store.get_member("小张") is None
    assert org_store.line_owners() == {"线路A": "大章"}   # 名下线路跟着走
    assert org_store.current()["name"] == "大章"          # 会话同步
    assert org_store.visible_accounts() == ["线路A"]
    assert org_store.rename_member("大章", "老板") == "目标姓名已存在"


def test_delete_member_releases_lines():
    org_store.create_member("老板", "1234")
    _mk("小张", lines=["线路A"])
    assert org_store.delete_member("小张") is None
    # 行留着、owner 置空：退回未绑定（只有 admin 可见）
    assert org_store.line_owners() == {"线路A": ""}


# ---------------- 登录 / 锁定 ----------------

def test_login_flow():
    org_store.create_member("老板", "1234")
    assert org_store.login("查无此人", "1234") == "成员不存在"
    assert "密码错误" in org_store.login("老板", "bad")
    assert org_store.login("老板", "1234") is None
    cur = org_store.current()
    assert cur == {"name": "老板", "role": "admin", "dept": ""}
    org_store.logout()
    assert org_store.current() is None
    assert org_store.get_member("老板")["last_login"]      # 成功登录打点


def test_login_inactive_member():
    org_store.create_member("老板", "1234")
    _mk("小张")
    assert org_store.update_member("小张", active=False) is None
    assert "停用" in org_store.login("小张", "1234")


def test_login_lockout_until_reset():
    """连错 5 次锁 60 秒；锁定期内对密码也拒；重置密码解除锁定"""
    org_store.create_member("老板", "1234")
    for _ in range(4):
        assert "还可试" in org_store.login("老板", "bad")
    assert "锁定" in org_store.login("老板", "bad")
    assert "秒后再试" in org_store.login("老板", "1234")
    assert org_store.current() is None
    assert org_store.set_password("老板", "fresh") is None
    assert org_store.login("老板", "fresh") is None


def test_wrong_password_resets_after_success():
    org_store.create_member("老板", "1234")
    for _ in range(4):
        org_store.login("老板", "bad")
    assert org_store.login("老板", "1234") is None       # 成功清零
    org_store.logout()
    for _ in range(4):
        assert "还可试" in org_store.login("老板", "bad")  # 又满 4 次才见锁


# ---------------- 部门 ----------------

def test_dept_rename_delete_cascade():
    org_store.create_member("老板", "1234")
    _mk("小张", dept="一部", lines=["线路A"])
    assert org_store.add_dept("一部") is None
    assert org_store.add_dept("一部") is None             # 重名静默通过
    assert org_store.rename_dept("查无", "新") == "部门不存在"
    assert org_store.rename_dept("一部", "新一") is None
    assert org_store.get_member("小张")["dept"] == "新一"
    assert org_store.login("小张", "1234") is None
    assert org_store.rename_dept("新一", "老名") is None
    assert org_store.current()["dept"] == "老名"          # 会话同步
    assert org_store.delete_dept("老名") is None
    assert org_store.get_member("小张")["dept"] == ""
    assert org_store.current()["dept"] == ""
    # 线路归属跟人：删部门不动可见范围
    assert org_store.visible_accounts() == ["线路A"]


# ---------------- 线路归属 / 可见范围 ----------------

def test_set_line_owner_basic():
    org_store.create_member("老板", "1234")
    _mk("小张")
    assert org_store.set_line_owner("", "小张")            # 线路名为空
    assert org_store.set_line_owner("线路A", "查无此人")    # 归属成员不存在
    assert org_store.set_line_owner("线路A", "小张") is None
    assert org_store.line_owners()["线路A"] == "小张"
    assert org_store.set_line_owner("线路A", "") is None   # 解绑＝删行
    assert org_store.line_owners() == {}


def test_all_lines_includes_history():
    """当前配置之外、库里跑过的老线路也要能出现在归属表里"""
    db.execute("INSERT INTO runs(task_id,num,product,account,job_id,status,"
               "started_at) VALUES(1,'1','P','老线路','j-old','completed',?)",
               (date.today().isoformat() + " 10:00:00",))
    assert "老线路" in org_store.all_lines()


def test_visible_accounts_three_roles():
    org_store.create_member("老板", "1234")
    _mk("小张", dept="一部", lines=["线路A"])
    _mk("小李", dept="一部", lines=["线路B"])
    _mk("主管", "manager", "一部")
    assert org_store.set_line_owner("线路C", "") is None   # C 无主（解绑不存在行也无害）
    # 无会话（console/未启用进程）＝不限制，与旧行为一致
    assert org_store.visible_accounts() is None
    assert org_store.login("老板", "1234") is None
    assert org_store.visible_accounts() is None           # admin 全量
    assert org_store.login("小张", "1234") is None
    assert org_store.visible_accounts() == ["线路A"]      # member 只看自己
    assert org_store.login("主管", "1234") is None
    assert org_store.visible_accounts() == ["线路A", "线路B"]   # 部门并集
    # 停用的成员不进主管并集
    assert org_store.update_member("小李", active=False) is None
    assert org_store.visible_accounts() == ["线路A"]


def test_visible_accounts_empty_for_no_line():
    """名下没线路的 member（含无部门主管）→ 空列表：什么都不给看"""
    org_store.create_member("老板", "1234")
    _mk("甲木", "manager")          # 无部门主管：union 只含自己＝空
    assert org_store.login("甲木", "1234") is None
    assert org_store.visible_accounts() == []


def test_is_admin_semantics():
    assert org_store.is_admin()                       # 未启用组织＝单机全权
    org_store.create_member("老板", "1234")
    assert not org_store.is_admin()                   # 启用后没登录不算 admin
    org_store.login("老板", "1234")
    assert org_store.is_admin()


def test_update_member_syncs_session_role():
    org_store.create_member("老板", "1234")
    _mk("小张", lines=["线路A"])
    assert org_store.login("小张", "1234") is None
    assert org_store.visible_accounts() == ["线路A"]
    assert org_store.update_member("小张", role="admin") is None
    assert org_store.current()["role"] == "admin"     # 会话同步
    assert org_store.visible_accounts() is None       # 升权立刻生效
