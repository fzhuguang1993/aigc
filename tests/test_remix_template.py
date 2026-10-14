"""
tests/test_remix_template.py —— AI 混剪双模式 Dialog 的拼装口径

这两个 Dialog 最容易出错、也最该锁死的是「拼装信号」本身：
- AI 自动拼装：段落顺序必须跟 block_categories.load() 一致（不是素材入库顺序、
  不是字典序），每段最多取 N 条、段内按时长降序——这是「AI 混剪 = 按板块结构排片」
  这句承诺的可执行契约。
- 自定义模板：右列候选必须只显示当前槽 block_type 的素材（选错类别＝拼出四不像），
  result_clips 必须按段落序列顺序回吐（用户就是照这个顺序排的片）。

用 offscreen QApplication 构造 Dialog，不 exec、不弹窗；类别清单 monkeypatch 成
固定序列，素材池直接传 clip dict（不碰 material_store），断言只看两条纯逻辑。
"""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from core import block_categories
from store import app_state
from gui.dialogs_remix_template import AutoTemplateDialog, TemplateEditorDialog


# 固定段落序列：测拼装顺序就靠它，故意与素材入库/字典序都不同
SEQ = ["钩子", "痛点", "产品介绍", "行动号召"]


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # 类别清单锁成 SEQ；app_state 隔到临时文件（避免上次模板残留影响默认段序列）
    monkeypatch.setattr(block_categories, "load", lambda: list(SEQ))
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")


def _clip(bt, name, dur=10.0, prod=""):
    path = f"/vids/{name}"
    return {"path": path, "block_type": bt, "product": prod,
            "duration": dur, "label": Path(path).name}


# ============================================================
# AI 自动拼装
# ============================================================

def test_auto_template_orders_by_block_categories(qapp):
    """拼装结果段落顺序须等于 SEQ，段内按时长降序——不受素材传入顺序影响。"""
    clips = [
        _clip("行动号召", "cta.mp4", 5),
        _clip("钩子", "h1.mp4", 10),
        _clip("痛点", "pain.mp4", 8),
        _clip("钩子", "h2.mp4", 20),
        _clip("产品介绍", "intro.mp4", 12),
    ]
    dlg = AutoTemplateDialog(None, clips)
    dlg._spin.setValue(5)                       # 放开每段上限，看完整排序
    result = dlg._compute_result()
    assert [c["block_type"] for c in result] == \
        ["钩子", "钩子", "痛点", "产品介绍", "行动号召"]
    # 同段（钩子）内按时长降序：h2(20s) 在 h1(10s) 前
    assert [c["label"] for c in result[:2]] == ["h2.mp4", "h1.mp4"]


def test_auto_template_caps_each_slot_and_picks_longest(qapp):
    """每段最多 N 条：置 1 时每个 block_type 只留时长最长的那一条。"""
    clips = [
        _clip("钩子", "h1.mp4", 10),
        _clip("钩子", "h2.mp4", 20),
        _clip("痛点", "pain.mp4", 8),
        _clip("产品介绍", "intro.mp4", 12),
        _clip("行动号召", "cta.mp4", 5),
    ]
    dlg = AutoTemplateDialog(None, clips)
    dlg._spin.setValue(1)
    result = dlg._compute_result()
    assert [c["block_type"] for c in result] == SEQ
    assert result[0]["label"] == "h2.mp4"        # 钩子里最长的一条


def test_auto_template_skips_empty_categories(qapp):
    """SEQ 里没素材的段落应被跳过，不产出空槽、不打乱其余顺序。"""
    clips = [_clip("行动号召", "cta.mp4", 5), _clip("钩子", "h.mp4", 10)]
    dlg = AutoTemplateDialog(None, clips)
    result = dlg._compute_result()
    assert [c["block_type"] for c in result] == ["钩子", "行动号召"]


