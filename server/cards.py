"""server/cards.py —— 卡密生成、归一化与运维 CLI

卡密格式：XXXX-XXXX-XXXX-XXXX，字符集剔除易混字符（0/O、1/I/L）。
售卖方式：gen 出文本文件上传第三方发卡平台当普通商品卖；
激活校验完全走本服务 /auth/activate，发卡平台不需要任何 API 对接。

CLI：
    python -m server.cards gen --days 30 --count 100 --out 卡密.txt
    python -m server.cards list [--status unused]
    python -m server.cards disable <卡密>
    python -m server.cards ban <机器码> [--reason 说明]
    python -m server.cards unban <机器码>
"""
import argparse
import secrets
import sys

from server import store

# 去掉 0/O/1/I/L：卡密靠买家手抄，一个混读字符就是一单售后纠纷
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_GROUPS = 4
_GROUP_LEN = 4


def gen_one():
    chars = [secrets.choice(_ALPHABET) for _ in range(_GROUPS * _GROUP_LEN)]
    return "-".join("".join(chars[i * _GROUP_LEN:(i + 1) * _GROUP_LEN])
                    for i in range(_GROUPS))


def normalize(raw):
    """宽松归一：用户粘贴带空格/小写/丢了连字符都认；字符集外的直接判无效。

    返回归一化卡密或 None——绝不做"智能纠错"，猜出来的卡密只会把试卡量放大。"""
    s = "".join(ch for ch in str(raw or "").upper() if ch.isalnum())
    if len(s) != _GROUPS * _GROUP_LEN:
        return None
    if any(ch not in _ALPHABET for ch in s):
        return None
    return "-".join(s[i * _GROUP_LEN:(i + 1) * _GROUP_LEN] for i in range(_GROUPS))


def generate(days, count):
    """生成 count 张不重复卡密并直接入库（发卡平台只负责卖，先入库才能验真）"""
    seen, rows = set(), []
    for _ in range(count * 3):        # 理论上撞不出 count 张，给 3 倍尝试次数兜底
        k = gen_one()
        if k in seen:
            continue
        seen.add(k)
        rows.append((k, days))
        if len(rows) >= count:
            break
    inserted = store.insert_cards(rows)
    return [r[0] for r in rows], inserted


def main(argv=None):
    store.init_db()
    ap = argparse.ArgumentParser(prog="server.cards", description="AIGC 网关卡密管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="批量生成卡密并入库")
    g.add_argument("--days", type=int, required=True, help="卡密时长（天）")
    g.add_argument("--count", type=int, default=10, help="张数（默认 10）")
    g.add_argument("--out", default="", help="输出到文本文件（一行一张，直接传发卡平台）")

    l = sub.add_parser("list", help="查看卡密")
    l.add_argument("--status", default="", choices=["", "unused", "used", "disabled"])

    d = sub.add_parser("disable", help="作废一张卡密")
    d.add_argument("card_key")

    b = sub.add_parser("ban", help="封禁机器码")
    b.add_argument("machine_code")
    b.add_argument("--reason", default="")

    u = sub.add_parser("unban", help="解封机器码")
    u.add_argument("machine_code")

    args = ap.parse_args(argv)
    if args.cmd == "gen":
        keys, inserted = generate(args.days, args.count)
        text = "\n".join(keys)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"✓ 生成 {inserted} 张（{args.days} 天卡）→ {args.out}")
        else:
            print(text)
    elif args.cmd == "list":
        rows = store.list_cards(args.status or None)
        for r in rows:
            expire = (store_ts(r["expire_at"]))
            print(f"{r['card_key']}  {r['days']:>3}天  {r['status']:<8} "
                  f"{r['machine_code'] or '-':<20} 到期:{expire}")
        print(f"合计 {len(rows)} 条（全库 unused={store.count_cards('unused')} "
              f"used={store.count_cards('used')}）")
    elif args.cmd == "disable":
        key = normalize(args.card_key)
        if not key or not store.get_card(key):
            print("卡密不存在或格式不对", file=sys.stderr)
            return 1
        store.set_card_status(key, "disabled")
        print(f"✓ 已作废 {key}")
    elif args.cmd == "ban":
        store.ban_machine(args.machine_code.strip(), args.reason)
        print(f"✓ 已封禁 {args.machine_code}")
    elif args.cmd == "unban":
        store.unban_machine(args.machine_code.strip())
        print(f"✓ 已解封 {args.machine_code}")
    return 0


def store_ts(ts):
    """时间戳转人话（不 import datetime 到顶部，CLI 专用）"""
    if not ts:
        return "-"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    sys.exit(main())
