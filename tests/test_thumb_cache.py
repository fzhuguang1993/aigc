"""
tests/test_thumb_cache.py —— 首帧缩略图缓存键与稳健性（不依赖 ffmpeg 成功）

只验纯逻辑：缓存键随 mtime/绝对路径变化（文件改了不脏读旧图）、同键稳定；
ensure_image 对非视频/缺 ffmpeg 都回空 QImage 而不抛（界面据此保留占位）。
"""
from gui import thumb_cache


def test_png_path_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(thumb_cache, "_DIR", str(tmp_path))
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    p = str(f)
    # 同 path 同 mtime → 同键；mtime 变 → 键变（改名/重录自然失效）
    assert thumb_cache.png_path(p, 100) == thumb_cache.png_path(p, 100)
    assert thumb_cache.png_path(p, 100) != thumb_cache.png_path(p, 200)
    # 不同绝对路径 → 不同键
    g = tmp_path / "b.mp4"
    g.write_bytes(b"x")
    assert thumb_cache.png_path(p, 100) != thumb_cache.png_path(str(g), 100)


def test_cache_dir_created(tmp_path, monkeypatch):
    monkeypatch.setattr(thumb_cache, "_DIR", None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    d = thumb_cache.cache_dir()
    assert d.endswith("thumbs") and (tmp_path / "aigc" / "thumbs").is_dir()


def test_ensure_image_null_on_non_video(tmp_path, monkeypatch):
    """非视频文件抽不出帧：回 isNull 的 QImage，不抛异常。"""
    monkeypatch.setattr(thumb_cache, "_DIR", str(tmp_path))
    f = tmp_path / "not_real.mp4"
    f.write_bytes(b"definitely not a video")
    img = thumb_cache.ensure_image(str(f), 1)
    assert img.isNull()
