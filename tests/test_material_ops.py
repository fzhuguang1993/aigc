"""
tests/test_material_ops.py —— 素材库批量操作的数据层（绑产品 / 改名 / 删文件）

对应 pages_material 顶部「操作」三件事，全走 material_store：
- set_products：批量改 product 列；
- rename：改盘上文件 + 同步索引 path；
- delete_with_files：文件进回收站（用假删模拟）+ 删索引，删失败的保留索引行。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 早于任何 PySide6 导入

from pathlib import Path

from store import db, material_store


def _mk(tmp_path, name, product="", block_type=""):
    p = tmp_path / name
    p.write_bytes(b"x")
    return material_store.add(str(p), block_type=block_type, product=product)


def _cleanup():
    db.execute("DELETE FROM material_clips")


def test_set_products_batch():
    _cleanup()
    try:
        a = material_store.add("C:/fake/a.mp4", product="")
        b = material_store.add("C:/fake/b.mp4", product="旧品")
        n = material_store.set_products([a, b], "骨胶原")
        assert n == 2
        assert material_store.get(a)["product"] == "骨胶原"
        assert material_store.get(b)["product"] == "骨胶原"
        # 传空 = 清空归属
        material_store.set_products([a], "")
        assert material_store.get(a)["product"] == ""
    finally:
        _cleanup()


def test_rename_syncs_index_path(tmp_path):
    _cleanup()
    try:
        cid = _mk(tmp_path, "old.mp4", product="骨胶原")
        old = material_store.get(cid)["path"]
        ok, new, _msg = material_store.rename(cid, "01_骨胶原")
        assert ok and Path(new).name == "01_骨胶原.mp4"
        assert not Path(old).exists() and Path(new).exists()
        assert material_store.get(cid)["path"] == str(new)   # 索引跟着走
    finally:
        _cleanup()


def test_rename_collision_gets_suffix(tmp_path):
    _cleanup()
    try:
        cid = _mk(tmp_path, "a.mp4")
        _mk(tmp_path, "taken.mp4")
        ok, new, _msg = material_store.rename(cid, "taken")
        assert ok and Path(new).name == "taken(2).mp4"
        assert Path(tmp_path / "taken.mp4").exists()
    finally:
        _cleanup()


def test_delete_with_files(tmp_path, monkeypatch):
    import utils.desktop_utils as du
    _cleanup()
    try:
        a = _mk(tmp_path, "a.mp4")
        b = _mk(tmp_path, "b.mp4")

        def _fake_trash(paths):
            for p in paths:
                Path(p).unlink(missing_ok=True)
            return list(paths), []

        monkeypatch.setattr(du, "move_to_trash", _fake_trash)
        done, failed = material_store.delete_with_files([a, b])
        assert sorted(done) == sorted([a, b]) and not failed
        assert material_store.get(a) is None and material_store.get(b) is None
        assert not (tmp_path / "a.mp4").exists()
    finally:
        _cleanup()


def test_delete_with_files_keeps_index_on_failure(tmp_path, monkeypatch):
    """被占用删不成的，保留索引行（别做成盘上还在、索引没了的孤儿）。"""
    import utils.desktop_utils as du
    _cleanup()
    try:
        a = _mk(tmp_path, "a.mp4")
        b = _mk(tmp_path, "b.mp4")

        def _partial(paths):
            # 只删掉 b.mp4，a.mp4 假装失败
            for p in paths:
                if Path(p).name == "b.mp4":
                    Path(p).unlink(missing_ok=True)
                    continue
            ok = [p for p in paths if not Path(p).exists()]
            bad = [(p, "被占用") for p in paths if Path(p).exists()]
            return ok, bad

        monkeypatch.setattr(du, "move_to_trash", _partial)
        done, failed = material_store.delete_with_files([a, b])
        assert len(done) == 1 and len(failed) == 1
        assert material_store.get(a) is not None      # 失败那条索引保留
        assert material_store.get(b) is None          # 成功那条删了
    finally:
        _cleanup()


def test_material_field_render():
    """素材字段取值：build_context 统一造字典（已去 per-field lambda）。"""
    from gui.dialogs_rename import build_context
    row = {"path": "素材库/骨胶原/钩子/0000-0003.mp4", "block_type": "钩子",
           "product": "骨胶原", "created_at": "2026-09-23 10:32:00",
           "source_task_id": 42}
    ctx = build_context(row, 1)
    assert ctx["seq"] == "001"
    assert ctx["stem"] == "0000-0003"
    assert ctx["block_type"] == "钩子"
    assert ctx["product"] == "骨胶原"
    assert ctx["date"] == "0923"
    assert ctx["time"] == "1032"
    assert ctx["task_id"] == "42"


def test_batch_rename_dialog_construct_and_plan():
    """实例化弹窗走一遍取名链路：防属性把同名方法遮蔽那类 bug（单测不弹窗）。"""
    from PySide6.QtWidgets import QApplication
    from gui.dialogs_rename import BatchRenameDialog, MATERIAL_FIELDS, OUTPUT_FIELDS

    app = QApplication.instance() or QApplication([])  # noqa: F841
    rows = [{"path": "素材库/a.mp4", "block_type": "钩子", "product": "骨胶原",
             "created_at": "2026-09-23 10:32:00", "source_task_id": 7},
            {"path": "素材库/b.mp4", "block_type": "行动号召", "product": "",
             "created_at": "2026-09-23 10:33:00", "source_task_id": 7}]
    dlg = BatchRenameDialog(None, rows, fields=MATERIAL_FIELDS, title="批量重命名素材")
    assert dlg._tokens == ["seq", "stem"]          # 默认字段顺序
    plan = [s for _r, s in dlg._build_plan()]
    assert plan == ["001_a", "002_b"], plan            # 素材行没 name，stem 取 path

    # 成品那套也要能构造（fields 默认分支）
    out_rows = [{"name": "001_关节.mp4", "path": "成品/001_关节.mp4",
                 "product": "骨胶原", "created_at": "2026-09-23 10:32:00"}]
    dlg2 = BatchRenameDialog(None, out_rows, fields=OUTPUT_FIELDS)
    assert [s for _r, s in dlg2._build_plan()] == ["001_001_关节"]