def test_auto_template_product_filter(qapp):
    """产品筛选生效时只取该产品的素材；其它产品的同段落被排除。"""
    clips = [
        _clip("钩子", "a_hook.mp4", 10, prod="A"),
        _clip("钩子", "b_hook.mp4", 30, prod="B"),
        _clip("行动号召", "a_cta.mp4", 5, prod="A"),
    ]
    dlg = AutoTemplateDialog(None, clips, products=["A", "B"])
    dlg.cb_prod.setCurrentText("A")
    result = dlg._compute_result()
    assert {c["product"] for c in result} == {"A"}
    assert [c["label"] for c in result] == ["a_hook.mp4", "a_cta.mp4"]


def test_auto_template_result_clips_matches_compute(qapp):
    """_do_accept 后 result_clips() 应等于刚算出的有序清单。"""
    clips = [_clip("钩子", "h.mp4", 10), _clip("痛点", "p.mp4", 8)]
    dlg = AutoTemplateDialog(None, clips)
    dlg._do_accept()
    assert [c["block_type"] for c in dlg.result_clips()] == ["钩子", "痛点"]


# ============================================================
# 自定义模板拼装
# ============================================================

def _cand_clips():
    return [
        _clip("钩子", "h1.mp4", 10),
        _clip("钩子", "h2.mp4", 20),
        _clip("痛点", "pain.mp4", 8),
        _clip("产品介绍", "intro.mp4", 12),
    ]


def _cand_block_types(dlg):
    out = []
    for i in range(dlg._cand_list.count()):
        c = dlg._cand_list.item(i).data(0x0100)   # Qt.ItemDataRole.UserRole
        if isinstance(c, dict):
            out.append(c["block_type"])
    return out


def test_template_editor_defaults_to_category_sequence(qapp):
    """无历史模板时，默认段落序列取 block_categories 前 6 项（这里即整份 SEQ）。"""
    dlg = TemplateEditorDialog(None, _cand_clips())
    assert [s["block_type"] for s in dlg._slots] == SEQ
    assert dlg._current_idx == 0


def test_template_editor_candidates_filtered_by_slot_type(qapp):
    """选中某槽后，右列候选只显示该 block_type 的素材。"""
    dlg = TemplateEditorDialog(None, _cand_clips())
    dlg._slot_list.setCurrentRow(0)               # 钩子槽
    assert set(_cand_block_types(dlg)) == {"钩子"}
    dlg._slot_list.setCurrentRow(1)               # 痛点槽
    assert set(_cand_block_types(dlg)) == {"痛点"}


def test_template_editor_candidate_click_binds_current_slot(qapp):
    """单击候选 → 绑定到当前槽，且只绑该类素材。"""
    dlg = TemplateEditorDialog(None, _cand_clips())
    dlg._slot_list.setCurrentRow(0)               # 钩子
    # 点第一条候选（钩子）
    dlg._cand_list.item(0).clicked.emit() if hasattr(dlg._cand_list.item(0), "clicked") \
        else dlg._on_candidate_clicked(dlg._cand_list.item(0))
    assert dlg._slots[0]["clip"] is not None
    assert dlg._slots[0]["clip"]["block_type"] == "钩子"
    assert dlg._slots[1]["clip"] is None           # 别的槽不受影响


def test_template_editor_result_follows_slot_order(qapp):
    """result_clips 严格按段落序列顺序回吐，跳过未选段。"""
    dlg = TemplateEditorDialog(None, _cand_clips())
    hook = _clip("钩子", "h2.mp4", 20)
    intro = _clip("产品介绍", "intro.mp4", 12)
    dlg._slots[0]["clip"] = hook
    dlg._slots[2]["clip"] = intro                  # 槽 1/3 留空
    assert dlg.result_clips() == [hook, intro]


def test_template_editor_move_slot_changes_order(qapp):
    """上/下移段落会改变 result_clips 顺序——这是自定义拼装的核心可控性。"""
    dlg = TemplateEditorDialog(None, _cand_clips())
    dlg._slots[0]["clip"] = _clip("钩子", "h.mp4", 10)
    dlg._slots[1]["clip"] = _clip("痛点", "p.mp4", 8)
    dlg._slot_list.setCurrentRow(1)                # 选中痛点
    dlg._move_slot(-1)                             # 上移到钩子前
    order = [c["block_type"] for c in dlg.result_clips()]
    assert order == ["痛点", "钩子"]
