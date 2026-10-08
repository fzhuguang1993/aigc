"""
gui/main_window.py —— 主窗口：左侧导航 + 页面栈 + 状态栏，2 秒自动刷新
"""
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QShortcut, QKeySequence
from PySide6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
                               QListWidget, QStackedWidget, QLabel, QPushButton,
                               QMessageBox, QApplication)

from utils.desktop_utils import open_path
from gui.window_frame import TitleBar, apply_rounded
from gui.pages_tasks import TasksPage
from gui.pages_settings import SettingsPage
from gui.pages_dashboard import DashboardPage
from gui.pages_console import ConsolePage
from gui.pages_products import ProductsPage
from gui.pages_tools import ToolsPage, load_pinned, set_pinned
from gui.pages_guide import GuidePage
from gui.pages_api import ApiManagerPage
from gui.pages_org import OrgPage
from gui.pages_breakdown import BreakdownTasksPage
from gui.pages_material import MaterialPage
from gui.pages_output_lib import OutputLibPage
from gui.pages_remix import RemixPage
from gui.pages_local_build import LocalBuildPage
from gui.pages_ui_kit import UiKitPage
from gui.menus import StyledMenu
from gui.tools_registry import LAUNCHER_TOKEN, HOME_TOKEN, RESERVED_TOKENS
from gui.window_foreground import raise_to_front
from store import app_state


def resource_path(rel):
    """资源文件路径：兼容开发模式与 PyInstaller 打包（sys._MEIPASS）"""
    base = getattr(sys, "_MEIPASS", None)
    candidates = []
    if base:
        candidates.append(Path(base) / rel)
    candidates.append(Path(__file__).resolve().parent.parent / rel)
    candidates.append(Path.cwd() / rel)
    for p in candidates:
        if p.exists():
            return str(p)
    return ""


# 导航基础项：(文本, 页栈索引)。页栈顺序固定不变；导航行号与索引不再
# 一一对应——「固定在左侧」的工具项插在「⚙️ 设置」之前，由 _nav_rows 映射表桥接
# 「🔥 拆解任务」页常驻页栈末尾（索引 9），显示位在「工具中心」之后、「设置」仍在末位
# 「🎛 UI 组件库」是维护/联调用的视觉画廊（索引 14），先摆在侧栏便于整体核对，后续可改口令呼出
_NAV_PAGES = [("🖥️  数据中台", 0), ("🎯  任务中心", 1), ("🧩  产品中心", 2),
              ("📡  线路负载", 3), ("🧰  工具中心", 4), ("🏗  批量基建", 13),
              ("🔥  拆解任务", 9), ("📖  新手入门", 5), ("🎛  UI 组件库", 14),
              ("⚙️  设置", 6)]

# 素材工坊折叠分组：素材库/成品库/AI 混剪三项收进一个可折叠组行，点击组行
# 展开/收起（状态持久化到 ui_state）；页栈索引 10/11/12 与三个页面本体都不动。
_WORKSHOP_KEY = "nav_workshop_open"
_WORKSHOP_PAGES = [("🎞  素材库", 10), ("📦  成品库", 11), ("🎬  AI 混剪", 12)]
_WORKSHOP_INDEXES = {idx for _t, idx in _WORKSHOP_PAGES}


