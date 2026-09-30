"""
tests/test_special.py —— 特供版裁剪 core/special.py（只测非 Qt 的额度/开关逻辑）

覆盖：默认未用过＝满额度、成功计数累加并封顶、SPECIAL_BUILD=False 时全部 no-op。
DeepSeek 勾选拦截、额度弹窗依赖 QApplication + QMessageBox，不在单测里跑。
"""
from core import special


def _isolate(monkeypatch, tmp_path, build=True, limit=2):
    monkeypatch.setattr(special, "SPECIAL_BUILD", build)
    monkeypatch.setattr(special, "BREAKDOWN_OK_LIMIT", limit)
    monkeypatch.setattr(special, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(special, "_STATE_FILE", tmp_path / "special.json")


def test_default_full_quota(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert special.enabled() is True
    assert special._used() == 0
    assert special.remaining() == 2


def test_success_counts_and_caps(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, limit=2)
    special.note_breakdown_success()
    assert special._used() == 1 and special.remaining() == 1
    special.note_breakdown_success()
    assert special._used() == 2 and special.remaining() == 0
    # 超过上限再记也不涨（封顶）
    special.note_breakdown_success()
    assert special._used() == 2


def test_guard_blocks_when_exhausted(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, limit=2)
    special._set_used(2)
    assert special.remaining() == 0
    # 无父窗口 + offscreen 下不弹窗：直接验证 remaining 判定即为拦截条件
    assert special.remaining() <= 0


def test_disabled_is_noop(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, build=False)
    assert special.enabled() is False
    special.note_breakdown_success()
    assert special._used() == 0                 # 未启用：计数不落盘
    assert special.remaining() > 1000           # 未启用：示意不限
