"""
video_text_tools/local_push/runner.py —— 批量基建批量编排（方案 × 账户 矩阵）

把「M 个计划方案 × N 个本地推账户」铺成一个搭建矩阵，逐格交给对应平台适配器
执行（素材 → 项目 → 营销），收集每格结果。原则（对齐一键发布/爆款拆解的
"不静默"）：
- 单格失败绝不中断整批：异常/无适配器都落成 ok=False 的 BuildRecord；
- 每格即时回报 log/progress，方便 GUI 刷进度条；
- 尊重 should_stop：已开始的这格跑完，后续格直接标"已取消"不再建。

本模块不碰 Qt、不落库；落库由上层（GUI）拿返回的 records 调 store.build_store。
"""
from .models import BuildRecord, platform_label, apply_org_fields
from .base import get_adapter


def build_batch(plans, accounts, log=None, progress=None, should_stop=None):
    """plans: [PlanItem]，accounts: [LocalAccount]；返回 [BuildRecord]。"""
    def _log(msg):
        if log:
            log(msg)

    total = max(len(plans) * len(accounts), 1)
    done = 0
    records = []

    if not plans:
        _log("⚠ 没有可执行的计划方案")
        return records
    if not accounts:
        _log("⚠ 没有勾选任何本地推账户")
        return records

    for plan in plans:
        for acct in accounts:
            if should_stop and should_stop():
                _log("⏹ 收到停止请求，剩余任务标记为已取消")
                rec = BuildRecord(platform=acct.platform,
                                  account_label=acct.label,
                                  advertiser_id=acct.advertiser_id,
                                  plan_name=plan.display_name())
                rec.message = "已取消"
                apply_org_fields(rec, acct)
                records.append(rec)
                done += 1
                continue

            _log(f"▶ [{platform_label(acct.platform)}/{acct.label}] {plan.display_name()}")
            adapter = get_adapter(acct.platform)
            if adapter is None:
                rec = BuildRecord(platform=acct.platform, account_label=acct.label,
                                  advertiser_id=acct.advertiser_id,
                                  plan_name=plan.display_name())
                rec.message = f"{platform_label(acct.platform)} 无可用适配器"
                _log(f"  ✗ {rec.message}")
            else:
                try:
                    rec = adapter.run_one(acct, plan, log=log,
                                          progress=progress, should_stop=should_stop)
                except Exception as e:            # 适配器自身漏网异常也不带走整批
                    rec = BuildRecord(platform=acct.platform,
                                      account_label=acct.label,
                                      advertiser_id=acct.advertiser_id,
                                      plan_name=plan.display_name())
                    rec.message = f"{type(e).__name__}: {e}"
                _log("  " + rec.summary())
            apply_org_fields(rec, acct)      # 无适配器/漏网异常行也带组织维度（幂等）
            records.append(rec)
            done += 1
            if progress:
                progress(done, total, plan.display_name())

    ok = sum(1 for r in records if r.ok)
    _log(f"搭建完成：成功 {ok} / 共 {len(records)}（失败 {len(records) - ok}）")
    return records
