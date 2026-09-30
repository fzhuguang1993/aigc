"""
tests/test_trial.py —— 试用版限时锁定 core/trial.py

覆盖：默认不启用＝放行、全新首次运行、超过 N 小时到期、改系统时间回拨锁死、
签名被手改＝该份作废回退。注册表锚点在测试里打桩成 None，只走 json 单锚点。
"""
import json
import time

from core import trial


def _isolate(monkeypatch, tmp_path, hours=72):
    """把 trial 指向临时文件、桩掉注册表，返回临时 trial.json 路径。"""
    f = tmp_path / "trial.json"
    monkeypatch.setattr(trial, "TRIAL_FILE", f)
    monkeypatch.setattr(trial, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(trial, "trial_hours", lambda: hours)
    monkeypatch.setattr(trial, "_read_reg", lambda: None)
    monkeypatch.setattr(trial, "_write_reg", lambda *a: None)
    return f


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(trial, "trial_hours", lambda: 0)
    assert trial.trial_enabled() is False
    ok, msg, remain = trial.check_and_persist()
    assert ok is True and remain == 0 and msg == ""


def test_fresh_run_within_window(monkeypatch, tmp_path):
    f = _isolate(monkeypatch, tmp_path, hours=72)
    assert not f.exists()
    ok, msg, remain = trial.check_and_persist()
    assert ok is True
    assert 71 * 3600 < remain <= 72 * 3600
    assert f.exists()                              # 首次运行已落盘


def test_expired_after_window(monkeypatch, tmp_path):
    f = _isolate(monkeypatch, tmp_path, hours=72)
    first = int(time.time()) - 73 * 3600           # 首启在 73 小时前
    f.write_text(json.dumps(trial._seal(first, first)), encoding="utf-8")
    ok, msg, remain = trial.check_and_persist()
    assert ok is False and remain == 0
    assert "72" in msg and "到期" in msg


def test_clock_rollback_locks(monkeypatch, tmp_path):
    f = _isolate(monkeypatch, tmp_path, hours=72)
    now = int(time.time())
    # max_seen 记在未来 2 小时 → 当前时间比它早太多，判为回拨
    f.write_text(json.dumps(trial._seal(now - 10, now + 7200)), encoding="utf-8")
    ok, msg, remain = trial.check_and_persist()
    assert ok is False and remain == 0
    assert "回拨" in msg


def test_tampered_signature_falls_back(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, hours=72)
    # 手改成"首启在很久以前"但没重算签名 → 视为无效，无第二锚点则当作全新首次，放行
    bad = {"first_run": 1, "max_seen": 1, "sig": "deadbeefdeadbeefdeadbeefdeadbeef"}
    (tmp_path / "trial.json").write_text(json.dumps(bad), encoding="utf-8")
    ok, msg, remain = trial.check_and_persist()
    assert ok is True
    assert remain > 71 * 3600
