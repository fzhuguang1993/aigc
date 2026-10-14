"""
tests/test_software_manager.py —— 软件卸载清理功能层（纯逻辑）

winreg 一律用假对象顶掉（注入 sys.modules），单测绝不碰真机注册表：
钉住 list_installed 的过滤/去重/自保护、build_uninstall_cmd 的各分支、
parse_uninstall_string 的引号/裸路径拆解、scan_residue 的名称匹配，
以及 _reg_delete_recursive 的「先删叶子再删父键」删除顺序。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from video_text_tools import software_manager as sm


# ---------------- 假注册表 ----------------
HKLM = 1
HKCU = 2


class _Node:
    def __init__(self):
        self.children = {}      # name -> _Node
        self.values = {}        # name -> data


class _FakeKey:
    def __init__(self, reg, node):
        self._reg = reg
        self.node = node

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def Close(self):
        pass


class FakeWinreg:
    """实现 list/scan/delete 用得到的方法，并记录 DeleteKey 调用顺序。"""
    HKEY_LOCAL_MACHINE = HKLM
    HKEY_CURRENT_USER = HKCU
    HKEY_CLASSES_ROOT = 0
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    KEY_WOW64_64KEY = 0x0100
    KEY_WOW64_32KEY = 0x0200
    REG_SZ = 1

    def __init__(self):
        self._roots = {HKLM: _Node(), HKCU: _Node()}
        self.delete_log = []

    def add(self, root, path, values=None):
        node = self._root(root)
        for part in path.split("\\"):
            node = node.children.setdefault(part, _Node())
        node.values.update(values or {})
        return node

    def _root(self, root):
        return self._roots[root]

    def _node(self, root, path):
        node = self._root(root)
        for part in path.strip("\\").split("\\"):
            node = node.children.get(part)
            if node is None:
                return None
        return node

    def OpenKey(self, root, path, reserved=0, access=0):
        node = self._node(root, path)
        if node is None:
            raise FileNotFoundError(2, "系统找不到指定的键。")
        return _FakeKey(self, node)

    def EnumKey(self, key, index):
        names = list(key.node.children)
        if index >= len(names):
            raise OSError(87, "no more")
        return names[index]

    def EnumValue(self, key, index):
        names = list(key.node.values)
        if index >= len(names):
            raise OSError(87, "no more")
        n = names[index]
        return n, key.node.values[n], self.REG_SZ

    def DeleteValue(self, key, name):
        key.node.values.pop(name, None)

    def DeleteKey(self, root, path):
        parts = path.strip("\\").split("\\")
        leaf = parts[-1]
        parent = self._node(root, "\\".join(parts[:-1])) if len(parts) > 1 else self._root(root)
        if parent is None or leaf not in parent.children:
            raise FileNotFoundError(2, "no key")
        del parent.children[leaf]
        self.delete_log.append(path.lower())


@pytest.fixture
def reg(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


BASE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


# ---------------- list_installed ----------------
def test_list_filters_bad_entries(reg):
    reg.add(HKLM, BASE + r"\Good", {
        "DisplayName": "好软件", "DisplayVersion": "1.0", "Publisher": "某厂",
        "UninstallString": r"C:\P\Good\unins000.exe"})
    reg.add(HKLM, BASE + r"\NoName", {"DisplayVersion": "1.0",
                                      "UninstallString": "x"})          # 无名 -> 剔除
    reg.add(HKLM, BASE + r"\SysComp", {"DisplayName": "系统组件",
                                       "SystemComponent": 1, "UninstallString": "x"})  # 组件
    reg.add(HKLM, BASE + r"\Hotfix", {"DisplayName": "KB12345",
                                      "ParentKeyName": "x", "UninstallString": "x"})   # 补丁
    reg.add(HKLM, BASE + r"\Upd", {"DisplayName": "Update for Windows (KB9)",
                                   "UninstallString": "x"})             # 更新前缀
    reg.add(HKLM, BASE + r"\NoCmd", {"DisplayName": "无卸载命令"})        # 无卸载串
    names = [r["name"] for r in sm.list_installed()]
    assert names == ["好软件"]


def test_list_hkcu_and_dedup(reg):
    reg.add(HKCU, BASE + r"\Mine", {"DisplayName": "用户级软件",
                                    "UninstallString": "u.exe"})
    recs = sm.list_installed()
    assert [r["name"] for r in recs] == ["用户级软件"]
    assert recs[0]["source"] == "HKCU"


def test_list_msi_flags_product_code(reg):
    reg.add(HKLM, BASE + r"\{ABCD-1234}", {
        "DisplayName": "MSI 程序", "WindowsInstaller": 1,
        "UninstallString": "msiexec /x {ABCD-1234} /I"})
    r = sm.list_installed()[0]
    assert r["is_msi"] is True
    assert r["product_code"] == "{ABCD-1234}"


def test_list_marks_self_protected(reg, monkeypatch):
    monkeypatch.setattr(sm, "_app_dir", lambda: r"c:\apps\aigc")
    reg.add(HKLM, BASE + r"\Self", {
        "DisplayName": "本程序", "InstallLocation": r"C:\Apps\AIGC",
        "UninstallString": r"C:\Apps\AIGC\unins000.exe"})
    r = sm.list_installed()[0]
    assert r["protected"] is True


# ---------------- build_uninstall_cmd ----------------
def _rec(**kw):
    base = {"name": "X", "is_msi": False, "product_code": "",
            "uninstall_string": "", "quiet_uninstall_string": "",
            "display_icon": "", "hive": HKLM, "key_path": "",
            "install_location": "", "publisher": "", "protected": False}
    base.update(kw)
    return base


def test_cmd_msi_silent_and_gui():
    r = _rec(is_msi=True, product_code="{GUID}")
    assert sm.build_uninstall_cmd(r, "silent") == {
        "exe": "msiexec.exe", "args": ["/x", "{GUID}", "/qn", "/norestart"],
        "needs_gui": False, "msg": ""}
    g = sm.build_uninstall_cmd(r, "gui")
    assert g["exe"] == "msiexec.exe" and "/qn" not in g["args"]


def test_cmd_exe_uses_quiet_for_silent():
    r = _rec(uninstall_string=r"C:\p\u.exe /S", quiet_uninstall_string=r"C:\p\u.exe /S")
    c = sm.build_uninstall_cmd(r, "silent")
    assert c["needs_gui"] is False and c["exe"] == r"C:\p\u.exe"


def test_cmd_exe_silent_without_quiet_requests_gui():
    r = _rec(uninstall_string=r"C:\p\u.exe")
    c = sm.build_uninstall_cmd(r, "silent")
    assert c["needs_gui"] is True and "静默" in c["msg"]


# ---------------- parse_uninstall_string ----------------
def test_parse_quoted_exe_and_args():
    exe, args = sm.parse_uninstall_string(r'"C:\Program Files\Foo\unins000.exe" /SILENT /LANG')
    assert exe == r"C:\Program Files\Foo\unins000.exe"
    assert args == ["/SILENT", "/LANG"]


def test_parse_msiexec_bare():
    exe, args = sm.parse_uninstall_string("msiexec /x {GUID}")
    assert exe == "msiexec" and args == ["/x", "{GUID}"]


def test_parse_unquoted_path_with_space():
    """回归：裸路径带空格时不能把中间目录（C:\\Program）当 exe 弹出文件夹。"""
    exe, args = sm.parse_uninstall_string(r"C:\Program Files\A B\unins000.exe /S")
    assert exe == r"C:\Program Files\A B\unins000.exe"
    assert args == ["/S"]


def test_parse_msiexec_exe_full():
    exe, args = sm.parse_uninstall_string("MsiExec.exe /X{GUID}")
    assert exe.lower() == "msiexec.exe" and args == ["/X{GUID}"]


def test_parse_empty():
    assert sm.parse_uninstall_string("") == ("", [])


# ---------------- scan_residue 名称匹配 ----------------
def test_match_token_exact_and_prefix():
    toks = {"fooapp", "foosoft"}
    assert sm._match_token("FooApp", toks) is True
    assert sm._match_token("FooApp 2", toks) is True     # 前缀命中
    assert sm._match_token("Other", toks) is False


def test_scan_residue_lists_only_matching_dirs(monkeypatch):
    rec = _rec(name="FooApp", publisher="FooSoft", install_location="")
    monkeypatch.setattr(sm, "_env_dirs", lambda: ["C:\\PF"])
    monkeypatch.setattr(sm.os, "listdir", lambda b: ["FooApp", "Other", "FooSoft"])
    monkeypatch.setattr(sm.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(sm, "_dir_size_kb", lambda p: 100)
    monkeypatch.setattr(sm, "_reg_key_exists", lambda r, p: False)
    paths = [it["path"] for it in sm.scan_residue(rec)]
    assert "C:\\PF\\FooApp" in paths and "C:\\PF\\FooSoft" in paths
    assert all("Other" not in p for p in paths)


# ---------------- _reg_delete_recursive 删除顺序 ----------------
def test_reg_delete_removes_leaves_first(reg):
    reg.add(HKLM, r"SOFTWARE\Foo\Bar\Baz", {"v": 1})
    reg.add(HKLM, r"SOFTWARE\Foo\Bar", {"w": 2})
    reg.add(HKLM, r"SOFTWARE\Foo", {"x": 3})
    ok, err = sm._reg_delete_recursive(HKLM, r"SOFTWARE\Foo")
    assert ok is True and err == ""
    # 叶子 Baz 先于 Bar 先于 Foo
    assert reg.delete_log == [r"software\foo\bar\baz",
                              r"software\foo\bar", r"software\foo"]


def test_reg_delete_missing_is_ok(reg):
    ok, err = sm._reg_delete_recursive(HKLM, r"SOFTWARE\NoSuch")
    assert ok is True


# ---------------- remove_residue 注册表分派 ----------------
def test_remove_residue_reg_dispatch(reg, monkeypatch):
    reg.add(HKLM, r"SOFTWARE\Foo\Stale", {"v": 1})
    items = [{"kind": "reg", "path": "HKEY_LOCAL_MACHINE\\SOFTWARE\\Foo\\Stale",
              "root": HKLM, "rel": r"SOFTWARE\Foo\Stale", "size": 0, "exists": True}]
    logs = []
    res = sm.remove_residue(items, log=logs.append)
    assert res[0]["ok"] is True
    assert r"software\foo\stale" in reg.delete_log


def test_remove_residue_skips_missing_file(monkeypatch):
    monkeypatch.setattr(sm.os.path, "exists", lambda p: False)
    items = [{"kind": "dir", "path": "C:\\gone", "size": 0, "exists": False}]
    res = sm.remove_residue(items)
    assert res[0]["ok"] is True


# ---------------- _launchable / icon_source_path ----------------
def test_launchable_rejects_missing_path_file(monkeypatch):
    monkeypatch.setattr(sm.os.path, "isfile", lambda p: False)
    assert sm._launchable(r"C:\Program Files\Foo") is False      # 带分隔符且不存在 -> 否


def test_launchable_accepts_existing_file(monkeypatch):
    monkeypatch.setattr(sm.os.path, "isfile", lambda p: p == r"C:\p\u.exe")
    assert sm._launchable(r"C:\p\u.exe") is True


def test_launchable_bare_command():
    assert sm._launchable("msiexec.exe") is True                 # 裸命令名不拦


def test_icon_source_from_display_icon(monkeypatch):
    monkeypatch.setattr(sm.os.path, "isfile", lambda p: p == r"C:\a\a.exe")
    r = _rec(display_icon=r'"-C:\a\a.exe,0"')
    assert sm.icon_source_path(r) == r"C:\a\a.exe"


def test_icon_source_falls_back_to_uninstaller(monkeypatch):
    monkeypatch.setattr(sm.os.path, "isfile",
                        lambda p: p == r"C:\p\unins000.exe")
    r = _rec(display_icon="", uninstall_string=r'"C:\p\unins000.exe" /S')
    assert sm.icon_source_path(r) == r"C:\p\unins000.exe"


# ---------------- classify（分组） ----------------
def test_classify_driver_by_name():
    assert sm.classify({"name": "Realtek Audio Driver", "publisher": "Realtek"}) == "driver"
    assert sm.classify({"name": "NVIDIA Graphics Driver", "publisher": "NVIDIA"}) == "driver"


def test_classify_system_component():
    assert sm.classify({"name": "Microsoft Visual C++ 2019 Redistributable",
                        "publisher": "Microsoft Corporation"}) == "system"
    assert sm.classify({"name": "Windows 应用存储安装器", "publisher": "Microsoft"}) == "system"


def test_classify_app_keeps_microsoft_third_party():
    # 发布者是 Microsoft 但名字不命中关键词的第三方软件，不能被误归系统组
    assert sm.classify({"name": "Visual Studio Code", "publisher": "Microsoft Corporation"}) == "app"
    assert sm.classify({"name": "Google Chrome", "publisher": "Google LLC"}) == "app"


def test_classify_empty_defaults_app():
    assert sm.classify({"name": "", "publisher": ""}) == "app"


def test_list_installed_has_category(reg):
    reg.add(HKLM, BASE + r"\Drv", {"DisplayName": "样例显卡驱动",
                                    "UninstallString": r"C:\p\u.exe"})
    reg.add(HKLM, BASE + r"\App", {"DisplayName": "某个应用",
                                    "UninstallString": r"C:\p\a.exe"})
    by = {r["name"]: r["category"] for r in sm.list_installed()}
    assert by["样例显卡驱动"] == "driver"
    assert by["某个应用"] == "app"


def test_icon_source_empty_when_nothing_exists(monkeypatch):
    monkeypatch.setattr(sm.os.path, "isfile", lambda p: False)
    r = _rec(display_icon=r"C:\nope.ico", uninstall_string=r"C:\nope\u.exe")
    assert sm.icon_source_path(r) == ""


# ---------------- 静默参数推断（无 QuietUninstallString 时） ----------------
def test_cmd_infer_inno_silent():
    # Inno Setup：unins000.exe 文件名 -> 自动补 /VERYSILENT
    r = _rec(uninstall_string=r'"C:\p\unins000.exe"')
    c = sm.build_uninstall_cmd(r, "silent")
    assert c["needs_gui"] is False
    assert c["exe"] == r"C:\p\unins000.exe" and "/VERYSILENT" in c["args"]


def test_cmd_infer_nsis_silent():
    r = _rec(uninstall_string=r"C:\p\uninstall.exe")
    c = sm.build_uninstall_cmd(r, "silent")
    assert c["needs_gui"] is False and c["args"] == ["/S"]


def test_cmd_unknown_exe_still_needs_gui():
    r = _rec(uninstall_string=r"C:\p\setup.exe /u")
    c = sm.build_uninstall_cmd(r, "silent")
    assert c["needs_gui"] is True


def test_silent_capable_branches():
    assert sm.silent_capable(_rec(is_msi=True, product_code="{G}")) is True
    assert sm.silent_capable(_rec(uninstall_string=r"C:\p\unins000.exe")) is True
    assert sm.silent_capable(_rec(quiet_uninstall_string=r"C:\p\x.exe /S")) is True
    assert sm.silent_capable(_rec(uninstall_string=r"C:\p\setup.exe")) is False


# ---------------- 残留分级 confidence ----------------
def test_scan_residue_confidence_tags(monkeypatch):
    rec = _rec(name="FooApp", publisher="FooSoft", install_location=r"C:\PF\FooApp")
    monkeypatch.setattr(sm, "_env_dirs", lambda: ["C:\\PF"])
    monkeypatch.setattr(sm.os, "listdir", lambda b: ["FooApp", "FooSoft"])
    monkeypatch.setattr(sm.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(sm, "_dir_size_kb", lambda p: 100)
    monkeypatch.setattr(sm, "_reg_key_exists", lambda r, p: False)
    by = {it["path"]: it["confidence"] for it in sm.scan_residue(rec)}
    assert by[r"C:\PF\FooApp"] == "high"      # 软件自身安装目录 = 高置信
    assert by[r"C:\PF\FooSoft"] == "low"       # 公共目录下同名 = 低置信


def test_split_residue():
    items = [{"confidence": "high", "path": "a"}, {"confidence": "low", "path": "b"},
             {"path": "c"}]   # 缺字段按低置信
    high, low = sm.split_residue(items)
    assert [h["path"] for h in high] == ["a"]
    assert [lo["path"] for lo in low] == ["b", "c"]


# ---------------- 批量编排 auto_uninstall_and_clean ----------------
def _patch_uninstall_ok(monkeypatch):
    monkeypatch.setattr(sm, "run_uninstall",
                        lambda rec, mode="gui", log=None: {"ok": True, "code": 0, "msg": "已卸载"})
    monkeypatch.setattr(sm, "scan_residue",
                        lambda rec: [{"kind": "dir", "path": "hi", "confidence": "high"},
                                     {"kind": "dir", "path": "lo", "confidence": "low"}])
    monkeypatch.setattr(sm, "remove_residue",
                        lambda items, log=None: [{"path": it["path"], "ok": True} for it in items])


def test_auto_orchestration_statuses(monkeypatch):
    _patch_uninstall_ok(monkeypatch)
    recs = [
        _rec(name="Protected", protected=True),
        _rec(name="NoSilent", uninstall_string=r"C:\p\setup.exe"),
        _rec(name="Inno", uninstall_string=r"C:\p\unins000.exe"),
    ]
    reports = sm.auto_uninstall_and_clean(recs)
    st = {r["name"]: r["status"] for r in reports}
    assert st["Protected"] == "skipped_protected"
    assert st["NoSilent"] == "skipped_nosilent"
    assert st["Inno"] == "uninstalled"
    inno = next(r for r in reports if r["name"] == "Inno")
    assert inno["cleaned"] == ["hi"]          # 只删高置信
    assert inno["kept_low"] == ["lo"]         # 低置信保留列出


def test_auto_orchestration_failure(monkeypatch):
    monkeypatch.setattr(sm, "run_uninstall",
                        lambda rec, mode="gui", log=None: {"ok": False, "code": None, "msg": "UAC 取消"})
    recs = [_rec(name="Inno", uninstall_string=r"C:\p\unins000.exe")]
    reports = sm.auto_uninstall_and_clean(recs)
    assert reports[0]["status"] == "failed" and "UAC" in reports[0]["msg"]


def test_auto_orchestration_should_stop(monkeypatch):
    _patch_uninstall_ok(monkeypatch)
    recs = [_rec(name=f"S{i}", uninstall_string=r"C:\p\unins000.exe") for i in range(5)]
    reports = sm.auto_uninstall_and_clean(recs, should_stop=lambda: True)
    assert reports == []      # 第一条前就中断
