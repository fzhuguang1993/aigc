"""
tests/test_desktop_reveal.py —— 「定位文件位置」命令行回归

踩过的坑：把 /select,"路径" 当成一个参数用列表交给 subprocess 时，Python 会把里面
那对引号再转义成 \"，explorer 自己那套解析不认这层反斜杠，整串视为无效路径 ——
表现就是文件明明在，点定位却开到【此电脑】。所以 Windows 分支必须递一条完整字符串。
"""
import os
import sys
from pathlib import Path

import pytest

from utils import desktop_utils as du


@pytest.fixture
def spy(monkeypatch):
    """拦住真正会弹窗口的两个动作，只记录参数"""
    calls = {"popen": [], "startfile": []}
    monkeypatch.setattr(du.subprocess, "Popen",
                        lambda args, *a, **kw: calls["popen"].append(args))
    monkeypatch.setattr(du.os, "startfile",
                        lambda p, *a, **kw: calls["startfile"].append(p), raising=False)
    return calls


def test_reveal_windows_passes_plain_command_line(spy, tmp_path):
    if not sys.platform.startswith("win"):
        pytest.skip("只在 Windows 上验 explorer 传参")
    f = tmp_path / "素材 成品 1.mp4"
    f.write_bytes(b"")

    du.reveal_in_folder(str(f))

    assert len(spy["popen"]) == 1
    args = spy["popen"][0]
    # 关键：必须是整条字符串。一旦退回 ["explorer", '/select,"..."']，
    # list2cmdline 会把它拼成 /select,\"...\" ，explorer 就只开【此电脑】
    assert isinstance(args, str), f"命令行被拆成参数列表了：{args!r}"
    assert '\\"' not in args and '"' in args
    assert args == f'explorer /select,"{os.path.normpath(str(f))}"'


def test_reveal_windows_falls_back_to_parent_when_missing(spy, tmp_path):
    if not sys.platform.startswith("win"):
        pytest.skip("只在 Windows 上验 explorer 传参")
    gone = tmp_path / "子目录" / "已经不在了.mp4"
    (tmp_path / "子目录").mkdir()

    du.reveal_in_folder(str(gone))

    assert spy["popen"] == []                      # 不再用无效路径去问 explorer
    assert spy["startfile"] == [str(tmp_path / "子目录")]   # 退一步：只打开父目录


def test_reveal_windows_missing_parent_does_nothing(spy, tmp_path):
    if not sys.platform.startswith("win"):
        pytest.skip("只在 Windows 上验 explorer 传参")
    du.reveal_in_folder(str(tmp_path / "没有这层目录" / "x.mp4"))
    assert spy["popen"] == [] and spy["startfile"] == []


def test_reveal_other_platforms(monkeypatch, tmp_path):
    popped = []
    monkeypatch.setattr(du.subprocess, "Popen", lambda *a, **kw: popped.append(a))
    f = tmp_path / "a.mp4"
    f.write_bytes(b"")

    monkeypatch.setattr(du.sys, "platform", "darwin")
    du.reveal_in_folder(str(f))
    assert popped[-1][0] == ["open", "-R", os.path.normpath(str(f))]

    monkeypatch.setattr(du.sys, "platform", "linux")
    du.reveal_in_folder(str(f))
    assert popped[-1][0] == ["xdg-open", str(Path(os.path.normpath(str(f))).parent)]
