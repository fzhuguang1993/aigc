"""
video_text_tools/local_push/accounts.py —— 本地推账户读写门面（对接 local_org_store）

账户已从 config.json 迁入 SQLite 三级组织的最底层（local_ad_accounts）：
- 凭证的加密落盘 / 解密回填统一在 store.local_org_store（Fernet 盐=USER_NAME）；
- 首次读取触发一次 config → DB 迁移守卫（幂等）；
- 默认按当前会话权限过滤（org_store.visible_owners 语义：admin/单机不限）。
本模块只做 dict ↔ LocalAccount 对象的翻译，不碰磁盘、不碰密文与 SQL 细节。
客户/执照两层的增删改查由 GUI 直接调 store.local_org_store。
"""
from .models import LocalAccount


def list_accounts(apply_permission=True):
    """本地推账户列表（secret 已解密为明文 dict），默认按权限过滤。"""
    from store import local_org_store
    local_org_store.migrate_from_config()      # 幂等：老数据只在首次搬进 DB
    return [LocalAccount(**d)
            for d in local_org_store.list_accounts(apply_permission=apply_permission)]


def save_account(acct: LocalAccount):
    """新增或按 id 覆盖保存一个账户（内部完成凭证加密）。返回带 id 的账号。"""
    from store import local_org_store
    acct.id = str(local_org_store.save_account({
        "id": acct.id, "license_id": acct.license_id, "platform": acct.platform,
        "label": acct.label, "advertiser_id": acct.advertiser_id,
        "auth_type": acct.auth_type, "secret": acct.secret,
        "extra": acct.extra, "owner": acct.owner,
    }))
    return acct


def delete_account(acc_id):
    from store import local_org_store
    return local_org_store.delete_account(acc_id)
