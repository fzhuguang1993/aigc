"""
store/risk_store.py —— 风控中心：平台/产品风控政策的增删查改
政策条目中的「禁用词」会被口播规范检测（checkers.spec_checker）自动引用。
"""
import re
from datetime import datetime

from store import db

SCOPE_PLATFORM = "platform"   # 平台风控（抖音/视频号等平台规则）
SCOPE_PRODUCT = "policy"      # 产品风控（品类/品牌特有限制）
SCOPE_LABELS = {SCOPE_PLATFORM: "平台风控", SCOPE_PRODUCT: "产品风控"}

# 首次使用时种入一条通用平台政策（广告法极限词）
_DEFAULT_BANNED = ("最好,最佳,第一,顶级,独家,绝无仅有,史无前例,空前绝后,"
                   "根治,包治,特效,全效,速效,安全无副作用,无任何副作用,"
                   "100%有效,百分百有效,彻底根治,永不复发,零风险,"
                   "稳赚,包赚,秒杀全网,全网底价,全网销量第一")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_default():
    rows = db.query("SELECT COUNT(*) c FROM risk_rules")
    if rows and rows[0]["c"] == 0:
        db.execute(
            "INSERT INTO risk_rules(scope,title,applies,content,banned,active,updated_at) "
            "VALUES(?,?,?,?,?,1,?)",
            (SCOPE_PLATFORM, "广告法极限词（通用）", "",
             "《广告法》禁止使用的绝对化、疗效类用语。口播脚本/提示词中出现右侧禁用词，"
             "即判定为高风险，需要改写。各平台（抖音/视频号/小红书）均同步管控。",
             _DEFAULT_BANNED, _now()))


def list_rules(scope=None, keyword=""):
    sql, args = "SELECT * FROM risk_rules", []
    where = []
    if scope:
        where.append("scope=?"); args.append(scope)
    if keyword:
        where.append("(title LIKE ? OR content LIKE ? OR banned LIKE ?)")
        args += [f"%{keyword}%"] * 3
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY scope, updated_at DESC"
    return db.query(sql, tuple(args))


def get_rule(rid):
    rows = db.query("SELECT * FROM risk_rules WHERE id=?", (rid,))
    return rows[0] if rows else None


def add_rule(scope, title, applies="", content="", banned=""):
    return db.execute(
        "INSERT INTO risk_rules(scope,title,applies,content,banned,active,updated_at) "
        "VALUES(?,?,?,?,?,1,?)",
        (scope, title, applies, content, banned, _now()))


def update_rule(rid, scope, title, applies, content, banned, active=1):
    db.execute("UPDATE risk_rules SET scope=?,title=?,applies=?,content=?,"
               "banned=?,active=?,updated_at=? WHERE id=?",
               (scope, title, applies, content, banned, active, _now(), rid))


def delete_rule(rid):
    db.execute("DELETE FROM risk_rules WHERE id=?", (rid,))


def _split_words(s):
    return [w.strip() for w in re.split(r"[,，;；\n]+", s or "") if w.strip()]


def banned_words(product_name=""):
    """汇总所有生效政策的禁用词；产品名非空时附带该产品专属政策的禁用词"""
    out = []
    for r in db.query("SELECT * FROM risk_rules WHERE active=1"):
        applies = (r["applies"] or "").strip()
        if applies and product_name and product_name not in applies:
            continue
        if applies and not product_name:
            continue          # 专属政策不计入全局汇总
        out += _split_words(r["banned"])
    return sorted(set(out))


def rules_for(product_name=""):
    """某产品适用的政策列表（全局 + 名称命中的专属政策）"""
    out = []
    for r in db.query("SELECT * FROM risk_rules WHERE active=1 ORDER BY scope"):
        applies = (r["applies"] or "").strip()
        if not applies or (product_name and product_name in applies):
            out.append(r)
    return out
