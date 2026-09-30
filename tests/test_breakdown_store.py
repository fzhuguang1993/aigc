"""
tests/test_breakdown_store.py —— 拆解任务库 link_key 去重（upsert）与整链命中

覆盖阶段1.3：同链接 save 只更新一行不堆重复；find_complete 只在 status='ok'
且图集目录 + Word 文档都在盘上时命中，任一被删即回退重跑。
"""
import uuid

import pytest

from store import db, breakdown_store
from video_text_tools.breakdown.models import BreakdownResult, STAGE_ACQUIRE


@pytest.fixture(autouse=True)
def _ensure_schema():
    db.init()          # 幂等：建表 + 补列 + 建索引
    yield


def _ok_res(link, report=""):
    """造一条「完整」结果：只标解析下载 ok、无失败阶段 → status='ok'。"""
    r = BreakdownResult(link=link, title="标题", video_path="v.mp4",
                        report_path=report, duration=5.0, shot_count=2)
    r.mark(STAGE_ACQUIRE, ok=True)
    return r


def _fresh_link():
    return f"https://v.douyin.com/{uuid.uuid4().hex[:10]}"


def _cleanup(key):
    ids = [r["id"] for r in db.query(
        "SELECT id FROM breakdown_tasks WHERE link_key=?", (key,))]
    for i in ids:
        breakdown_store.delete(i)


class TestLinkKey:
    def test_compute_key_normalizes(self):
        a = breakdown_store.compute_link_key("https://v.douyin.com/abc/?utm_source=x")
        b = breakdown_store.compute_link_key("7.99 复制 https://v.douyin.com/abc/ 看看")
        assert a == b and a

    def test_empty_link_no_key(self):
        assert breakdown_store.compute_link_key("") == ""
        assert breakdown_store.compute_link_key("   ") == ""


class TestUpsert:
    def test_same_link_one_row(self, tmp_path):
        link = _fresh_link()
        key = breakdown_store.compute_link_key(link)
        gal = tmp_path / "g"; gal.mkdir()
        rep = tmp_path / "r.docx"; rep.write_text("x", encoding="utf-8")
        try:
            id1 = breakdown_store.save(_ok_res(link, str(rep)), gallery_dir=str(gal))
            # 同一作品的另一种分享文案（规范化后同 key）：应更新、复用 id
            id2 = breakdown_store.save(_ok_res(link + "/?ch=share", str(rep)),
                                       gallery_dir=str(gal))
            assert id1 == id2
            n = db.query("SELECT COUNT(*) c FROM breakdown_tasks WHERE link_key=?",
                         (key,))[0]["c"]
            assert n == 1
        finally:
            _cleanup(key)

    def test_different_links_different_rows(self, tmp_path):
        l1, l2 = _fresh_link(), _fresh_link()
        k1, k2 = (breakdown_store.compute_link_key(x) for x in (l1, l2))
        gal = tmp_path / "g"; gal.mkdir()
        rep = tmp_path / "r.docx"; rep.write_text("x", encoding="utf-8")
        try:
            i1 = breakdown_store.save(_ok_res(l1, str(rep)), gallery_dir=str(gal))
            i2 = breakdown_store.save(_ok_res(l2, str(rep)), gallery_dir=str(gal))
            assert i1 != i2
        finally:
            _cleanup(k1); _cleanup(k2)


class TestFindComplete:
    def test_hit_and_file_deletion(self, tmp_path):
        link = _fresh_link()
        key = breakdown_store.compute_link_key(link)
        gal = tmp_path / "g"; gal.mkdir()
        rep = tmp_path / "r.docx"; rep.write_text("x", encoding="utf-8")
        try:
            breakdown_store.save(_ok_res(link, str(rep)), gallery_dir=str(gal))
            assert breakdown_store.find_complete(link) is not None
            import shutil
            shutil.rmtree(gal)                          # 图集目录被删 → 不再命中
            assert breakdown_store.find_complete(link) is None
        finally:
            _cleanup(key)

    def test_partial_not_matched(self, tmp_path):
        link = _fresh_link()
        key = breakdown_store.compute_link_key(link)
        gal = tmp_path / "g"; gal.mkdir()
        rep = tmp_path / "r.docx"; rep.write_text("x", encoding="utf-8")
        try:
            r = BreakdownResult(link=link, title="半成品", report_path=str(rep))
            r.mark(STAGE_ACQUIRE, ok=False, reason="没解析出视频")
            breakdown_store.save(r, gallery_dir=str(gal))
            assert breakdown_store.find_complete(link) is None   # partial 不算完整
        finally:
            _cleanup(key)

    def test_unknown_link_none(self):
        assert breakdown_store.find_complete(_fresh_link()) is None
