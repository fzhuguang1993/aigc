"""
core/modules.py —— 功能模块开关（安装期选模块 / 设置里增量启用）

背景：exe 是 PyInstaller 全量打包，四个板块的代码永远都在包里。所以"分模块安装"
并不是把代码拆开发，而是——安装时勾了哪些模块写进一份清单，软件读这份清单决定
「侧栏 / 悬浮球扇形 / 托盘菜单 / 工具区」里露不露各板块的入口。页面本体照常构建，
关掉某个模块只是不显示它的入口，绝不会让别处 import 崩（见 page_visible 默认放行）。

三层读取优先级（enabled_modules）：
1. 用户层 app_state["modules_enabled"]（list）——设置里增量启用写这里，是最终真源；
2. 安装层 ProgramData 清单 install.json——首次运行、用户层还没记录时采纳并落到用户层；
3. 都没有（开发态 / 绿色版 / CI）——全部开启，且不写盘（免得把开发机钉死成半套功能）。

本模块只做数据与解析，不 import 任何 gui 类（避免 core←→gui 的环）；生效后的
"重拼导航"由调用方（设置页）拿到主窗口自己触发。页栈索引是整数常量表，与
gui/main_window.py 的装配顺序一一对齐，改归属只动本文件。
"""
import json

from core.paths import COMMON_DATA_DIR

# ---------- 四个可安装模块（顺序即安装向导 / 设置页展示顺序）----------
MODULE_DASHBOARD = "dashboard"
MODULE_AIGC = "aigc"
MODULE_BUILD = "build"
MODULE_TOOLS = "tools"
MODULE_ORDER = [MODULE_DASHBOARD, MODULE_AIGC, MODULE_BUILD, MODULE_TOOLS]
ALL = frozenset(MODULE_ORDER)

MODULES = {
    MODULE_DASHBOARD: {
        "label": "数据中台", "icon": "🖥️",
        "desc": "任务/产出多维统计看板、下钻明细与执行记录"},
    MODULE_AIGC: {
        "label": "AIGC板块 + 素材库", "icon": "🎯",
        "desc": "任务中心 · 产品中心 · 线路负载 · 爆款拆解 · 素材工坊"},
    MODULE_BUILD: {
        "label": "基建板块 + 素材库", "icon": "🏗",
        "desc": "批量基建（客户/执照/账户三级）· 素材工坊"},
    MODULE_TOOLS: {
        "label": "工具中心", "icon": "🧰",
        "desc": "录屏 / 语音识别 / 一键发布 等周边小工具 · 本地文件搜索"},
}

# ---------- 页栈索引 → 模块归属（索引与 gui/main_window.py 固定装配顺序一致）----------
# 单页归一个模块：该模块关掉，这一页的入口就收起
_PAGE_SINGLE = {
    0: MODULE_DASHBOARD,
    1: MODULE_AIGC, 2: MODULE_AIGC, 3: MODULE_AIGC, 9: MODULE_AIGC,
    13: MODULE_BUILD,
    4: MODULE_TOOLS,
}
# 素材工坊三页（素材库 10 / 成品库 11 / AI 混剪 12）：AIGC 或 基建 任一启用即可见
_PAGE_ANY = {
    10: frozenset({MODULE_AIGC, MODULE_BUILD}),
    11: frozenset({MODULE_AIGC, MODULE_BUILD}),
    12: frozenset({MODULE_AIGC, MODULE_BUILD}),
}
# 常驻核心 / 维护页：不受四个模块裁剪（新手入门 5、设置 6、接口管理 7、组织结构 8、UI 组件库 14）
_CORE_PAGES = frozenset({5, 6, 7, 8, 14})


def manifest_path():
    """安装期 Inno 写下的模块清单路径（ProgramData，机器级）。"""
    return COMMON_DATA_DIR / "install.json"


def _app_state():
    """延迟 import：core 是下层，不在模块级反向依赖 store（store 会拉起 config 载入）。"""
    from store import app_state
    return app_state


def _raw_manifest():
    """安装清单原始 dict；读不到 / 坏档返回 None。"""
    try:
        raw = json.loads(manifest_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def _read_manifest():
    """安装清单里选中的模块集合；无文件 / 坏档 / 空集合都返回 None（＝"无安装记录"）。"""
    raw = _raw_manifest()
    if raw is None:
        return None
    mods = raw.get("modules")
    if isinstance(mods, list):
        picked = {m for m in mods if m in ALL}
        return picked or None
    return None


def install_output_dir():
    """安装清单里用户选的成品输出目录；没有返回 None。"""
    raw = _raw_manifest()
    if raw:
        d = str(raw.get("output_dir") or "").strip()
        return d or None
    return None


def seed_output_dir():
    """首启把安装期选的输出目录采纳进 config.json 的 paths.output（仅在用户尚未
    自定义时）；返回是否写入了新值。desktop.py 在 reload config 之前调用一次，
    让 DOWNLOAD_DIR 按安装期选择初始化（API 接口配置不在此处动，仍走首配向导）。"""
    d = install_output_dir()
    if not d:
        return False
    from core.paths import CONFIG_DIR
    cfg = CONFIG_DIR / "config.json"
    if not cfg.exists():
        return False
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    paths = data.get("paths")
    if not isinstance(paths, dict):
        paths = {}
    if str(paths.get("output", "") or "").strip():
        return False                     # 用户已在设置里自定义过：安装值不覆盖
    paths["output"] = d
    data["paths"] = paths
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def enabled_modules():
    """有效启用集：用户层 > 安装层 > 全开。读到安装清单且用户层还没记录时，
    把清单结果 seed 进用户层，此后用户增减都以用户层为准。"""
    st = _app_state()
    cur = st.get("modules_enabled")
    if isinstance(cur, list):
        return {m for m in cur if m in ALL}
    picked = _read_manifest()
    if picked is None:
        return set(ALL)                       # 开发 / 绿色 / CI：全开，且不写盘
    st.set_value("modules_enabled", sorted(picked))
    return set(picked)


def is_enabled(key):
    return key in enabled_modules()


def set_enabled(key, on):
    """翻转某模块的启用状态并落盘（用户层）；返回翻转后的启用集。
    是否触发"重拼导航"由调用方决定，本函数只负责把事实写对。"""
    cur = enabled_modules()
    if on:
        cur.add(key)
    else:
        cur.discard(key)
    _app_state().set_value("modules_enabled", sorted(cur))
    return cur


def page_visible(idx):
    """某一页的入口当前是否该显示。核心/维护页恒 True；未登记的页默认放行
    （宁可多显示，也不把没进表的新页误藏）。"""
    if idx in _CORE_PAGES:
        return True
    if idx in _PAGE_SINGLE:
        return _PAGE_SINGLE[idx] in enabled_modules()
    if idx in _PAGE_ANY:
        return bool(enabled_modules() & _PAGE_ANY[idx])
    return True


def module_of_page(idx):
    """页索引所属的单一模块键（工坊/核心页返回 None）；供调用方按模块聚合。"""
    return _PAGE_SINGLE.get(idx)


def workshop_visible():
    """素材工坊分组行是否出现：AIGC 或 基建 任一启用。"""
    en = enabled_modules()
    return bool(en & _PAGE_ANY[10])
