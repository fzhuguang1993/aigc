"""
video_text_tools/publish/accounts.py —— 平台账号读写门面（对接 core.config）

凭证的加密落盘 / 解密回填统一在 core.config（list_publish_accounts /
save_publish_account / delete_publish_account，复用 Fernet 盐=USER_NAME）。
本模块只负责把那些 dict 翻成内存里的 Account 对象、以及反向组装，供 GUI 与
runner 使用；不自己碰磁盘、不碰密文细节。
"""
from .models import Account


def list_accounts():
    """全部平台账号（secret 已解密为明文 dict）。"""
    from core.config import list_publish_accounts
    return [Account(**d) for d in list_publish_accounts()]


def accounts_by_platform():
    """{platform: [Account]}，UI 分组显示用。"""
    grouped = {}
    for a in list_accounts():
        grouped.setdefault(a.platform, []).append(a)
    return grouped


def save_account(acct: Account):
    """新增或按 id 覆盖保存一个账号（内部完成凭证加密）。返回带 id 的账号。"""
    from core.config import save_publish_account
    acct.id = save_publish_account({
        "id": acct.id, "platform": acct.platform, "label": acct.label,
        "auth_type": acct.auth_type, "secret": acct.secret,
        "extra": acct.extra,
    })
    return acct


def delete_account(acc_id):
    from core.config import delete_publish_account
    return delete_publish_account(acc_id)
