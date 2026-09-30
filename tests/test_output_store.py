"""
tests/test_output_store.py —— 成品库只读视图（扫盘 + join 标记）

用 tmp_path 当扫描根，造几个视频/非视频文件，验证：只认视频扩展名、按修改时间倒序、
产品/标签/标记过滤。标记 join 走 file_marks 表（按 resolve 后的真实路径匹配）。
"""
from pathlib import Path

from store import db, output_store


def _touch(p, secs=0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    import os
    os.utime(p, (1_700_000_000 + secs, 1_700_000_000 + secs))
    return p


def test_iter_only_videos_sorted(tmp_path):
    a = _touch(tmp_path / "a.mp4", secs=10)
    b = _touch(tmp_path / "sub" / "b.mov", secs=20)
    _touch(tmp_path / "note.txt", secs=30)          # 非视频不入选
    rows = output_store.iter_outputs(root=tmp_path)
    paths = {Path(r["path"]) for r in rows}
    assert paths == {a, b}
    assert rows[0]["path"] == str(b)                # mtime 新的在前


def test_mark_join_and_filter(tmp_path):
    bad = _touch(tmp_path / "bad.mp4", secs=5)
    ok = _touch(tmp_path / "ok.mp4", secs=6)
    db.execute("INSERT OR REPLACE INTO file_marks(path, mark) VALUES(?, 'bad')",
                (str(Path(bad).resolve()),))
    try:
        rows = output_store.list_outputs(root=tmp_path)
        by = {r["name"]: r for r in rows}
        assert by["bad.mp4"]["mark"] == "bad"
        assert by["ok.mp4"]["mark"] == ""
        only_bad = output_store.list_outputs(mark="bad", root=tmp_path)
        assert [r["name"] for r in only_bad] == ["bad.mp4"]
        hidden = output_store.list_outputs(hide_bad=True, root=tmp_path)
        assert "bad.mp4" not in [r["name"] for r in hidden]
    finally:
        db.execute("DELETE FROM file_marks")


def test_to_clip_shape(tmp_path):
    p = _touch(tmp_path / "x.mp4")
    c = output_store.to_clip(str(p))
    assert c["path"] == str(p) and c["block_type"] == "成品"
    assert c["label"] == "x.mp4"


def test_bind_product_and_unbound_filter(tmp_path):
    """人工绑定产品优先于自动 join；绑定后 bound=True；未分类筛未归属的行。"""
    a = _touch(tmp_path / "a.mp4", secs=10)
    b = _touch(tmp_path / "b.mp4", secs=20)
    try:
        # 初始都未分类
        assert output_store.list_outputs(product=output_store.UNBOUND, root=tmp_path)
        output_store.bind(str(a), "关节钙", product_id=7)
        by = {r["name"]: r for r in output_store.iter_outputs(root=tmp_path)}
        assert by["a.mp4"]["product"] == "关节钙" and by["a.mp4"]["bound"] is True
        assert by["b.mp4"]["product"] == "" and by["b.mp4"]["bound"] is False
        # 按产品过滤命中绑定行；未分类只剩 b
        assert [r["name"] for r in output_store.list_outputs(
            product="关节钙", root=tmp_path)] == ["a.mp4"]
        assert [r["name"] for r in output_store.list_outputs(
            product=output_store.UNBOUND, root=tmp_path)] == ["b.mp4"]
        # 解绑回未分类
        output_store.unbind(str(a))
        by = {r["name"]: r for r in output_store.iter_outputs(root=tmp_path)}
        assert by["a.mp4"]["product"] == "" and by["a.mp4"]["bound"] is False
    finally:
        db.execute("DELETE FROM output_binds")


def test_rename_migrates_bind_and_mark(tmp_path):
    """改名：盘上改名 + 人工绑定 / 审片标记跟着新路径走（不跟就成孤儿）。"""
    a = _touch(tmp_path / "a.mp4", secs=10)
    try:
        output_store.bind(str(a), "关节钙", product_id=7)
        db.execute("INSERT OR REPLACE INTO file_marks(path, mark) VALUES(?, 'bad')",
                    (str(Path(a).resolve()),))
        ok, new, _msg = output_store.rename(str(a), "01_关节钙")
        assert ok and Path(new).name == "01_关节钙.mp4"
        assert not a.exists() and Path(new).exists()
        by = {r["name"]: r for r in output_store.iter_outputs(root=tmp_path)}
        assert "01_关节钙.mp4" in by
        assert by["01_关节钙.mp4"]["product"] == "关节钙"
        assert by["01_关节钙.mp4"]["bound"] is True
        assert by["01_关节钙.mp4"]["mark"] == "bad"
    finally:
        db.execute("DELETE FROM output_binds")
        db.execute("DELETE FROM file_marks")


def test_rename_collision_gets_suffix(tmp_path):
    """目标名已被别的成品占着：挂 (2) 而不是覆盖。"""
    a = _touch(tmp_path / "a.mp4", secs=10)
    _touch(tmp_path / "taken.mp4", secs=5)
    try:
        ok, new, _msg = output_store.rename(str(a), "taken")
        assert ok and Path(new).name == "taken(2).mp4"
        assert Path(tmp_path / "taken.mp4").exists()   # 原占用者不动
    finally:
        db.execute("DELETE FROM output_binds")
        db.execute("DELETE FROM file_marks")


def test_batch_delete_clears_bind(tmp_path, monkeypatch):
    """批量删：回收站成功后清掉按路径存的绑定 / 标记（回收站用假删模拟）。"""
    import utils.desktop_utils as du
    a = _touch(tmp_path / "a.mp4", secs=10)

    def _fake_trash(paths):
        for p in paths:
            Path(p).unlink(missing_ok=True)
        return list(paths), []

    monkeypatch.setattr(du, "move_to_trash", _fake_trash)
    try:
        output_store.bind(str(a), "关节钙", product_id=7)
        db.execute("INSERT OR REPLACE INTO file_marks(path, mark) VALUES(?, 'bad')",
                    (str(Path(a).resolve()),))
        ok, failed = output_store.batch_delete([str(a)])
        assert ok == [str(a)] and not failed
        assert not a.exists()
        assert output_store.binds_map() == {}
        assert db.query("SELECT * FROM file_marks") == []
    finally:
        db.execute("DELETE FROM output_binds")
        db.execute("DELETE FROM file_marks")


def test_rename_field_render():
    """批量重命名拼名：build_context 取值 + render_stem 用分隔符连接、空段自动省略（已去 lambda）。

    字段渲染不再有 per-field lambda 表：统一 build_context(row, idx) 造取值字典，
    render_stem 按 tokens 顺序取值、各自 sanitize、丢空段、用分隔符连接。"""
    from gui.dialogs_rename import build_context, render_stem
    row = {"name": "001_关节不舒服.mp4", "product": "骨胶原", "tag": "开场钩子",
           "created_at": "2026-09-23 10:32", "source_task_id": 128}
    ctx = build_context(row, 1)
    assert ctx["seq"] == "001"
    assert ctx["stem"] == "001_关节不舒服"
    assert ctx["product"] == "骨胶原"
    assert ctx["tag"] == "开场钩子"
    assert ctx["date"] == "0923"
    assert ctx["date_full"] == "20260923"
    assert ctx["time"] == "1032"
    assert ctx["task_id"] == "128"
    # 拼名：日期_品名_任务ID，用下划线连接
    assert render_stem(["date", "product", "task_id"], "_", ctx) == "0923_骨胶原_128"
    # 空字段那段自动丢弃：不留孤立分隔符
    empty = build_context({"name": "x.mp4"}, 1)
    assert render_stem(["date", "product", "stem"], "_", empty) == "x"


def _patch_trash(tmp_path, monkeypatch):
    """把回收站目录/扫描根都接回 tmp_path（否则测试会往真实 DOWNLOAD_DIR 下写）。"""
    tr = tmp_path / "_回收站"
    monkeypatch.setattr(output_store, "scan_root", lambda: tmp_path)
    monkeypatch.setattr(output_store, "trash_root", lambda: tr)
    return tr


def test_to_trash_hides_from_lib_and_lists(tmp_path, monkeypatch):
    """移入回收站：物理搬进回收站目录、成品库视图消失、回收站列表能看到。"""
    tr = _patch_trash(tmp_path, monkeypatch)
    a = _touch(tmp_path / "a.mp4", secs=10)
    try:
        ok, failed = output_store.to_trash([str(a)])
        assert ok and not failed
        assert not a.exists()                       # 原位置文件已搬走
        assert (tr / "a.mp4").exists()              # 物理挪到回收站目录
        assert [r["name"] for r in output_store.iter_outputs(root=tmp_path)] == []
        listed = output_store.list_trash()
        assert len(listed) == 1 and Path(listed[0]["path"]).exists()
    finally:
        db.execute("DELETE FROM output_trash")


def test_restore_moves_back_and_keeps_metadata(tmp_path, monkeypatch):
    """还原：物理移回原目录、重回成品库；绑定与外显名随路径迁回，不一挪就变孤儿。"""
    _patch_trash(tmp_path, monkeypatch)
    a = _touch(tmp_path / "a.mp4", secs=10)
    try:
        output_store.bind(str(a), "关节钙", product_id=7)
        output_store.set_display(str(a), "第一章")
        ok, _f = output_store.to_trash([str(a)])
        assert ok
        assert output_store.binds_map() != {}       # 绑定跟着迁到了回收站新路径
        r_ok, r_fail = output_store.restore(ok)
        assert r_ok and not r_fail
        assert (tmp_path / "a.mp4").exists()
        assert [r["name"] for r in output_store.iter_outputs(root=tmp_path)] == ["a.mp4"]
        assert output_store.list_trash() == []
        by = {r["name"]: r for r in output_store.iter_outputs(root=tmp_path)}
        assert by["a.mp4"]["product"] == "关节钙"
        assert by["a.mp4"]["display"] == "第一章"
    finally:
        db.execute("DELETE FROM output_trash")
        db.execute("DELETE FROM output_binds")
        db.execute("DELETE FROM output_display")


def test_purge_deletes_for_real(tmp_path, monkeypatch):
    """彻底删除：真删磁盘文件并清掉回收站行与按路径存的元数据。"""
    tr = _patch_trash(tmp_path, monkeypatch)
    a = _touch(tmp_path / "a.mp4", secs=10)
    try:
        ok, _f = output_store.to_trash([str(a)])
        output_store.bind(ok[0], "关节钙")
        assert (tr / "a.mp4").exists()
        p_ok, p_fail = output_store.purge(ok)
        assert p_ok and not p_fail
        assert not (tr / "a.mp4").exists()          # 物理真删
        assert output_store.list_trash() == []
        assert output_store.binds_map() == {}
    finally:
        db.execute("DELETE FROM output_trash")
        db.execute("DELETE FROM output_binds")


def test_default_display_falls_back_to_stem(tmp_path):
    """外显名口径：未自定义回落到「文件名去后缀」；自定义后用自定义。"""
    p = _touch(tmp_path / "001_关节不舒服.mp4")
    try:
        output_store.set_display(str(p), "第一章开场")
        row = {"name": p.name, "display": output_store.display_map().get(output_store._norm(p), "")}
        assert output_store.default_display(row) == "第一章开场"
        output_store.set_display(str(p), "")         # 清除自定义
        assert output_store.display_map() == {}
        row2 = {"name": p.name, "display": ""}
        assert output_store.default_display(row2) == "001_关节不舒服"
    finally:
        db.execute("DELETE FROM output_display")
