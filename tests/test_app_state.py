"""
tests/test_app_state.py —— 界面偏好持久化（时长/步数/KOL 跨重启沿用）

这里的东西丢了不影响业务，所以读写异常一律静默；但「上次用 10 秒、这次重启
变回 5 秒」会被使用者当成「改了没生效」，必须真的存下来。
"""
import json

from store import app_state


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")
    assert app_state.get("exec_params") is None
    app_state.set_value("exec_params", {"duration": 10, "steps": 20, "kol": None})
    assert app_state.get("exec_params") == {"duration": 10, "steps": 20, "kol": None}


def test_other_keys_are_preserved(tmp_path, monkeypatch):
    """整份文件是一起读写的，写一个键不能把别的键冲掉"""
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")
    app_state.set_value("a", 1)
    app_state.set_value("b", 2)
    assert json.loads((tmp_path / "ui_state.json").read_text(encoding="utf-8")) \
        == {"a": 1, "b": 2}


def test_broken_file_degrades_to_empty(tmp_path, monkeypatch):
    """文件被写坏（上次断电留个半截）也不能让软件起不来"""
    f = tmp_path / "ui_state.json"
    f.write_text("{不是 JSON", encoding="utf-8")
    monkeypatch.setattr(app_state, "STATE_FILE", f)
    assert app_state.get("exec_params", "默认") == "默认"
    assert app_state.set_value("exec_params", {"duration": 5}) is True
    assert app_state.get("exec_params") == {"duration": 5}


def test_non_dict_content_is_ignored(tmp_path, monkeypatch):
    f = tmp_path / "ui_state.json"
    f.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(app_state, "STATE_FILE", f)
    assert app_state.get("exec_params", "默认") == "默认"


def test_unwritable_path_returns_false(tmp_path, monkeypatch):
    """写不进去（只读目录等）只返回 False，不抛异常打断界面操作"""
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "不存在的目录" / "x.json")
    (tmp_path / "不存在的目录").mkdir(mode=0o500)      # 只读目录，里面建不了文件
    assert app_state.set_value("exec_params", {"duration": 5}) is False
