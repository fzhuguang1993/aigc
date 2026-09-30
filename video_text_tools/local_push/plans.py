"""
video_text_tools/local_push/plans.py —— 计划方案模板读写门面（对接 core.config）

方案 = 项目/营销层参数 + 素材清单（见 models.PlanItem），无敏感信息，
明文存 config.json 的 local_plans 段。本模块只做 PlanItem ↔ dict 的互转，
读写全在 core.config（list/save/delete_local_plan）。
"""
from dataclasses import asdict, fields

from .models import PlanItem

# 只认 PlanItem 声明的字段：config 里夹带的未知键（旧版本/手改）读时丢弃
_FIELDS = {f.name for f in fields(PlanItem)}


def list_plans():
    """全部计划方案（PlanItem 列表）。"""
    from core.config import list_local_plans
    return [PlanItem(**{k: v for k, v in d.items() if k in _FIELDS})
            for d in list_local_plans()]


def save_plan(plan: PlanItem):
    """新增或按 id 覆盖保存一个方案；返回带 id 的方案。"""
    from core.config import save_local_plan
    plan.id = save_local_plan(asdict(plan))
    return plan


def delete_plan(plan_id):
    from core.config import delete_local_plan
    return delete_local_plan(plan_id)
