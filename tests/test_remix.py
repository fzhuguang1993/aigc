"""
tests/test_remix.py —— AI 混剪（排序建议 + 拼接清单 + 导出）

覆盖：suggest_order 合法排列采纳 / 非法回退优先级 / 调用异常回退 / 单片段原序；
apply_order 补齐保证不丢；plan_remix 目录命名与规格探测重编码；run_remix 假 concat
断言按顺序拼接与落库路径。不依赖真 ffmpeg / 真豆包。
"""
from pathlib import Path

from video_text_tools.remix import remixer


class FakeVision:
    def __init__(self, reply=None, boom=False):
        self._reply = reply
        self._boom = boom
        self.calls = 0

    def chat_text(self, prompt, **kw):
        self.calls += 1
        if self._boom:
            raise RuntimeError("豆包没配好")
        return self._reply


def _clip(bt, dur=3.0, path="x.mp4", label=""):
    return {"path": path, "block_type": bt, "duration": dur, "label": label or bt}


# ---------------- suggest_order ----------------
def test_suggest_order_valid_perm():
    clips = [_clip("钩子"), _clip("痛点"), _clip("行动号召")]
    v = FakeVision("[2,0,1]")
    assert remixer.suggest_order(clips, vision=v) == [2, 0, 1]
    assert v.calls == 1


def test_suggest_order_object_reply():
    clips = [_clip("钩子"), _clip("痛点")]
    v = FakeVision('{"order":[1,0]}')
    assert remixer.suggest_order(clips, vision=v) == [1, 0]


def test_suggest_order_invalid_falls_back_priority():
    # 模型回非法排列（越界/缺项）→ 回退按板块优先级排（钩子在前、号召在后）
    clips = [_clip("行动号召"), _clip("钩子"), _clip("痛点")]
    v = FakeVision("[9,9]")
    order = remixer.suggest_order(clips, vision=v)
    assert sorted(order) == [0, 1, 2]              # 仍是完整排列，不丢片段
    assert [clips[i]["block_type"] for i in order] == ["钩子", "痛点", "行动号召"]


def test_suggest_order_exception_falls_back():
    clips = [_clip("行动号召"), _clip("钩子")]
    v = FakeVision(boom=True)
    order = remixer.suggest_order(clips, vision=v)
    assert order == [1, 0]                          # 优先级兜底：钩子提到最前


def test_suggest_order_single_no_call():
    v = FakeVision("[0]")
    assert remixer.suggest_order([_clip("钩子")], vision=v) == [0]
    assert v.calls == 0                             # 一条没必要问模型


def test_apply_order_completes_missing():
    clips = [_clip("a"), _clip("b"), _clip("c")]
    # 排列只给了两个：遗漏的那个补在末尾，绝不丢
    out = remixer.apply_order(clips, [1, 0])
    assert [c["block_type"] for c in out] == ["b", "a", "c"]


# ---------------- plan_remix ----------------
def test_plan_remix_dir_and_name(tmp_path):
    clips = [{"path": str(tmp_path / "a.mp4"), "block_type": "钩子", "duration": 3}]
    out_dir = tmp_path / "成品库" / "混剪"
    plan = remixer.plan_remix(clips, name="骨胶原混剪", out_dir=out_dir, reencode=False)
    dst = Path(plan["dst"])
    assert dst.parent == out_dir and out_dir.exists()
    assert dst.suffix == ".mp4" and "骨胶原混剪" in dst.name
    assert plan["reencode"] is False
    assert plan["clips"] == [clips[0]["path"]]


def test_plan_remix_empty(tmp_path):
    assert remixer.plan_remix([], out_dir=tmp_path) is None


def test_plan_remix_reencode_auto_detect(tmp_path):
    clips = [{"path": "a.mp4", "block_type": "钩子"}, {"path": "b.mp4", "block_type": "痛点"}]

    def probe_same(p):
        return {"width": "1080", "height": "1920", "fps": "30.00"}

    def probe_diff(p):
        return {"width": "1080", "height": "1920",
                "fps": "30.00"} if p.endswith("a.mp4") else {"width": "720",
                                                             "height": "1280",
                                                             "fps": "25.00"}
    p1 = remixer.plan_remix(clips, out_dir=tmp_path / "m", probe=probe_same)
    p2 = remixer.plan_remix(clips, out_dir=tmp_path / "m", probe=probe_diff)
    assert p1["reencode"] is False and p2["reencode"] is True


# ---------------- run_remix ----------------
def test_run_remix_fake_concat(tmp_path):
    seen = {}

    def fake_concat(clips, dst, reencode=False, **kw):
        seen["clips"] = list(clips)
        seen["dst"] = dst
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"v")
        return True, ""

    clips = [_clip("痛点", path=str(tmp_path / "b.mp4")),
             _clip("钩子", path=str(tmp_path / "a.mp4"))]
    res = remixer.run_remix(clips, name="混剪A", out_dir=tmp_path / "m",
                            reencode=False, order=[1, 0], concat=fake_concat)
    assert res["ok"] is True and Path(res["dst"]).exists()
    # 按 order 重排后：钩子(索引1)在前
    assert seen["clips"] == [str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")]
    assert res["count"] == 2


def test_run_remix_empty():
    res = remixer.run_remix([])
    assert res["ok"] is False and res["reason"] == "无可拼接片段"
