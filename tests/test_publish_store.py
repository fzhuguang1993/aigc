"""
test_publish_store.py —— 发布历史入库回读

record() 收 PublishRecord 对象或同名字段 dict；account_label 落到 account 列；
recent/stats 口径正确。用例前后清空 publishes 表，不污染其它用例。
"""
import pytest

from store import db, publish_store
from video_text_tools.publish.models import PublishRecord


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM publishes")
    yield
    db.execute("DELETE FROM publishes")


def test_record_object_and_readback():
    rec = PublishRecord(platform="douyin", account_label="主号", title="标题A",
                        video_path="a.mp4", ok=True, post_id="777",
                        post_url="https://www.douyin.com/video/777",
                        published_at="2026-09-26 10:00:00")
    assert publish_store.record(rec) is not None
    rows = publish_store.recent()
    assert len(rows) == 1
    assert rows[0]["account"] == "主号"
    assert rows[0]["ok"] == 1
    assert rows[0]["post_url"].endswith("/777")


def test_record_dict_and_stats():
    publish_store.record_all([
        {"platform": "kuaishou", "account_label": "ks", "title": "B",
         "video_path": "b.mp4", "ok": False, "message": "接口待配置"},
        {"platform": "douyin", "account": "dy", "title": "C",
         "video_path": "c.mp4", "ok": True, "post_url": "u/c"},
    ])
    s = publish_store.stats()
    assert s == {"total": 2, "ok": 1, "fail": 1}
