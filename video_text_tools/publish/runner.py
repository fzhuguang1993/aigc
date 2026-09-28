"""
video_text_tools/publish/runner.py —— 一键发布批量编排

把「M 条视频 × N 个账号」铺成一个任务矩阵，逐格交给对应平台适配器发布，
收集每格结果。原则（对齐爆款拆解的"不静默"）：
- 单格失败绝不中断整批：异常/无适配器/未实现都落成 ok=False 的 PublishRecord；
- 每格即时回报 log/progress，方便 GUI 刷进度条；
- 尊重 should_stop：已开始的这格跑完，后续格直接标"已取消"不再发。

本模块不碰 Qt、不落库；落库由上层（GUI）拿返回的 records 调 store.publish_store。
"""
from .models import PublishRecord, platform_label
from .base import get_adapter


def publish_batch(items, accounts, log=None, progress=None, should_stop=None):
    """items: [PublishItem]，accounts: [Account]；返回 [PublishRecord]。"""
    def _log(msg):
        if log:
            log(msg)

    total = max(len(items) * len(accounts), 1)
    done = 0
    records = []

    if not items:
        _log("⚠ 没有待发布的视频")
        return records
    if not accounts:
        _log("⚠ 没有勾选任何发布账号")
        return records

    for item in items:
        for acct in accounts:
            if should_stop and should_stop():
                _log("⏹ 收到停止请求，剩余任务标记为已取消")
                rec = PublishRecord(platform=acct.platform,
                                    account_label=acct.label,
                                    title=item.display_title(),
                                    video_path=item.video_path)
                rec.message = "已取消"
                records.append(rec)
                done += 1
                continue

            _log(f"▶ [{platform_label(acct.platform)}/{acct.label}] {item.display_title()}")
            adapter = get_adapter(acct.platform, acct.auth_type)
            if adapter is None:
                rec = PublishRecord(platform=acct.platform, account_label=acct.label,
                                    title=item.display_title(),
                                    video_path=item.video_path)
                rec.message = f"{platform_label(acct.platform)} 无可用适配器（{acct.auth_type}）"
                _log(f"  ✗ {rec.message}")
            else:
                try:
                    rec = adapter.publish_one(acct, item, log=log,
                                              progress=progress, should_stop=should_stop)
                except Exception as e:            # 适配器自身漏网异常也不带走整批
                    rec = PublishRecord(platform=acct.platform,
                                        account_label=acct.label,
                                        title=item.display_title(),
                                        video_path=item.video_path)
                    rec.message = f"{type(e).__name__}: {e}"
                _log("  " + rec.summary())
            records.append(rec)
            done += 1
            if progress:
                progress(done, total, item.display_title())

    ok = sum(1 for r in records if r.ok)
    _log(f"发布完成：成功 {ok} / 共 {len(records)}（失败 {len(records) - ok}）")
    return records
