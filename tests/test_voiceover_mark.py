"""
tests/test_voiceover_mark.py —— 「口播标记 + ASR 门控」与「视频类别」改造回归

约定（用户敲定）：
- 只有被「标记为口播素材」的视频才生成口播逐字稿（ASR 有时把环境音误识别成口播）；
  素材库与成品库同口径——pending 只取「已标记且尚未转写」的行；
- 成品库左栏的标签维度改用「视频类别」（品牌类/促销类/情景剧…），按路径手动存，
  词库在设置里维护、可自定义；
- 视频类别词库镜像「素材板块类别」core.block_categories。

用假 worker 替换真实 Whisper（避免回归依赖模型），用 monkeypatch 把 config 段隔到临时文件。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

import types  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from store import db, material_store, app_state  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_real_asr():
    """默认关掉成品库自动识别，避免构造页面时真起线程跑 Whisper。
    需要测门控的用例里再显式打开。"""
    old = app_state.get("output_asr_autorun", True)
    app_state.set_value("output_asr_autorun", False)
    yield
    app_state.set_value("output_asr_autorun", old)


class _FakeWorker:
    """替身 worker：记下入队的 paths/clips，不碰任何 Qt/Whisper。"""

    def __init__(self, store_capture, key):
        self._cap = store_capture
        self._key = key
        self.done_one = types.SimpleNamespace(connect=lambda cb: None)

    def isRunning(self):
        return False

    def start(self):
        pass


# ---------------- 素材库：只队已标记的 ----------------
def test_material_only_marked_get_asr(qapp, tmp_path, monkeypatch):
    from gui.pages_material import MaterialPage
    from gui import pages_material
    db.execute("DELETE FROM material_clips")
    ids = []
    for nm in ("m1.mp4", "m2.mp4", "m3.mp4"):
        f = tmp_path / nm
        f.write_bytes(b"x")
        ids.append(material_store.add(str(f), block_type="钩子", product="骨胶原"))
    material_store.set_voiceover([ids[0]], True)        # 仅第 1 条标记口播

    captured = {}
    monkeypatch.setattr("video_text_tools.asr.transcribe.best_ready_size",
                        lambda: "base")

    def _fake(size, clips, parent=None):
        captured["clips"] = list(clips)
        return _FakeWorker(captured, "material")
    monkeypatch.setattr(pages_material, "MaterialAsrWorker", _fake)

    page = MaterialPage()
    try:
        captured.clear()
        page._maybe_asr()
        assert len(captured.get("clips", [])) == 1, captured
        assert captured["clips"][0][0] == ids[0], captured["clips"]
    finally:
        page.close()
        db.execute("DELETE FROM material_clips")


def test_material_set_voiceover_store(tmp_path):
    """set_voiceover 只动标记；set_clip_transcript/clear_clip_transcript 管文字与转写态。"""
    db.execute("DELETE FROM material_clips")
    f = tmp_path / "c.mp4"
    f.write_bytes(b"x")
    cid = material_store.add(str(f))
    material_store.set_voiceover([cid], True)
    assert int(material_store.get(cid)["is_voiceover"]) == 1
    material_store.set_clip_transcript(cid, "你好世界", True, "base")
    row = material_store.get(cid)
    assert row["transcript"] == "你好世界" and int(row["has_speech"]) == 1 \
        and int(row["transcribed"]) == 1
    material_store.clear_clip_transcript([cid])         # 清文字保留标记
    row = material_store.get(cid)
    assert row["transcript"] == "" and int(row["transcribed"]) == 0 \
        and int(row["is_voiceover"]) == 1
    db.execute("DELETE FROM material_clips")


# ---------------- 成品库：_maybe_asr 门控在已标记 ----------------
def test_output_maybe_asr_gates_on_mark(qapp, tmp_path, monkeypatch):
    from store import output_store
    from gui import pages_output_lib

    def mk(nm, marked, transcribed):
        f = tmp_path / nm
        f.write_bytes(b"x")
        return {"path": str(f), "name": nm, "product": "", "mark": "", "bound": False,
                "tag": "", "display": "", "size": 1, "created_at": "2026-09-02 10:00",
                "_ts": 0, "transcribed": transcribed, "has_speech": 0, "transcript": "",
                "voiceover_marked": marked, "category": ""}

    rows = [mk("a.mp4", True, False), mk("b.mp4", False, False), mk("c.mp4", True, True)]
    monkeypatch.setattr(output_store, "list_outputs", lambda **kw: list(rows))
    monkeypatch.setattr("video_text_tools.asr.transcribe.best_ready_size", lambda: "base")

    captured = {}

    def _fake(size, paths, parent=None):
        captured["paths"] = list(paths)
        return _FakeWorker(captured, "output")
    monkeypatch.setattr(pages_output_lib, "OutputAsrWorker", _fake)

    app_state.set_value("output_asr_autorun", True)     # 门控测要跑 _maybe_asr
    page = pages_output_lib.OutputLibPage()
    try:
        captured.clear()
        page._maybe_asr()
        # 仅 a（已标记且未转写）进队；b 未标记、c 已转写都不进
        assert captured.get("paths") == [rows[0]["path"]], captured
    finally:
        page.close()


# ---------------- 视频类别词库 ----------------
def test_video_categories_load_default(monkeypatch, tmp_path):
    from core import video_categories as vc
    monkeypatch.setattr(vc, "CONFIG_JSON", tmp_path / "cfg.json")
    monkeypatch.setattr(vc, "_CACHE", None)
    got = vc.load()
    assert got == vc.DEFAULT_CATEGORIES
    assert len(got) == 6 and "品牌类" in got and "大字报" in got


def test_video_categories_custom(monkeypatch, tmp_path):
    from core import video_categories as vc
    monkeypatch.setattr(vc, "CONFIG_JSON", tmp_path / "cfg.json")
    monkeypatch.setattr(vc, "_CACHE", None)
    vc.add("测评类")
    assert "测评类" in vc.load()
    monkeypatch.setattr(vc, "_CACHE", None)             # 重读（从临时 config）仍在
    assert "测评类" in vc.load()


# ---------------- 成品库筛选走 category 而非 tag ----------------
def test_output_category_filter(qapp, tmp_path, monkeypatch):
    from store import output_store
    from gui.pages_output_lib import OutputLibPage

    def mk(nm, cat, tag):
        f = tmp_path / nm
        f.write_bytes(b"x")
        return {"path": str(f), "name": nm, "product": "", "mark": "", "bound": False,
                "tag": tag, "display": "", "size": 1, "created_at": "2026-09-02 10:00",
                "_ts": 0, "transcribed": False, "has_speech": 0, "transcript": "",
                "voiceover_marked": False, "category": cat}

    rows = [mk("a.mp4", "品牌类", "开场钩子"), mk("b.mp4", "促销类", "活动促销")]
    monkeypatch.setattr(output_store, "list_outputs", lambda **kw: list(rows))
    page = OutputLibPage()
    try:
        page._tag = "品牌类"
        out = page._filtered(rows)
        assert len(out) == 1 and out[0]["category"] == "品牌类", out
        # 未分类（category 空）不匹配已选类别
        page._tag = "促销类"
        assert [r["name"] for r in page._filtered(rows)] == ["b.mp4"]
    finally:
        page.close()
