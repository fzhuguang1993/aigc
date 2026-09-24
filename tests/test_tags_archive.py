"""
tests/test_tags_archive.py —— 内容标签词库 + 任务 tag 字段 + 批量归档

覆盖三块：
- core.tags：清洗/去重/兜底、写盘读回、坏文件退默认；
- task_store：tag 作为普通业务列的 CRUD 与筛选候选；
- processors.archiver：以库为准把成品搬进「日期/产品/标签」，同步输出路径与
  审片标记，且幂等（已归档的不再动、孤儿文件不认）。
"""
import json

import pytest

from core import tags as tag_lib
from store import db, task_store
from processors import archiver


@pytest.fixture
def tag_home(tmp_path, monkeypatch):
    """把词库读写指到临时 config.json，并清空模块缓存（不吃上一个用例的值）"""
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(tag_lib, "CONFIG_JSON", cfg)
    monkeypatch.setattr(tag_lib, "_CACHE", None)
    yield cfg
    tag_lib._CACHE = None


# ---------------- core.tags ----------------

def test_tags_default_when_no_config(tag_home):
    assert tag_lib.load() == list(tag_lib.DEFAULT_TAGS)


def test_tags_normalize_dedups_and_falls_back():
    # 去空、去重、保序；顺手把路径分隔符换成下划线（标签＝目录名）
    assert tag_lib.normalize(["a", " a ", "b", "a", "", "x/y"]) == ["a", "b", "x_y"]
    assert tag_lib.normalize([]) == list(tag_lib.DEFAULT_TAGS)
    assert tag_lib.normalize(["", "  "]) == list(tag_lib.DEFAULT_TAGS)


def test_tags_persist_roundtrip_and_add_remove(tag_home):
    tag_lib.set_all(["开场钩子", "情景剧"])
    assert json.loads(tag_home.read_text(encoding="utf-8"))["tags"] == ["开场钩子", "情景剧"]
    # 合并写回不吞其它字段
    data = json.loads(tag_home.read_text(encoding="utf-8"))
    data["user_name"] = "罗成"
    tag_home.write_text(json.dumps(data), encoding="utf-8")
    tag_lib.add("认证背书")
    got = tag_lib.load()
    assert got == ["开场钩子", "情景剧", "认证背书"]
    assert json.loads(tag_home.read_text(encoding="utf-8"))["user_name"] == "罗成"  # 没被写没
    tag_lib.remove("情景剧")
    assert "情景剧" not in tag_lib.load()


def test_tags_bad_config_falls_back(tag_home):
    tag_home.write_text("{这不是json", encoding="utf-8")
    assert tag_lib.load(force=True) == list(tag_lib.DEFAULT_TAGS)


# ---------------- task_store：tag 字段 ----------------

def test_task_tag_crud_and_filter_choices():
    tid = task_store.add_task("1", "诺特兰德VB", "提示词X")
    task_store.update_row(tid, **{"标签": "开场钩子"})
    assert task_store.get_task(tid)["tag"] == "开场钩子"
    ch = task_store.filter_choices()
    assert "开场钩子" in ch["tags"]
    # 列表 df 里带着中文「标签」列（供表格显示/导出）
    df = task_store.list_tasks_df()
    assert "标签" in df.columns and str(df.iloc[0]["标签"]) == "开场钩子"


# ---------------- processors.archiver ----------------

@pytest.fixture
def arch_home(tmp_path, monkeypatch):
    """把输出根目录指到临时目录，返回 outputs 根 Path"""
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(archiver, "DOWNLOAD_DIR", str(out))
    return out


def _seed_task(product, tag, out_path):
    tid = task_store.add_task("1", product, f"提示词_{tid_seed()}")
    if product is not None:
        task_store.update_row(tid, **{"品名": product})
    if tag:
        task_store.update_row(tid, **{"标签": tag})
    db.execute("UPDATE tasks SET output=? WHERE id=?", (out_path, tid))
    return tid


_counter = {"n": 0}


def tid_seed():
    _counter["n"] += 1
    return _counter["n"]


def test_archive_moves_and_syncs(arch_home):
    date_dir = arch_home / "0924"
    date_dir.mkdir()
    src = date_dir / "001_维B_0924_01_罗成.mp4"
    src.write_text("v", encoding="utf-8")
    tid = _seed_task("维生素B", "开场钩子", str(src))
    task_store.set_file_mark(str(src), task_store.MARK_OK)     # 已标可用

    items = archiver.plan()
    assert len(items) == 1 and items[0]["product"] == "维生素B"
    st = archiver.run(items)
    assert len(st["moved"]) == 1 and not st["failed"]

    dst = arch_home / "0924" / "维生素B" / "开场钩子" / src.name
    assert dst.is_file() and not src.exists()
    # 输出路径同步：任务表指向新位置（双击还能播）
    assert task_store.get_task(tid)["output"] == str(dst)
    # 审片标记跟着搬家：新路径仍记着「可用」，老路径不再有孤儿标记
    assert task_store.get_file_mark(str(dst)) == task_store.MARK_OK
    assert task_store.get_file_mark(str(src)) == ""
    # 幂等：再点一次归档不该重复搬 / 报错
    assert archiver.plan() == []


def test_archive_untagged_and_missing_product(arch_home):
    date_dir = arch_home / "0925"
    date_dir.mkdir()
    f = date_dir / "x.mp4"
    f.write_text("v", encoding="utf-8")
    _seed_task("", "", str(f))                       # 无产品、无标签
    archiver.run()
    assert (arch_home / "0925" / archiver.UNPRODUCT / archiver.UNTAGGED / "x.mp4").is_file()


def test_archive_ignores_orphans_and_subdirs(arch_home):
    date_dir = arch_home / "0926"
    date_dir.mkdir()
    orphan = date_dir / "同事手丢的.mp4"
    orphan.write_text("v", encoding="utf-8")         # 库里没有：不认不动
    # 已在三层子目录里的（库里记着）：视为已归档，不再搬
    deep = date_dir / "维B" / "情景剧" / "y.mp4"
    deep.parent.mkdir(parents=True)
    deep.write_text("v", encoding="utf-8")
    _seed_task("维B", "情景剧", str(deep))
    assert archiver.plan() == []
    assert orphan.is_file()
