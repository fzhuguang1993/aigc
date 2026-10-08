"""
tests/test_paths_data_home.py —— 数据家四态与旧库收养（装进 Program Files 的地基）

覆盖：
1. 四种口径各归各位：AIGC_HOME > 开发态 > 绿色版标记 > 安装版（%LOCALAPPDATA%）；
2. 安装版首次运行把旧绿色版落在程序目录的任务库**复制**进新数据家，旧原件不动；
3. 新家已有库时绝不拿旧数据盖用户新账；
4. 非安装版（开发态、测试用 AIGC_HOME 隔离）一律不动程序目录——
   这条守卫就是"跑测试时别把仓库里的 data/aigc.db 拷进临时家"的保险丝。
"""
from core import paths


# ---------------- 四态口径 ----------------

def test_data_home_dev_uses_app_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("AIGC_HOME", raising=False)
    monkeypatch.setattr(paths, "_is_frozen", lambda: False)
    home, mode = paths._data_home(tmp_path)
    assert (home, mode) == (tmp_path, "dev")


def test_data_home_installed_goes_to_localappdata(tmp_path, monkeypatch):
    monkeypatch.delenv("AIGC_HOME", raising=False)
    monkeypatch.setattr(paths, "_is_frozen", lambda: True)
    monkeypatch.setattr(paths, "_local_appdata", lambda: tmp_path / "LocalAppData")
    app_dir = tmp_path / "Program Files" / paths.APP_NAME
    home, mode = paths._data_home(app_dir)
    assert mode == "installed"
    # 关键：安装版的数据家绝不能是 exe 旁边（Program Files 普通用户不可写）
    assert home == tmp_path / "LocalAppData" / paths.APP_NAME
    assert home != app_dir


def test_data_home_portable_marker_keeps_data_next_to_exe(tmp_path, monkeypatch):
    monkeypatch.delenv("AIGC_HOME", raising=False)
    monkeypatch.setattr(paths, "_is_frozen", lambda: True)
    monkeypatch.setattr(paths, "_local_appdata", lambda: tmp_path / "LocalAppData")
    (tmp_path / paths.PORTABLE_MARKER).write_text("", encoding="utf-8")
    home, mode = paths._data_home(tmp_path)
    assert (home, mode) == (tmp_path, "portable")      # U 盘携带：拷走即整个搬家


def test_data_home_env_beats_everything(tmp_path, monkeypatch):
    """AIGC_HOME 优先级最高：回归测试靠它隔离，多实例/自动化也用它"""
    monkeypatch.setenv("AIGC_HOME", str(tmp_path / "env_home"))
    monkeypatch.setattr(paths, "_is_frozen", lambda: True)
    (tmp_path / paths.PORTABLE_MARKER).write_text("", encoding="utf-8")
    home, mode = paths._data_home(tmp_path)
    assert (home, mode) == (tmp_path / "env_home", "env")


# ---------------- 旧库收养 ----------------

def _legacy_app_home(tmp_path):
    """伪造一个旧绿色版：exe 旁边有 data/aigc.db + 非空 outputs + 空 exports"""
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "data" / "aigc.db").write_text("OLD", encoding="utf-8")
    (app / "data" / "extra.db").write_text("EXTRA", encoding="utf-8")
    (app / "outputs" / "sub").mkdir(parents=True)
    (app / "outputs" / "sub" / "v1.mp4").write_text("V", encoding="utf-8")
    (app / "exports").mkdir(parents=True)               # 空目录不该被提示
    return app


def test_adopts_legacy_db_and_reports_media(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_MODE", "installed")
    app = _legacy_app_home(tmp_path)
    home = tmp_path / "new_home"
    home.mkdir()

    info = paths._adopt_legacy_data(home, app)

    assert info["adopted"] is True
    assert (home / "data" / "aigc.db").read_text(encoding="utf-8") == "OLD"
    assert (home / "data" / "extra.db").exists()
    # 旧位置原件一律不动：那份可能还在别的机器/文件夹被绿色版用着
    assert (app / "data" / "aigc.db").exists()
    # 只报非空产物目录，路径交给设置页让用户自己指过去（几十 GB 不自动拷）
    assert info["legacy_media"] == [str(app / "outputs")]
    assert info["legacy_home"] == str(app)


def test_never_overwrites_existing_db_in_new_home(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_MODE", "installed")
    app = _legacy_app_home(tmp_path)
    home = tmp_path / "new_home"
    (home / "data").mkdir(parents=True)
    (home / "data" / "aigc.db").write_text("NEW", encoding="utf-8")

    info = paths._adopt_legacy_data(home, app)

    assert info["adopted"] is False
    assert (home / "data" / "aigc.db").read_text(encoding="utf-8") == "NEW"
    assert not (home / "data" / "extra.db").exists()


def test_no_legacy_db_means_nothing_to_adopt(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_MODE", "installed")
    app = tmp_path / "app"
    app.mkdir()
    home = tmp_path / "new_home"
    home.mkdir()

    info = paths._adopt_legacy_data(home, app)

    assert info == {"adopted": False, "legacy_home": "", "legacy_media": []}


def test_dev_and_env_modes_never_touch_app_dir(tmp_path, monkeypatch):
    """非安装版：APP_DIR 就是仓库根，跟着探等于把开发机的库拷进临时家（用例秒歪）"""
    monkeypatch.setattr(paths, "DATA_MODE", "dev")
    app = _legacy_app_home(tmp_path)
    home = tmp_path / "new_home"
    home.mkdir()

    info = paths._adopt_legacy_data(home, app)

    assert info["adopted"] is False
    assert not (home / "data").exists()


def test_ensure_data_home_creates_layout(tmp_path, monkeypatch):
    """数据家不存在也要建好 data/ 子目录（库文件落它里面）"""
    monkeypatch.setattr(paths, "DATA_MODE", "dev")
    home = tmp_path / "fresh"
    info = paths.ensure_data_home(home, tmp_path / "no_such_app")
    assert (home / "data").is_dir()
    assert info["mode"] == "dev"
    assert info["home"] == str(home)
    assert info["adopted"] is False


# ---------------- 模块级事实（config/store 都从这里派生） ----------------

def test_module_level_values_are_consistent():
    assert paths.DATA_HOME_INFO["mode"] == paths.DATA_MODE
    assert paths.DATA_HOME_INFO["home"] == str(paths.RUNTIME_DIR)
    assert paths.RUNTIME_DIR == paths.Path(paths.DATA_HOME_INFO["home"])
    # 测试家由 conftest 的 AIGC_HOME 指定，绝不能是仓库根
    assert paths.DATA_MODE == "env"
    assert paths.APP_DIR != paths.RUNTIME_DIR or paths.DATA_MODE == "dev"
