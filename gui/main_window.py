"""
gui/main_window.py —— 主窗口：左侧导航 + 页面栈 + 状态栏，2 秒自动刷新
"""
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QCursor, QShortcut, QKeySequence
from PySide6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
                               QListWidget, QStackedWidget, QLabel, QPushButton,
                               QMessageBox, QMenu)

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
_NAV_PAGES = [("📊  数据中台", 0), ("📋  任务中心", 1), ("🧩  产品中心", 2),
              ("📡  线路负载", 3), ("🧰  工具中心", 4), ("🔥  拆解任务", 9),
              ("📖  新手入门", 5), ("⚙️  设置", 6)]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
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
        # 顶栏身份区：登录后显示「姓名 · 角色（部门）」+ 切换用户；
        # 挤在标题文字（stretch=1）与窗控按钮之间，单机模式自动隐藏
        self.lbl_ident = QLabel()
        self.lbl_ident.setObjectName("AppTitleText")
        self.btn_switch = QPushButton("🔄 切换用户")
        self.btn_switch.setObjectName("AppTitleBtn")
        self.btn_switch.clicked.connect(self._switch_user)
        tl = self._titlebar.layout()
        tl.insertWidget(1, self.lbl_ident)
        tl.insertWidget(2, self.btn_switch)
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
        self.lbl_health = QLabel()
        self.lbl_health.setObjectName("SideStatus")
        self.lbl_health.setWordWrap(True)
        # 灯不是只读装饰：点一下立刻逐条真实探活（后台线程，不卡界面）
        self.lbl_health.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.lbl_health.setToolTip(
            "点击重新检测全部线路（后台线程，几秒后刷新）\n"
            "🟢 探活接口已应答　🟡 服务在线但探活路径未实现（照样能提交）\n"
            "🔴 连不上/5xx（选线跳过它）　⚪ 还没测过")
        self.lbl_health.mousePressEvent = lambda e: self._recheck_health()
        self._checking = False
        slay.addWidget(self.lbl_health)
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
        # 页栈顺序固定：数据中台(0)、任务、产品、线路负载、工具、新手、
        # 设置、接口管理(7)、组织结构(8)、拆解任务(9)，与 _NAV_PAGES 的索引对应
        for p in (self.page_dashboard, self.page_tasks, self.page_products,
                  self.page_console, self.page_tools, self.page_guide,
                  self.page_settings, self.page_api, self.page_org,
                  self.page_breakdown):
            self.pages.addWidget(p)
        body.addWidget(self.pages, 1)

        # 数据中台第 0 项、默认选中（执行记录已并入数据中台弹窗，不再单列）
        self.nav.currentRowChanged.connect(self._on_nav_row)
        self._rebuild_nav()

        # ----- 侧栏底部：输出文件夹 + 命令行模式入口 -----
        b_out = QPushButton("📂 输出文件夹")
        b_out.setObjectName("SideBtn")
        b_out.setToolTip("打开生成视频的存放目录（运行目录下 outputs/）")
        b_out.clicked.connect(self.open_output_dir)
        slay.addWidget(b_out)

        b_console = QPushButton("🖥 打开命令行窗口")
        b_console.setObjectName("SideBtn")
        b_console.setToolTip("在新窗口打开黑窗口模式（与界面共用同一数据库）")
        b_console.clicked.connect(self.open_console_mode)
        slay.addWidget(b_console)

        self.statusBar().showMessage("就绪 — 完成的视频会自动保存到运行目录 outputs/ 下")

        # ----- 商用网关模式属性：必须在首次 _refresh() 之前就位 -----
        # （_update_side_status 会读 _gateway_mode 画侧栏授权行）
        self._lic_checking = False
        from core.config import GATEWAY_MODE
        self._gateway_mode = GATEWAY_MODE

        # ----- 定时刷新（当前页 + 侧栏健康状态）-----
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
        self._update_identity()

        # 鼠标手势引擎：全局事件过滤器，右键划过阈值→匹配轨迹→按当前上下文派发
        from gui.mouse_gesture import MouseGestureEngine
        self.gesture = MouseGestureEngine(self)
        self.gesture.set_resolver(self._resolve_gesture)

        # 整窗改无边框圆角：标题栏上面已自备，故 add_titlebar=False
        apply_rounded(self, add_titlebar=False, resizable=True)

        # 默认停在数据中台：启动布局稳定后尝试起一次页面向导（看过则自动跳过）
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
        idx = self.pages.currentIndex()
        for row, (kind, val) in enumerate(self._nav_rows):
            if kind == "page" and val == idx:
                self.nav.blockSignals(True)
                self.nav.setCurrentRow(row)
                self._page_row = row
                self.nav.blockSignals(False)
                return

    # ---------- 导航：基础 7 页 + 组织结构(admin) + 固定工具（插到「设置」前面）+ 接口管理 ----------
    def _rebuild_nav(self):
        """按 app_state 里的固定列表重拼导航；选中页不变（行号可能因插入而顺移）"""
        from gui.pages_tools import TOOLS
        from store import org_store
        icons = {n: ico for n, ico, _d, _f in TOOLS}
        rows = []
        items = []
        tail = len(_NAV_PAGES) - 1                # 「⚙️ 设置」是基础项最后一位
        for i, (text, idx) in enumerate(_NAV_PAGES):
            if i == tail:                         # 固定工具默认排在设置前一位
                # 组织结构只给管理员看（未启用组织时 is_admin=True，要能进来建首个成员）
                if org_store.is_admin():
                    rows.append(("page", 8))
                    items.append("🏢  组织结构")
                for name in load_pinned():
                    rows.append(("tool", name))
                    items.append(f"{icons.get(name, '🧰')}  {name}")
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
        try:
            row = next(r for r, (k, v) in enumerate(rows) if k == "page" and v == want)
        except StopIteration:
            row = 0
        self._page_row = row
        self.nav.setCurrentRow(row)
        self.nav.blockSignals(False)

    def _on_nav_row(self, row):
        if not (0 <= row < len(self._nav_rows)):
            return
        kind, val = self._nav_rows[row]
        if kind == "page":
            self._page_row = row
            self.pages.setCurrentIndex(val)
            QTimer.singleShot(500, lambda: self._maybe_page_guide(val))
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
        m = QMenu(self)
        m.addAction("📌 从左侧导航取消", lambda n=val: self._unpin_tool(n))
        m.exec(self.nav.mapToGlobal(pos))

    def _unpin_tool(self, name):
        set_pinned(name, False)
        self._rebuild_nav()

    def apply_tool_shortcuts(self):
        """「设置-工具快捷键」保存后即时生效：全窗口任意页面一键呼出工具"""
        for s in getattr(self, "_tool_scuts", []):
            s.setEnabled(False)
            s.setParent(None)
        self._tool_scuts = []
        seqs = app_state.get("tool_shortcuts") or {}
        for name, seq in seqs.items():
            if not seq:
                continue
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(lambda n=name: self.page_tools.open_tool(n))
            self._tool_scuts.append(sc)
        self.page_tools.refresh_sc_hints()      # 卡片标题旁的快捷键提示跟着重注册刷新

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
        self._update_side_status()

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

    def _update_side_status(self):
        if self._checking:                # 检测中不被 2 秒定时刷打断
            return
        from registry.manager import ACCOUNTS, first_check_done
        from core.config import USER_NAME
        if not first_check_done():
            # 首轮探活还没回来：此刻的 healthy 只是默认值，不能当结果画成绿灯
            dots = "检测中…" if ACCOUNTS else "无线路"
        else:
            # 🟢 探活接口正常应答；🟡 服务有话回但探活路径未实现（能提交，但不谎称
            # “已测正常”）；🔴 连不上/5xx —— 三档共用 gui.header.line_light
            from gui.header import line_light
            dots = "  ".join(line_light(a, True)[0] for a in ACCOUNTS)
        text = f"服务：{dots}\n当前用户：{USER_NAME or '未配置'}"
        if self._gateway_mode:
            # 剩余天数只读本地缓存（不联网）；到期前 3 天黄标提醒续费
            from core import license as lic
            left = lic.days_remaining()
            mark = "🟢" if left > 3 else ("🟡" if left > 0 else "🔴")
            text += f"\n授权：{mark} 剩余 {left} 天"
        self.lbl_health.setText(text)

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

    def _recheck_health(self):
        """手动触发一轮真实检测：绿灯必须是测出来的，不是默认值"""
        if self._checking:
            return
        self._checking = True
        self.lbl_health.setText("服务：检测中…")

        def run():
            from registry.manager import check_all_accounts
            try:
                check_all_accounts()
            finally:
                # 回主线程复位（QTimer.singleShot 线程安全）
                QTimer.singleShot(0, self._recheck_done)
        threading.Thread(target=run, daemon=True, name="recheck").start()

    def _recheck_done(self):
        self._checking = False
        self._update_side_status()

    # ---------- 登录身份 / 切换用户 ----------
    def _update_identity(self):
        """顶栏身份区：只有登录了（org 启用且有会话）才显示；单机模式整块隐藏"""
        from store import org_store
        cur = org_store.current()
        if not cur:
            self.lbl_ident.setVisible(False)
            self.btn_switch.setVisible(False)
            return
        role = org_store.ROLES.get(cur["role"], cur["role"])
        dept = f"（{cur['dept']}）" if cur.get("dept") else ""
        self.lbl_ident.setText(f"👤 {cur['name']} · {role}{dept}")
        self.lbl_ident.setVisible(True)
        self.btn_switch.setVisible(True)

    def _switch_user(self):
        """弹登录框换人：成功→重建导航+刷新全部页；取消→维持原会话"""
        from PySide6.QtWidgets import QDialog
        from gui.dialogs_login import LoginDialog
        from store import org_store
        dlg = LoginDialog(self, title="🔄 切换用户")
        dlg.b_cancel.setText("取消")          # 这里取消不退出，只是不换人
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

    def open_console_mode(self):
        """启动命令行（黑窗口）模式，与 GUI 共用 data/aigc.db"""
        from core.config import RUNTIME_DIR
        try:
            if getattr(sys, "frozen", False):
                exe = Path(sys.executable).with_name("AIGC视频助手-命令行.exe")
                if not exe.exists():
                    QMessageBox.information(
                        self, "提示",
                        "未找到「AIGC视频助手-命令行.exe」，\n"
                        "请把它与本程序放在同一目录（运行 build.bat 可同时打包两个版本）")
                    return
                cmd = [str(exe)]
            else:
                py = Path(sys.executable)
                python = py.with_name("python.exe")
                cmd = [str(python if python.exists() else py), "main.py"]
            kwargs = {}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            else:
                kwargs["start_new_session"] = True   # 脱离 GUI 终端，独立运行
            subprocess.Popen(cmd, cwd=str(RUNTIME_DIR), **kwargs)
        except Exception as e:
            QMessageBox.critical(self, "启动失败", str(e))
