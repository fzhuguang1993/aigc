"""
tests/test_tags_archive.py —— 内容标签词库 + 任务 tag 字段 + 批量归档

覆盖三块：
- core.tags：清洗/去重/兜底、写盘读回、坏文件退默认；
- task_store：tag 作为普通业务列的 CRUD 与筛选候选；
- processors.archiver：以库为准把【标了可用】的成品搬进「产品/标签」，可选
  成品库 / 素材库两目的地，同步输出路径与审片标记；进素材库的登记 material_clips，
  且幂等（已入库的不再动、未标可用的不收、孤儿文件不认）。
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
    """把两个库的根指到临时目录，返回 {"gen": 生成目录, "output": 成品库根,
    "material": 素材库根}（archiver.roots 被改写，plan/run 都认这几个目录）。"""
    gen = tmp_path / "outputs"          # 任务刚出片的地方（在制品）
    gen.mkdir()
    out = tmp_path / "成品库"
    mat = tmp_path / "素材库"
    monkeypatch.setattr(archiver, "roots",
                        lambda: {"output": out, "material": mat})
    return {"gen": gen, "output": out, "material": mat}


def _seed_task(product, tag, out_path, mark=None):
    tid = task_store.add_task("1", product, f"提示词_{tid_seed()}")
    if product is not None:
        task_store.update_row(tid, **{"品名": product})
    if tag:
        task_store.update_row(tid, **{"标签": tag})
    db.execute("UPDATE tasks SET output=? WHERE id=?", (out_path, tid))
    if mark:
        task_store.set_file_mark(out_path, mark)
    return tid


_counter = {"n": 0}


def tid_seed():
    _counter["n"] += 1
    return _counter["n"]


def test_archive_moves_and_syncs(arch_home):
    """成品库归档：可用成品搬进 <成品库>/<产品>/<标签>/，输出路径与标记跟着走，幂等。"""
    date_dir = arch_home["gen"] / "0924"
    date_dir.mkdir()
    src = date_dir / "001_维B_0924_01_罗成.mp4"
    src.write_text("v", encoding="utf-8")
    tid = _seed_task("维生素B", "开场钩子", str(src), mark=task_store.MARK_OK)

    items = archiver.plan()
    assert len(items) == 1 and items[0]["product"] == "维生素B"
    st = archiver.run(items, dest=archiver.DEST_OUTPUT)
    assert len(st["moved"]) == 1 and not st["failed"]
    assert st["dest"] == archiver.DEST_OUTPUT

    dst = arch_home["output"] / "维生素B" / "开场钩子" / src.name
    assert dst.is_file() and not src.exists()
    # 输出路径同步：任务表指向新位置（双击还能播）
    assert task_store.get_task(tid)["output"] == str(dst)
    # 审片标记跟着搬家：新路径仍记着「可用」，老路径不再有孤儿标记
    assert task_store.get_file_mark(str(dst)) == task_store.MARK_OK
    assert task_store.get_file_mark(str(src)) == ""
    # 归档进成品库不登记素材库
    assert db.query("SELECT * FROM material_clips") == []
    # 幂等：再算一次清单不该重复搬（已在库目录下）
    assert archiver.plan() == []


def test_archive_only_usable(arch_home):
    """只收「可用」：未标记 / 标了不可用的成品不进归档清单（留在生成目录）。"""
    date_dir = arch_home["gen"] / "0927"
    date_dir.mkdir()
    unmarked = date_dir / "没标的.mp4"
    unmarked.write_text("v", encoding="utf-8")
    bad = date_dir / "不可用的.mp4"
    bad.write_text("v", encoding="utf-8")
    good = date_dir / "可用的.mp4"
    good.write_text("v", encoding="utf-8")
    _seed_task("P", "T", str(unmarked))                          # 未标
    _seed_task("P", "T", str(bad), mark=task_store.MARK_BAD)     # 不可用
    _seed_task("P", "T", str(good), mark=task_store.MARK_OK)     # 可用

    items = archiver.plan()
    assert len(items) == 1 and items[0]["src"] == str(good.resolve())
    archiver.run(items, dest=archiver.DEST_OUTPUT)
    # 未标 / 不可用的留在原地不动
    assert unmarked.is_file() and bad.is_file()


def test_archive_material_registers_clip(arch_home):
    """素材库归档：搬进 <素材库>/<产品>/<标签>/ 且登记一行 material_clips（类型=标签）。"""
    date_dir = arch_home["gen"] / "0928"
    date_dir.mkdir()
    src = date_dir / "clip.mp4"
    src.write_text("v", encoding="utf-8")
    _seed_task("蛋白粉", "情景剧", str(src), mark=task_store.MARK_OK)

    st = archiver.run(dest=archiver.DEST_MATERIAL)
    assert len(st["moved"]) == 1 and not st["failed"]
    dst = arch_home["material"] / "蛋白粉" / "情景剧" / "clip.mp4"
    assert dst.is_file() and not src.exists()
    rows = db.query("SELECT * FROM material_clips")
    assert len(rows) == 1
    assert rows[0]["path"] == str(dst)
    assert rows[0]["block_type"] == "情景剧"          # 板块类型取标签
    assert rows[0]["product"] == "蛋白粉"


def test_archive_material_default_block_type(arch_home):
    """素材库归档无标签时：目录落「未打标」，material_clips 板块类型兜底「成品」。"""
    date_dir = arch_home["gen"] / "0929"
    date_dir.mkdir()
    src = date_dir / "n.mp4"
    src.write_text("v", encoding="utf-8")
    _seed_task("维C", "", str(src), mark=task_store.MARK_OK)
    archiver.run(dest=archiver.DEST_MATERIAL)
    rows = db.query("SELECT * FROM material_clips")
    assert rows and rows[0]["block_type"] == "成品"
    assert (arch_home["material"] / "维C" / archiver.UNTAGGED / "n.mp4").is_file()


def test_archive_untagged_and_missing_product(arch_home):
    date_dir = arch_home["gen"] / "0925"
    date_dir.mkdir()
    f = date_dir / "x.mp4"
    f.write_text("v", encoding="utf-8")
    _seed_task("", "", str(f), mark=task_store.MARK_OK)       # 无产品、无标签，但标了可用
    archiver.run(dest=archiver.DEST_OUTPUT)
    assert (arch_home["output"] / archiver.UNPRODUCT / archiver.UNTAGGED / "x.mp4").is_file()


def test_archive_ignores_orphans_and_in_library(arch_home):
    date_dir = arch_home["gen"] / "0926"
    date_dir.mkdir()
    orphan = date_dir / "同事手丢的.mp4"
    orphan.write_text("v", encoding="utf-8")         # 库里没记：plan 逐任务遍历根本看不到它
    # 已经在成品库目录下的（库里记着且可用）：视为已归档，不再搬
    inlib = arch_home["output"] / "维B" / "情景剧"
    inlib.mkdir(parents=True)
    deep = inlib / "y.mp4"
    deep.write_text("v", encoding="utf-8")
    _seed_task("维B", "情景剧", str(deep), mark=task_store.MARK_OK)
    assert archiver.plan() == []
    assert orphan.is_file() and deep.is_file()
