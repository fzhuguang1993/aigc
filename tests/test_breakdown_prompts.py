# -*- coding: utf-8 -*-
"""tests/test_breakdown_prompts.py —— 分镜提示词/改写「分批防截断」

钉住实测 bug：60 分镜×5字段单次请求被 max_tokens 截断 → _extract_json 解析不出
→ 整批降级"空/非数组"。修法是分批（每批≤_PROMPT_BATCH）+ 按位置对齐 + 缺的逐条兜底。
"""
import json

from video_text_tools.breakdown import prompts as P
from video_text_tools.breakdown.models import SegmentPrompts


def _shots(n):
    return [{"idx": i + 1, "start": float(i * 3), "end": float(i * 3 + 3),
             "time_range": f"{i * 3 // 60:02d}:{i * 3 % 60:02d}-"
                           f"{(i * 3 + 3) // 60:02d}:{(i * 3 + 3) % 60:02d}",
             "shot_size": "特写", "camera": "推", "composition": "居中",
             "transition": "硬切", "on_screen_text": "", "emotion": "紧迫",
             "raw": "", "speech": f"第{i + 1}句口播"} for i in range(n)]


class FakeVision:
    """chat_text 每次回 items 个提示词条目（或按 raise_ 抛异常）；记录调用次数。"""

    def __init__(self, items, raise_=False):
        self.items = items
        self.raise_ = raise_
        self.calls = 0

    def _payload(self):
        rows = [{"index": i + 1, "time_range": "00:00-00:03", "summary": "s",
                 "visual_prompt": f"V{i + 1}", "copy_prompt": f"C{i + 1}",
                 "shoot_prompt": f"S{i + 1}"} for i in range(self.items)]
        return json.dumps(rows, ensure_ascii=False)

    def chat_text(self, prompt, temperature=0.4, max_tokens=2048):
        self.calls += 1
        if self.raise_:
            raise ValueError("豆包生成超时（180s 无响应）")
        return self._payload()


def test_build_prompts_batches_large_input():
    """30 分镜 → 分 3 批（每批≤12），每批都拿到模型条目、返回满 30 条。"""
    v = FakeVision(items=12)
    logs = []
    out = P.build_prompts(_shots(30), v, log=logs.append)
    assert v.calls == 3                       # ceil(30/12)
    assert len(out) == 30
    assert all(o.visual_prompt.startswith("V") for o in out)   # 全部来自模型
    assert any("分 3 批" in l for l in logs)


def test_build_prompts_partial_fills_fallback():
    """模型每批只回 1 条 → 30 条里 3 条来自模型、其余逐条降级画面字段直拼，仍满 30。"""
    v = FakeVision(items=1)
    logs = []
    out = P.build_prompts(_shots(30), v, log=logs.append)
    assert len(out) == 30
    assert sum(1 for o in out if o.visual_prompt.startswith("V")) == 3   # 3 批各 1 条
    joined = "\n".join(logs)
    assert "分镜提示词完成" in joined and "降级" in joined


def test_build_prompts_all_fail_logs_generation_failed():
    """整批全抛异常 → n_model=0，仍返回兜底结果，并明确记 '生成失败'（对齐旧语义）。"""
    v = FakeVision(items=0, raise_=True)
    logs = []
    out = P.build_prompts(_shots(30), v, log=logs.append)
    assert len(out) == 30
    assert all(o.visual_prompt and not o.visual_prompt.startswith("V") for o in out)
    assert any("分镜提示词生成失败" in l for l in logs)


def test_rewrite_for_dedup_batches():
    """60 条文案 → 分 2 批（每批≤30），逐条改写成功、调用 2 次。"""
    segs = [SegmentPrompts(index=i + 1, time_range="", visual_prompt="",
                           copy_prompt=f"原{i + 1}", shoot_prompt="", summary="")
            for i in range(60)]

    class Rv:
        def __init__(self):
            self.calls = 0

        def chat_text(self, prompt, temperature=0.4, max_tokens=2048):
            self.calls += 1
            return json.dumps([{"index": i, "copy_prompt": f"R{i}"}
                               for i in range(1, 61)], ensure_ascii=False)

    v = Rv()
    logs = []
    ok = P.rewrite_for_dedup(segs, v, log=logs.append)
    assert ok and v.calls == 2
    assert all(s.copy_prompt.startswith("R") for s in segs)
