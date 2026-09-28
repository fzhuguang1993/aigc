"""
tests/test_speed_ball.py —— 悬浮球的纯逻辑（FolderSpeedometer + 格式化）

只测无 Qt 依赖的部分：目录增长→速率/本次新增，以及速率/字节格式化串。
SpeedBall 控件本身（paintEvent/拖拽）不在此覆盖，靠离屏烟测保证不炸。
"""
import time

from gui.speed_ball import (FolderSpeedometer, fmt_bytes, fmt_rate,
                            fmt_elapsed)


def test_folder_speedometer_tracks_growth(tmp_path):
    m = FolderSpeedometer(tmp_path, window=1000)     # 窗口放大，两条样本都留在窗内
    assert m.tick()["session"] == 0                  # 首次采样定为基线
    time.sleep(0.02)
    (tmp_path / "f.bin").write_bytes(b"x" * 1000)
    s = m.tick()
    assert s["session"] == 1000 and s["total"] == 1000
    assert s["rate"] > 0                             # 目录涨了 → 有正速率
    assert s["avg"] > 0                              # 平均速率作刷盘间隙的固定参考
    assert s["elapsed"] > 0


def test_folder_speedometer_counts_incomplete(tmp_path):
    """下载中途的 .incomplete 临时块也要计入（这正是"看得见在下"的关键）。"""
    m = FolderSpeedometer(tmp_path)
    (tmp_path / "model.x.incomplete").write_bytes(b"x" * 2048)
    assert m.tick()["total"] == 2048


def test_folder_speedometer_missing_dir_is_zero(tmp_path):
    m = FolderSpeedometer(tmp_path / "nope")
    assert m.tick()["total"] == 0


def test_fmt_rate_and_bytes():
    assert fmt_rate(0) == "空闲"
    assert fmt_rate(2 * 1024 * 1024) == "2.0M/s"
    assert fmt_rate(500 * 1024) == "500K/s"
    assert fmt_bytes(0) == "0 B"
    assert fmt_bytes(1024) == "1.0 KB"
    assert fmt_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert fmt_elapsed(65) == "01:05"