class MainWindow(QMainWindow):
    def __init__(self, start_minimized=False):
        super().__init__()
        self._quitting = False              # 只有走「退出」才置真，区分 ✕ 与真要退
        self._start_minimized = bool(start_minimized)
        self._seed_demo_once()          # 首运行看板没内容时播一批演示任务（一次性）
        self.setWindowTitle("AIGC 工厂")
        self._setup_geometry()
        icon = resource_path("assets/app.ico")
        if icon:
            self.setWindowIcon(QIcon(icon))

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # 无边框圆角窗口没有系统标题栏：自绘一条（拖动移动 + 最小/最大/关闭）
        self._titlebar = TitleBar(self, "🎬 AIGC 工厂")
        root.insertWidget(0, self._titlebar)
        body = QHBoxLayout()
        root.addLayout(body)

        # ----- 左侧导航 -----
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        slay = QVBoxLayout(sidebar)
        slay.setContentsMargins(6, 18, 6, 12)
        # 单行标题 + 关掉自动换行：旧版写“AIGC\n工厂”还带 17px，侧栏不够宽时
        # “AIGC”几字会被挤得竖排换行（用户反馈很丑）；现在整行不高长、自适应宽度
        logo = QLabel("🎬 AIGC 工厂")
        logo.setObjectName("Logo")
        logo.setWordWrap(False)
        slay.addWidget(logo)
        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        # 行号→(种类, 值)映射由 _rebuild_nav 维护：固定工具是“tool”项，
        # 点击只弹工具窗口、不占页面；右键固定项可取消
        self._nav_rows = []
        self._page_row = 0                    # 当前页所在导航行（点工具项后选回来）
        self._api_revealed = False            # 「接口管理」口令解锁后常驻末尾
        self.nav.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.nav.customContextMenuRequested.connect(self._nav_menu)
        slay.addWidget(self.nav, 1)
        body.addWidget(sidebar)

        # ----- 右侧页面 -----
        self.pages = QStackedWidget()
        self.page_tasks = TasksPage()
        self.page_products = ProductsPage()
        self.page_dashboard = DashboardPage()
        self.page_console = ConsolePage()
        self.page_tools = ToolsPage()
        self.page_guide = GuidePage()
        self.page_settings = SettingsPage()
        # 接口管理：默认不进导航（页面在栈里备着），设置页口令验证通过后
        # reveal_api_page() 才把导航项补出来并直接跳入；固定工具/解锁页都
        # 走 _rebuild_nav 重拼映射表，页栈索引永远不动
        self.page_api = ApiManagerPage()
        # 组织结构：页面常驻页栈（索引固定 8），导航项只有 admin 看得见
        self.page_org = OrgPage()
        # 拆解任务：爆款拆解历史库管理页，常驻页栈末尾（索引 9）
        self.page_breakdown = BreakdownTasksPage()
        # 素材库：板块切割片段索引页（索引 10），随拆解「切割入素材库」产出
        self.page_material = MaterialPage()
        # 成品库（索引 11）/ AI 混剪（索引 12）：产物集中审阅与拼接
        self.page_output_lib = OutputLibPage()
        self.page_remix = RemixPage()
        # 批量基建：三级组织（客户/执照/账户）+ 搭建独立页，常驻页栈末尾（索引 13）
        self.page_local_build = LocalBuildPage()
        # UI 组件库（索引 14）：全站视觉令牌/组件画廊，维护/联调用，不吃业务数据
        self.page_ui_kit = UiKitPage()
        # 页栈顺序固定：数据中台(0)、任务、产品、线路负载、工具、新手、
        # 设置、接口管理(7)、组织结构(8)、拆解任务(9)，与 _NAV_PAGES 的索引对应
        for p in (self.page_dashboard, self.page_tasks, self.page_products,
                  self.page_console, self.page_tools, self.page_guide,
                  self.page_settings, self.page_api, self.page_org,
                  self.page_breakdown, self.page_material, self.page_output_lib,
                  self.page_remix, self.page_local_build, self.page_ui_kit):
            self.pages.addWidget(p)
        body.addWidget(self.pages, 1)

        # 数据中台第 0 项、默认选中（执行记录已并入数据中台弹窗，不再单列）
        self.nav.currentRowChanged.connect(self._on_nav_row)
        self._rebuild_nav()

        # ----- 侧栏底部：只留两个按钮——输出文件夹 + 登录/当前用户 -----
        # 登录按账号定身份：点按钮弹框输成员姓名+密码，登上来是什么角色就是什么角色；
        # 按钮文字即状态——未登录显示「未登录」，已登录显示当前用户（见 _update_identity）。
        # 单机模式（未启用组织）没有登录概念，登录按钮隐藏，只剩输出文件夹。
        b_out = QPushButton("📂 输出文件夹")
        b_out.setObjectName("SideBtn")
        b_out.setToolTip("打开生成视频的存放目录（运行目录下 outputs/）")
        b_out.clicked.connect(self.open_output_dir)
        slay.addWidget(b_out)

        self.btn_switch = QPushButton("未登录")
        self.btn_switch.setObjectName("SideBtn")
        self.btn_switch.clicked.connect(self._switch_user)
        slay.addWidget(self.btn_switch)

        self.statusBar().showMessage("就绪 — 完成的视频会自动保存到运行目录 outputs/ 下")

        # ----- 商用网关模式属性：必须在首次 _refresh() 之前就位 -----
        # （GATEWAY_MODE 决定是否起联网复核定时器 _license_tick）
        self._lic_checking = False
        from core.config import GATEWAY_MODE
        self._gateway_mode = GATEWAY_MODE

        # ----- 定时刷新（当前页）-----
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(2000)
        self._refresh()

        # 每 1 小时醒一次，距上次联网复核超 24h 才真去验
        if GATEWAY_MODE:
            self._lic_timer = QTimer(self)
            self._lic_timer.timeout.connect(self._license_tick)
            self._lic_timer.start(3600 * 1000)

        # 定时刷新前先应用「设置-工具快捷键」里保存的一键呼出
        self.apply_tool_shortcuts()
        self.apply_launcher_shortcut()
        self.apply_home_shortcut()          # 默认不给键，设了才注册
        self.apply_launcher_ball()          # 桌面悬浮球：开关关着就什么都不做
        self._update_identity()

        # 本地文件索引：软件一启动就在后台建（用户点名要的不是“第一次用才扫”）。
        # 首扫冷盘实测 45 秒，压在第一次搜索上就是“敲了没反应”；线程内部
        # 延迟 3 秒才动手，不跟窗口首绘抢 IO，总开关关了就直接不装。
        from workers import file_watcher
        file_watcher.start()

        # 鼠标手势引擎：全局事件过滤器，右键划过阈值→匹配轨迹→按当前上下文派发
        from gui.mouse_gesture import MouseGestureEngine
        self.gesture = MouseGestureEngine(self)
        self.gesture.set_resolver(self._resolve_gesture)

        # 托盘：装好即接管「关闭 = 收进托盘」；托盘不可用时 close_to_tray() 自己返
        # 回 False，✕ 依旧直接退出（不能把用户锁在一个没有出口的后台进程里）
        from gui.tray import TrayController
        self.tray = TrayController(self, resource_path("assets/app.ico"))
        self.tray.install()
        self.tray.show_requested.connect(self.show_from_tray)
        self.tray.quit_requested.connect(self.quit_application)
        self.tray.launcher_requested.connect(self._summon_launcher)
        self.tray.tool_requested.connect(self.summon_tool)
        self.refresh_tray_prefs()      # 按已存偏好定下 ✕ 的归宿（含 quitOnLastWindowClosed）

        # 整窗改无边框圆角：标题栏上面已自备，故 add_titlebar=False
        apply_rounded(self, add_titlebar=False, resizable=True)

        # 默认停在数据中台：启动布局稳定后尝试起一次页面向导（看过则自动跳过）
        # 开机自启（藏在托盘）不起向导：那是个看不见界面的启动，弹向导只会弹在
        # 用户切回来时的脸上；顺便告诉他程序确实在后台跑着
        if self._start_minimized:
            QTimer.singleShot(800, lambda: self.tray.notify(
                "已在托盘运行",
                "双击右下角图标打开主界面；设过全局快捷键的话，"
                "在其它软件里按也能直接弹工具。想改：设置 → 托盘与全局快捷键。"))
        else:
            QTimer.singleShot(700, lambda: self._maybe_page_guide(self.pages.currentIndex()))

    # ---------- 鼠标手势：同一命令在不同上下文映射到不同行为 ----------
    def _active_tool_dialog(self):
        """当前活动窗口往上找最近的 ToolDialog（工具窗口开着时手势认它的面板）"""
        from PySide6.QtWidgets import QApplication
        from gui.pages_tools import ToolDialog
        w = QApplication.activeModalWidget() or QApplication.activeWindow()
        while w is not None:
            if isinstance(w, ToolDialog):
                return w
            w = w.parentWidget()
        return None

    def _resolve_gesture(self, cmd):
        from PySide6.QtWidgets import QApplication, QDialog
        from utils.desktop_utils import open_path
        aw = QApplication.activeModalWidget() or QApplication.activeWindow()
        dlg = self._active_tool_dialog()
        if cmd == "close":
            # 只关当前工具窗口/弹窗，绝不误关主窗口
            if isinstance(aw, QDialog) and aw is not self:
                aw.reject()
            return
        if cmd == "refresh":
            if dlg is not None and hasattr(dlg.panel, "refresh"):
                dlg.panel.refresh()
            else:
                self._refresh()
            return
        if cmd == "new":
            page = self.pages.currentWidget()
            if hasattr(page, "_new_task"):            # 任务中心→新建任务
                page._new_task()
            elif hasattr(page, "_create"):            # 产品中心→新建产品
                from store import product_store as ps
                page._create(ps.TYPE_PRODUCT)
            else:
                self.statusBar().showMessage("当前页面没有「新建」动作", 3000)
            return
        if cmd == "open_output":
            # 工具窗口有自己输出目录就开它自己的（录屏/素材提取各自地址）
            ed = getattr(dlg.panel, "ed_out", None) if dlg is not None else None
            if ed is not None and ed.text().strip():
                d = ed.text().strip()
                Path(d).mkdir(parents=True, exist_ok=True)
                open_path(d)
            else:
                self.open_output_dir()
            return
        if cmd == "open_tools":
            self.pages.setCurrentWidget(self.page_tools)
            self._sync_nav_to_page(self.page_tools)
            return
        if cmd == "open_dashboard":
            self.pages.setCurrentWidget(self.page_dashboard)
            self._sync_nav_to_page(self.page_dashboard)
            return

    def _sync_nav_to_page(self, page):
        """手势切页后把左侧选中行也跟过去（否则高亮还停在旧项）"""
        row = self._row_for_page(self.pages.currentIndex())
        self.nav.blockSignals(True)
        self.nav.setCurrentRow(row)
        self._page_row = row
        self.nav.blockSignals(False)

    def _row_for_page(self, idx):
        """页栈索引 → 导航行号：精确匹配优先；素材工坊折叠时其子页落到分组行"""
        for row, (kind, val) in enumerate(self._nav_rows):
            if kind == "page" and val == idx:
                return row
        if idx in _WORKSHOP_INDEXES:
            for row, (kind, _v) in enumerate(self._nav_rows):
                if kind == "group":
                    return row
        return 0

    # ---------- 导航：基础 7 页 + 素材工坊折叠组 + 组织结构(admin) + 固定工具（插到「设置」前面）+ 接口管理 ----------
    def _rebuild_nav(self):
        """按 app_state 里的固定列表重拼导航；选中页不变（行号可能因插入而顺移）"""
        from gui.pages_tools import TOOLS
        from store import org_store
        icons = {n: ico for n, ico, _d, _f in TOOLS}
        rows = []
        items = []
        tail = len(_NAV_PAGES) - 1                # 「⚙️ 设置」是基础项最后一位
        ws_open = bool(app_state.get(_WORKSHOP_KEY))
        for i, (text, idx) in enumerate(_NAV_PAGES):
            if i == tail:                         # 固定工具默认排在设置前一位
                # 组织结构只给管理员看（未启用组织时 is_admin=True，要能进来建首个成员）
                if org_store.is_admin():
                    rows.append(("page", 8))
                    items.append("🏢  组织结构")
                for name in load_pinned():
                    rows.append(("tool", name))
                    items.append(f"{icons.get(name, '🧰')}  {name}")
            if idx == 5:                          # 「素材工坊」分组插在「新手入门」前一位
                rows.append(("group", "workshop"))
                items.append(f"🗂  素材工坊 {'▾' if ws_open else '▸'}")
                if ws_open:
                    for ctext, cidx in _WORKSHOP_PAGES:
                        rows.append(("page", cidx))
                        items.append(f"　　└ {ctext}")
            rows.append(("page", idx))
            items.append(text)
        if self._api_revealed:
            rows.append(("page", 7))
            items.append("🔌  接口管理")
        self._nav_rows = rows
        want = self.pages.currentIndex()          # 选中跟着页面走，不跟旧行号
        self.nav.blockSignals(True)
        self.nav.clear()
        self.nav.addItems(items)
        for r, (kind, _v) in enumerate(rows):
            if kind == "group":
                self.nav.item(r).setToolTip("点击展开/收起：素材库 · 成品库 · AI 混剪")
        row = self._row_for_page(want)
        self._page_row = row
        self.nav.setCurrentRow(row)
        self.nav.blockSignals(False)
        tray = getattr(self, "tray", None)      # _rebuild_nav 在 __init__ 里跑得比
        if tray is not None and getattr(tray, "_menu", None) is not None:
            # 托盘装配还早，这里得先问过有没有（启动即 AttributeError 最冤）
            tray._rebuild_menu()                 # 托盘里的「固定工具」子菜单跟着走

    def _on_nav_row(self, row):
        if not (0 <= row < len(self._nav_rows)):
            return
        kind, val = self._nav_rows[row]
        if kind == "page":
            self._page_row = row
            self.pages.setCurrentIndex(val)
            QTimer.singleShot(500, lambda: self._maybe_page_guide(val))
            return
        if kind == "group":
            # 素材工坊：点击只展开/收起分组，不切页（高亮行由 _rebuild_nav 归位）
            app_state.set_value(_WORKSHOP_KEY, not bool(app_state.get(_WORKSHOP_KEY)))
            self._rebuild_nav()
            return
        # 固定工具：弹工具窗口、不切页，选中行退回原页面
        self.nav.blockSignals(True)
        self.nav.setCurrentRow(self._page_row)
        self.nav.blockSignals(False)
        self.page_tools.open_tool(val)

    def _nav_menu(self, pos):
        """左侧固定工具项右键→从导航取消；普通页面项不出菜单"""
        it = self.nav.itemAt(pos)
        if it is None:
            return
        row = self.nav.row(it)
        if not (0 <= row < len(self._nav_rows)):
            return
        kind, val = self._nav_rows[row]
        if kind != "tool":
            return
        m = StyledMenu(self)
        m.addAction("📌 从左侧导航取消", lambda n=val: self._unpin_tool(n))
        m.exec(self.nav.mapToGlobal(pos))

    def _unpin_tool(self, name):
        set_pinned(name, False)
        self._rebuild_nav()

    def apply_tool_shortcuts(self):
        """「设置-工具快捷键」保存后即时生效：全窗口任意页面一键呼出工具"""
        from gui import global_hotkey as ghk
        from gui.tools_registry import global_hotkey_enabled
        hk = ghk.manager
        for s in getattr(self, "_tool_scuts", []):
            s.setEnabled(False)
            s.setParent(None)
        self._tool_scuts = []
        seqs = app_state.get("tool_shortcuts") or {}
        use_global = hk.available() and global_hotkey_enabled()
        for name, seq in seqs.items():
            if not seq:
                continue
            hk.forget(name)
            if use_global and hk.register(name, seq):
                hk.set_callback(name, lambda n=name: self.summon_tool(n))
                continue
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(lambda n=name: self.page_tools.open_tool(n))
            self._tool_scuts.append(sc)
        # 配置里已经没有的键位（改键/清键剩下的）也要把系统热键放掉，
        # 否则旧键一直占着系统：别的软件按不到，我们也不再响应它
        want = {n for n, s in seqs.items() if s}
        for token in hk.bound_tokens() - want - RESERVED_TOKENS:
            hk.forget(token)
        self.page_tools.refresh_sc_hints()      # 卡片标题旁的快捷键提示跟着重注册刷新
        self._log_hotkeys("工具快捷键")

    def apply_launcher_shortcut(self):
        """总唤出面板的键：系统级优先，注册不上（被占/非 Windows）退回窗口内"""
        from gui import global_hotkey as ghk
        from gui.tools_registry import launcher_shortcut
        seq = launcher_shortcut()
        hk = ghk.manager
        for s in getattr(self, "_launcher_scuts", []):
            s.setEnabled(False)
            s.setParent(None)
        self._launcher_scuts = []
        hk.forget(LAUNCHER_TOKEN)
        if not seq:
            return
        if hk.available() and hk.register(LAUNCHER_TOKEN, seq):
            hk.set_callback(LAUNCHER_TOKEN, lambda _t=None: self._summon_launcher())
            self._log_hotkeys("总唤出键")
            return
        sc = QShortcut(QKeySequence(seq), self)
        sc.setContext(Qt.ShortcutContext.WindowShortcut)
        sc.activated.connect(self._summon_launcher)
        self._launcher_scuts.append(sc)
        # 走到这里＝系统级没注册上（键被占/非 Windows），已退回窗口内快捷键：
        # 主窗口藏在托盘时按它不会有反应，必须在日志里留话，不然又是一桩悬案
        self._log_hotkeys("总唤出键")

    def apply_launcher_ball(self):
        """按「设置 → 唤出面板 → 桌面悬浮球」开关显示/收起那颗悬浮球。

        关：收起球并关掉扇形菜单。开：重建球（show_launcher_ball 内部先收旧的，
        同一时刻只留一颗），接上单击/双击/右键三个入口。球常驻，扇形懒建。
        """
        from gui import launcher_ball
        if not app_state.get("launcher_ball_on"):
            launcher_ball.hide_launcher_ball()
            if getattr(self, "_fan", None) is not None:
                self._fan.hide_menu()
            return
        self._ball = launcher_ball.show_launcher_ball(
            self._on_ball_single, self._on_ball_double,
            on_search=self._summon_launcher,
            on_config=self._on_ball_config, on_index=self._on_ball_index)

    # ---------- 悬浮球：单击展开扇形 / 双击开主程序 / 右键入口 ----------
    def _on_ball_single(self):
        """点球：以球为极点朝屏内展开扇形功能菜单（再点一下收起）。"""
        from gui.launcher_fan import LauncherFan, load_fan_items
        if getattr(self, "_fan", None) is None:
            self._fan = LauncherFan(self._on_fan_pick, self._on_fan_changed)
        self._fan.toggle(getattr(self, "_ball", None), load_fan_items())

    def _on_fan_pick(self, item):
        """扇形项点落：智能混合——工具弹独立面板、搜索弹唤出面板、页面开主窗口到对应页。"""
        kind = (item or {}).get("kind")
        if kind == "search":
            self._summon_launcher()
        elif kind == "tool":
            self.summon_tool(item.get("ref"))
        elif kind == "page":
            try:
                self._goto_page(int(item.get("ref", 0)))
            except (TypeError, ValueError):
                pass

    def _on_fan_changed(self):
        """扇形就地增删后：若设置页开着，让它把扇形管理列表跟着刷一遍。"""
        try:
            if hasattr(self.page_settings, "reload_fan_items"):
                self.page_settings.reload_fan_items()
        except Exception:
            pass

    def _on_ball_double(self):
        """双击球：打开整个主程序并切到设置的默认页（未配置则回到上次所在页）。"""
        idx = app_state.get("launcher_dblclick_page")
        if idx is None:
            idx = self.pages.currentIndex()
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = self.pages.currentIndex()
        self._goto_page(idx)

    def _on_ball_config(self):
        """右键“功能项设置…”：开主窗口到设置页（那里有扇形项管理 + 双击默认页）。"""
        self._goto_page(6)

    def _on_ball_index(self):
        """右键“索引设置”：与唤出面板「⚙ 索引」、设置页同一对话框（单一真源）。"""
        from gui.dialogs_index_settings import IndexSettingsDialog
        IndexSettingsDialog(self).exec()

    def _goto_page(self, idx):
        """把主窗口拉到眼前并切到页栈 idx（从托盘隐藏态/最小化都能唤回）。"""
        self.show_home()
        if 0 <= idx < self.pages.count():
            self.pages.setCurrentIndex(idx)
            self._sync_nav_to_page(self.pages.currentWidget())

    def apply_home_shortcut(self):
        """呼出主界面的键：与总唤出面板各自一条（面板是选工具，这条是直接回到界面）。

        默认没键（用户要求），所以设了才注册；其余口径与总唤出键一致：
        系统级优先，注册不上就退回窗口内，成败都在日志里留一行。"""
        from gui import global_hotkey as ghk
        from gui.tools_registry import home_shortcut
        seq = home_shortcut()
        hk = ghk.manager
        for s in getattr(self, "_home_scuts", []):
            s.setEnabled(False)
            s.setParent(None)
        self._home_scuts = []
        hk.forget(HOME_TOKEN)
        if not seq:
            return
        if hk.available() and hk.register(HOME_TOKEN, seq):
            hk.set_callback(HOME_TOKEN, lambda _t=None: self.show_home())
            self._log_hotkeys("主界面键")
            return
        sc = QShortcut(QKeySequence(seq), self)
        sc.setContext(Qt.ShortcutContext.WindowShortcut)
        sc.activated.connect(self.show_home)
        self._home_scuts.append(sc)
        self._log_hotkeys("主界面键")

    def show_home(self):
        """把主界面拉回眼前：藏在托盘、最小化、被压在其它窗口下面都要露出来。

        不做“再按一次收起”：唤出面板那种小窗可以随手收，主界面是工作台，
        误按一下就把界面收走的代价远大于多按一次。"""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        raise_to_front(self)                 # 不补这一步会被前台锁拒掉，藏在用户正看着的窗口下面
        self._refresh()

    def _log_hotkeys(self, what):
        """把系统级热键的注册结果写进日志。

        这不是装饰：注册失败时界面只在那个设置页里画一行小字，用户不会去翻，
        于是"按了没反应"变成一件无从查起的事——本轮排查就是靠日志里一条热键
        记录都没有，只能在外面拿探针猜。成败都记一行，下次直接看日志。"""
        from core.logger import log
        from gui import global_hotkey as ghk
        hk = ghk.manager
        bound = sorted(hk.bound_tokens())
        if not hk.failures:
            log.info("%s：系统级已注册 %d 个 %s" % (what, len(bound), bound or "-"))
            return
        bad = "；".join("%s → %s" % (k, v) for k, v in hk.failures.items())
        log.warning("%s：系统级已注册 %d 个 %s；没注册上：%s"
                    % (what, len(bound), bound or "-", bad))

    # ---------- 托盘：收进去 / 叫回来 / 真要退出 ----------
    def refresh_tray_prefs(self):
        """设置页拨了开关后立即照着改行为，不等重启。

        关键是 quitOnLastWindowClosed 要跟着「关闭进托盘」开关走：开着它时主窗口
        只是 hide（任务栏没有入口），若还留着"最后一个窗口关了就退出"，用户关掉
        一个工具小窗就把整个程序带走了——看到的是"软件自己闪退"。"""
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(not self.tray.close_to_tray())
        self.tray.refresh_menu()          # 开机自启的勾选态同步进托盘菜单

    def try_minimize_to_tray(self):
        """标题栏「最小化」按偏好分流；True＝已收进托盘（调用方就不再 showMinimized）"""
        if not self.tray.minimize_to_tray():
            return False
        self.hide_to_tray()
        return True

    def hide_to_tray(self):
        self.hide()
        self.tray.notify_first_hide()          # 只有第一次会真弹（内部记账）

    def show_from_tray(self):
        self.tray.show_window()
        self._refresh()

    def quit_application(self):
        """退出：先释放系统热键与托盘图标，再关窗退出事件循环。

        顺序不能反：进程退了而图标还在，用户点一下就是一个“没响应”的孤儿图标。"""
        if self._quitting:
            return
        self._quitting = True
        from gui import global_hotkey as ghk
        try:
            ghk.manager.unregister_all()
        except Exception:
            pass
        from workers import file_watcher
        try:
            file_watcher.stop()             # 后台扫描线程别留在退出流程里跑完一整盘
        except Exception:
            pass
        try:                                # 缩略图线程不归面板管，归进程管：
            from gui.launcher_preview import shutdown_img_worker   # 这儿主动收一次，
            shutdown_img_worker()           # 免得 Qt 收尾时析构还在跑的 QThread（qFatal）
        except Exception:
            pass
        self.tray.remove()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, e):
        """✕ 的两种归宿：收进托盘（默认，可在设置里改回直接退出）或真退出。

        收进托盘时必须 ignore：真的 close 了就把窗口对象销毁了，托盘上点
        「打开主界面」再也叫不回界面。"""
        if self._quitting or not self.tray.close_to_tray():
            e.accept()
            return
        e.ignore()
        self.hide_to_tray()

    # ---------- 一键唤出：工具窗口 / 搜索面板 ----------
    def summon_tool(self, name):
        """托盘菜单/全局热键共用：藏在托盘时只抬工具窗，不弹主窗口

        （按 Ctrl+Alt+1 的人是要直接干活，不是来看导航的）。"""
        self.page_tools.open_tool(name)
        dlg = self.page_tools.opened_tool(name)
        if dlg is not None:
            dlg.showNormal()
            dlg.raise_()
            dlg.activateWindow()
            raise_to_front(dlg)                 # 同唤出面板：不补这一步会被压在用户正看着的窗口下面

    def _summon_launcher(self):
        from gui.dialogs_launcher import LauncherDialog
        if getattr(self, "_launcher", None) is None:
            self._launcher = LauncherDialog(self.summon_tool, None)
        self._launcher.summon()

    def _setup_geometry(self):
        """初始尺寸＝屏幕可用区的 75%，并居中；窗口仍可自由拉大放小

        （原 50% 时用户要求“整个软件尺寸放大 50%”——是窗口变大、
        字号不变，不是 DPI 缩放；75% ≈ 50%×1.5）"""
        from PySide6.QtWidgets import QApplication
        self.setMinimumSize(860, 560)
        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        if avail is None:                             # 拿不到屏幕信息：退回固定尺寸
            self.resize(1440, 860)
            return
        w = min(max(int(avail.width() * 0.75), 860), avail.width())
        h = min(max(int(avail.height() * 0.75), 560), avail.height())
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2,
                  avail.y() + (avail.height() - h) // 2)

    def _refresh(self):
        page = self.pages.currentWidget()
        if hasattr(page, "refresh"):
            page.refresh()

    # ---------- 首次使用引导（分步高亮）与看板演示数据 ----------
    def _seed_demo_once(self):
        """新用户首装且库里没有任何真实任务时，播一批演示任务丰富看板（一次性）。

        开发/维护机已有真实数据→demo_seed 内部自动跳过；播过也不重复。
        任何异常都吞掉，不能让播种影响启动。"""
        if app_state.get("demo_seeded"):
            return
        try:
            from store import demo_seed
            demo_seed.seed_demo()
            app_state.set_value("demo_seeded", True)
        except Exception:
            pass

    def _maybe_page_guide(self, index):
        """切到指定页且此前未看过时，起一个分步高亮向导（任务中心自带向导，不在此列）。"""
        from gui import onboarding
        if index == 0:
            d = self.page_dashboard
            steps = [
                (lambda: d.cb_range, "时间范围", "切换统计窗口：今日 / 近 7 天 / 近 30 天……下方所有图表随此联动。"),
                (lambda: d.k_total, "关键指标", "顶部 KPI 卡是执行总条数/成功/失败/成功率等概览，一眼看全局产能与健康度。"),
                (lambda: d.tabs, "多页视图", "总览与其他图表分页存放；点「＋ 新建视图」可自建画布，右键添加组件、拖拽布局。"),
                (lambda: d.trend, "图表可下钻", "点任意图表弹出明细窗，看逐日趋势/占比背后每一笔执行记录。"),
            ]
            onboarding.maybe_run(self, "dashboard", steps)
        elif index == 9:
            p = self.page_breakdown
            steps = [
                (lambda: p.ed_search, "搜索拆解任务", "按标题或关联产品名搜索历史拆解记录。"),
                (lambda: p._area, "三屏联动卡片", "每张卡片对应一次爆款拆解：点开即图集 / 播放器 / 文档三屏联动。"),
                (None, "关联产品与右键", "右键卡片可「关联产品」归类、删除；先到「工具中心 → 爆款拆解」跑一条即自动入库。"),
            ]
            onboarding.maybe_run(self, "breakdown_page", steps)

    def _license_tick(self):
        """后台联网复核：24h 一次；过期/封禁/超离线宽限 → 弹激活窗"""
        if self._lic_checking:
            return
        from core import license as lic
        if not lic.needs_recheck():
            return
        self._lic_checking = True

        def run():
            state, info = lic.verify_online()
            # 回主线程处置（QTimer.singleShot 线程安全）
            QTimer.singleShot(0, lambda: self._license_result(state, info))
        threading.Thread(target=run, daemon=True, name="lic-recheck").start()

    def _license_result(self, state, info):
        self._lic_checking = False
        from core import license as lic
        if state == "ok":
            return
        if state == lic.NET_ERROR and lic.offline_ok():
            return                            # 断网但在宽限期内，不打扰
        from PySide6.QtWidgets import QApplication, QDialog
        from gui.dialogs_license import LicenseDialog
        msg = info.get("message") if isinstance(info, dict) else ""
        if LicenseDialog(self, message=msg or "授权已到期，请输入新卡密续费").exec() \
                != QDialog.DialogCode.Accepted:
            QApplication.quit()               # 放弃续费：直接退出程序

    # ---------- 登录身份 / 切换用户 ----------
    def _update_identity(self):
        """左下角登录按钮文字即身份：
        - 已登录→「👤 姓名 · 角色（部门）」；
        - 组织已启用但没登录→「未登录」；
        - 单机模式（组织未启用）没有登录概念，隐藏该按钮。"""
        from store import org_store
        cur = org_store.current()
        if cur:
            role = org_store.ROLES.get(cur["role"], cur["role"])
            dept = f"（{cur['dept']}）" if cur.get("dept") else ""
            self.btn_switch.setText(f"👤 {cur['name']} · {role}{dept}")
        elif org_store.org_enabled():
            self.btn_switch.setText("未登录")
        else:
            self.btn_switch.setVisible(False)
            return
        self.btn_switch.setVisible(True)

    def _switch_user(self):
        """弹登录框（按账号定身份）：成功→重建导航+刷新全部页；取消→维持原会话"""
        from PySide6.QtWidgets import QDialog
        from gui.dialogs_login import LoginDialog
        from store import org_store
        title = "🔑 登录" if org_store.current() is None else "🔄 切换用户"
        dlg = LoginDialog(self, title=title)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._rebuild_nav()
        self._update_identity()
        # 当前停在对方没权限的页（组织结构/接口管理）：退回数据中台
        cur = self.pages.currentIndex()
        if (cur == 8 and not org_store.is_admin()) or \
                (cur == 7 and not self._api_revealed):
            self.pages.setCurrentIndex(0)
        self._refresh()

    def reveal_api_page(self):
        """口令验证通过后由设置页调用：把「🔌 接口管理」补进导航并跳过去

        本次运行期内保持可见（再锁回去没意义：口令都给你了）；
        重启后恢复默认隐藏。"""
        self._api_revealed = True
        self._rebuild_nav()
        self.nav.setCurrentRow(len(self._nav_rows) - 1)

    def open_output_dir(self):
        """打开（并自动创建）输出文件夹"""
        from core.config import DOWNLOAD_DIR
        try:
            d = Path(DOWNLOAD_DIR)
            d.mkdir(parents=True, exist_ok=True)
            open_path(d)
        except Exception as e:
            QMessageBox.critical(self, "打开失败", str(e))
