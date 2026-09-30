"""
gui/pages_tools.py —— 工具中心
小工具以卡片形式上架：点击「✓ 可用」卡片弹出工具窗口（非模态，可同时开多个）。
新增工具两步：
  1. 在 gui/tool_panels.py 的 PANEL_FACTORIES 里登记 factory；
  2. 在下方 TOOLS 里加一条卡片信息（factory 传 None 显示为「规划中」）。
功能逻辑一律放 video_text_tools 包（纯功能、无 UI），本层只做界面。
"""
from PySide6.QtCore import Qt, QTimer, QPoint, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QScrollArea, QSizePolicy, QDialog, QGridLayout,
                               QGraphicsDropShadowEffect, QKeySequenceEdit,
                               QDialogButtonBox)

from gui.header import page_header
from gui.tool_panels import PANEL_FACTORIES
from gui.window_frame import apply_rounded
from gui.menus import StyledMenu
from store import app_state
from gui.theme import tokenize

# 工具登记表：名称 / 图标 / 简介 / factory 名（None = 规划中）
TOOLS = [
    ("视频水印", "💧", "批量打静态/碰撞反弹水印，或统一格式化压制分辨率码率", "视频水印"),
    ("批量改名", "🏷", "数字/字母/罗马/希腊编号规则批量重命名，先预览再执行", "批量改名"),
    ("封面提取", "🖼", "查看视频参数（ffprobe），抽取指定帧生成封面图", "封面提取"),
    ("批量粘贴录入", "⌨", "剪贴板多行文本逐行自动粘贴（需辅助功能授权）", "批量粘贴录入"),
    ("SMB 上传", "📤", "成品视频批量上传到公司共享盘（需配置服务器账号）", "SMB 上传"),
    ("视频溯源", "🔎", "溯源码池取码、按规则重命名并入库（需内网 MySQL）", "视频溯源"),
    ("素材提取", "🧲", "粘贴唞喑/筷手分享链接：提取去水印视频、图集、文案", "素材提取"),
    ("屏幕录制", "🎥", "全屏/框选区域/指定窗口录制，选帧率码率保存 MP4", "屏幕录制"),
    ("录屏测试Demo", "🎬", "极简：一键录全屏，可选系统声音与画面混成单个 MP4（测试用）", "录屏测试Demo"),
    ("语音识别", "🎙", "选视频→Whisper 转口播逐字稿，导出 TXT/SRT 字幕、DeepSeek 纠错、可烧录", "语音识别"),
    ("爆款拆解", "🔥", "粘贴爆款链接：拆分镜/口播，产出画面/文案/复刻 3 类提示词与整体分析", "爆款拆解"),
    ("一键发布", "🚀", "选成品视频与平台账号，一键分发到抖音/快手/小红书/视频号", "一键发布"),
    ("素材瘦身", "🗜", "把参考图批量压缩到接口要求的大小，避免上传失败", None),
    ("文案查重", "🔍", "提示词相似度检查，防止一批任务生成的视频互相雷同", None),
]

CARD_W, CARD_H, GRID_GAP = 250, 150, 14

# ---------- 固定在左侧（卡片右键可 pin，持久化进 ui_state.json） ----------
PINNED_KEY = "pinned_tools"


def load_pinned():
    """已固定工具名列表；下架/失效的名字读时顺手丢掉（脏数据不留）。
    登记表的 factory 名恰好等于工具显示名，两边共用同一个键"""
    valid = {f for _n, _i, _d, f in TOOLS if f and f in PANEL_FACTORIES}
    return [n for n in (app_state.get(PINNED_KEY) or []) if n in valid]


def set_pinned(name, on):
    cur = [n for n in app_state.get(PINNED_KEY) or [] if n != name]
    if on:
        cur.append(name)
    app_state.set_value(PINNED_KEY, cur)


class ToolCard(QWidget):
    """单个工具卡片：可用时点击打开工具窗口。

    hover 不再靠 enter/leave 里贴/撕一段内联样式（那样只换个边框、
    还没阴影，看着像 winxp）：改用常驻 QGraphicsDropShadowEffect，鼠标进入
    时用 QPropertyAnimation 把阴影“养大”（模糊半径/下偏量/蓝色透明度
    同时变），配合 QSS #ToolCard:hover 的蓝色描边，呈现卡片“浮起来”的动画。

    ⚠ 卡内所有子控件对鼠标透明（WA_TransparentForMouseEvents）：否则鼠标
    移到图标/文字上时卡片会收到 Leave（子控件是独立的 hover 目标），动效
    忽灭忽现就是“抖动”的根因；点击也落在子控件上收不到，只有卡片空白处
    能点开——整卡必须是一个交互单元。

    动效时长取 320ms：170ms 时卡片间快速扫鼠标、点开瞬间的明暗切换都嫌
    “抽抽”，放缓后浮起/落回连贯得多（用户反馈“抖动太快”）。
    标题旁用灰色小字显示已配快捷键（右键卡片可设/改/清），随主窗口
    apply_tool_shortcuts() 重注册同步刷新（set_sc_hint）。"""
    _REST = (16, QPoint(0, 2), QColor(15, 23, 42, 26))       # 静止：轻阴影
    _HOVER = (30, QPoint(0, 8), QColor(51, 112, 255, 55))    # 悬停：浮起蓝影

    def __init__(self, name, icon, desc, factory, on_open, parent=None):
        super().__init__(parent)
        self.setObjectName("ToolCard")
        # ⚠ PySide6 的 Python 子类不会自动开 WA_StyledBackground，
        # 不设就是透明卡——白底/描边全被跳过（只在裸 QWidget 实例上才自动）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.tool_name = name
        self.factory = factory
        self._on_open = on_open
        self.setFixedSize(250, 150)
        # 驱动 QSS：只有可用（有 factory）的卡片悬停才变蓝描边，规划中卡片不变
        self.setProperty("canopen", "1" if factory else "0")
        # WA_Hover：让样式表的 :hover 伪态在普通 QWidget 上生效（否则不自换边框）
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._shadow = QGraphicsDropShadowEffect(self)
        r = self._REST
        self._shadow.setBlurRadius(r[0])
        self._shadow.setOffset(r[1])
        self._shadow.setColor(r[2])
        self.setGraphicsEffect(self._shadow)
        self._anim = QPropertyAnimation(self._shadow, b"blurRadius", self)
        self._anim.setDuration(320)
        self._anim2 = QPropertyAnimation(self._shadow, b"offset", self)
        self._anim2.setDuration(320)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(6)
        h = QHBoxLayout()
        badge = QLabel(icon)
        badge.setFixedSize(34, 34)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(tokenize("background:#EAF1FF; border-radius:9px; font-size:17px;"))
        self._badge = badge
        t = QLabel(name)
        t.setStyleSheet(tokenize("font-size:14px; font-weight:700; color:#1F2329; background:transparent;"))
        self._title_lbl = t
        sc = QLabel("")
        sc.setStyleSheet(tokenize("font-size:12px; font-weight:600; color:#8F959E; background:transparent;"))
        self._sc_lbl = sc
        h.addWidget(badge)
        h.addWidget(t)
        h.addWidget(sc)
        h.addStretch(1)
        lay.addLayout(h)
        d = QLabel(desc)
        d.setWordWrap(True)
        d.setStyleSheet(tokenize("font-size:12px; color:#646A73; background:transparent;"))
        lay.addWidget(d)
        lay.addStretch(1)
        state = QLabel("✓ 可用 · 点击打开" if factory else "🚧 规划中")
        state.setStyleSheet(
            "font-size:12px; color:#00A870; background:transparent;" if factory else
            "font-size:12px; color:#8F959E; background:transparent;")
        lay.addWidget(state)
        # 整卡一个交互单元：子控件不吃鼠标（见类注释的抖动/点不开根因）
        for w in (badge, t, self._sc_lbl, d, state):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        if factory:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.set_sc_hint((app_state.get("tool_shortcuts") or {}).get(name, ""))

    def set_sc_hint(self, seq):
        """标题旁的快捷键小字：随保存/主窗口重注册同步刷新"""
        self._sc_lbl.setText(f"[{seq}]" if seq else "")
        self._sc_lbl.setVisible(bool(seq))

    def mouseReleaseEvent(self, e):
        if self.factory and e.button() == Qt.MouseButton.LeftButton \
                and self.rect().contains(e.position().toPoint()):
            self._on_open(self)
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        """右键：固定到左侧导航 / 设置・更改・清除快捷键（仅可用工具）"""
        if not self.factory:
            return
        pinned = self.tool_name in load_pinned()
        cur = (app_state.get("tool_shortcuts") or {}).get(self.tool_name, "")
        m = StyledMenu(self)
        m.addAction("📌 从左侧导航取消" if pinned else "📌 固定在左侧导航",
                    self._toggle_pin)
        m.addAction("⌨ 更改快捷键…" if cur else "⌨ 设置快捷键…", self._set_shortcut)
        if cur:
            m.addAction("✕ 清除快捷键", self._clear_shortcut)
        m.exec(e.globalPos())

    def _set_shortcut(self):
        """卡片就地录入：按下组合键→确定即生效；清空键位＝取消绑定"""
        dlg = QDialog(self)
        dlg.setWindowTitle("工具快捷键")
        dlg.setModal(True)
        v = QVBoxLayout(dlg)
        cur = (app_state.get("tool_shortcuts") or {}).get(self.tool_name, "")
        v.addWidget(QLabel(f"为「{self.tool_name}」按下组合键（示例：F9 / Ctrl+Alt+1）：\n"
                           "同一键位只归一个工具，冲突时其它工具自动让位"))
        ed = QKeySequenceEdit(QKeySequence(cur))
        ed.keySequenceChanged.connect(lambda ks: dlg.accept() if ks.toString() else None)
        v.addWidget(ed)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        ed.setFocus()
        if not dlg.exec():
            return
        from gui.pages_settings import set_tool_shortcut
        set_tool_shortcut(self.tool_name, ed.keySequence().toString())
        w = self.window()
        page = getattr(w, "page_tools", None)
        if page is not None:
            page.refresh_sc_hints()          # 全部卡片提示跟着刷新（含让位的）

    def _clear_shortcut(self):
        from gui.pages_settings import set_tool_shortcut
        set_tool_shortcut(self.tool_name, "")
        w = self.window()
        page = getattr(w, "page_tools", None)
        if page is not None:
            page.refresh_sc_hints()

    def _toggle_pin(self):
        set_pinned(self.tool_name, self.tool_name not in load_pinned())
        w = self.window()
        if hasattr(w, "_rebuild_nav"):        # 主窗口即时把导航项补出来/收回去
            w._rebuild_nav()

    def _animate_shadow(self, target):
        """把当前阴影的模糊半径/下偏量补间到目标值（颜色直接切换，过渡足够自然）"""
        blur, off, col = target
        self._shadow.setColor(col)
        self._anim.stop()
        self._anim.setStartValue(self._shadow.blurRadius())
        self._anim.setEndValue(blur)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.start()
        self._anim2.stop()
        self._anim2.setStartValue(self._shadow.offset())
        self._anim2.setEndValue(off)
        self._anim2.start()

    def _set_badge_hover(self, on):
        """悬停时图标徽章跟着加深一档：动效铺满整卡，不只边框和阴影"""
        self._badge.setStyleSheet(
            f"background:{'#D6E4FF' if on else '#EAF1FF'}; border-radius:9px; font-size:17px;")

    def enterEvent(self, e):
        if self.factory:
            self._animate_shadow(self._HOVER)
            self._set_badge_hover(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._animate_shadow(self._REST)
        self._set_badge_hover(False)
        super().leaveEvent(e)


class ToolDialog(QDialog):
    """工具窗口：承载具体面板，非模态显示。

    与全家桶其它弹窗同一套无边框圆角外壳（apply_rounded 自动插标题栏），
    不再是带原生方角标题栏的默认 QDialog。"""

    def __init__(self, name, factory, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"🧰 {name}")
        self.resize(880, 620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        self.panel = factory(self)
        lay.addWidget(self.panel)
        apply_rounded(self, title=f"🧰 {name}")


class ToolsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._dialogs = {}          # name -> ToolDialog（保持引用防回收）
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)
        lay.addWidget(page_header("工具中心", "周边小工具集合 · 点击卡片打开 · 持续扩充", icon="🧰"))

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        holder = QWidget()
        # 网格排版：按窗口宽度自动换行（以前是一整排 QHBoxLayout，卡片多了一行不换行）
        self._grid = QGridLayout(holder)
        self._grid.setSpacing(GRID_GAP)
        self._grid.setContentsMargins(10, 14, 10, 14)
        self._cards = []
        self._cols = 0
        for name, icon, desc, fac_name in TOOLS:
            factory = PANEL_FACTORIES.get(fac_name) if fac_name else None
            card = ToolCard(name, icon, desc, factory, self._open)
            card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            self._cards.append(card)
        area.setWidget(holder)
        self._area = area
        lay.addWidget(area, 1)
        QTimer.singleShot(0, self._relayout)      # 首次显示按实际宽度排一次

        tip = QLabel("有想要的实用小工具？告诉维护人，排期上架～　·　右键卡片可固定在左侧导航，"
                     "还可在「设置-工具快捷键」配一键呼出")
        tip.setObjectName("PageTip")
        lay.addWidget(tip)

    def _relayout(self):
        """卡片按视口宽度自动分列，放不下一行就换行"""
        w = self._area.viewport().width()
        cols = max(1, (w - 8 + GRID_GAP) // (CARD_W + GRID_GAP))
        if cols == self._cols:
            return
        self._cols = cols
        while self._grid.count():
            self._grid.takeAt(0)               # 只移布局项，卡片控件仍由 self._cards 持有
        for i, card in enumerate(self._cards):
            self._grid.addWidget(card, i // cols, i % cols,
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._grid.setRowStretch(self._grid.rowCount(), 1)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._relayout)   # 防抖：拖拽窗口时合并重排

    def _open(self, card):
        self.open_tool(card.tool_name)

    def open_tool(self, name):
        """按工具名打开窗口（卡片点击 / 左侧固定项 / 快捷键共用同一入口）：
        已有窗口则前置，否则新建；父挂主窗口，不随页面切换被藏掉"""
        factory = PANEL_FACTORIES.get(name)
        if factory is None:
            return
        dlg = self._dialogs.get(name)
        if dlg is None or not dlg.isVisible():
            dlg = ToolDialog(name, factory, self.window() or self)
            self._dialogs[name] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        # 首次打开该工具：面板布局稳定后尝试起分步引导（看过则自动跳过）
        QTimer.singleShot(600, lambda: self._maybe_tool_guide(name, dlg))

    def _maybe_tool_guide(self, name, dlg):
        """为首次打开的重点工具（爆款拆解/素材提取/屏幕录制）起高亮向导。

        面板可能为依赖缺失占位窗，getattr 取不到控件时该步自动退化跳过。"""
        from gui import onboarding
        panel = getattr(dlg, "panel", None)
        if panel is None:
            return
        g = lambda attr: (lambda: getattr(panel, attr, None))
        specs = {
            "爆款拆解": ("tool_爆款拆解", [
                (g("ed_input"), "粘贴爆款链接", "一行一个分享链接，支持拆多个；拆完自动入库可在「拆解任务」回看。"),
                (g("cb_model"), "选拆解模型", "选择用于分镜/口播拆解的模型（需先在隐藏接口管理页配好豆包/DeepSeek）。"),
                (g("b_run"), "开始拆解", "点开始后会后台跑，完成时自动写入拆解历史库。"),
            ]),
            "素材提取": ("tool_素材提取", [
                (g("ed_input"), "粘贴分享链接", "支持抖音/快手等分享链接，一行一个，提取去水印视频/图集/文案。"),
                (g("ck_video"), "选提取内容", "勾选要提取的部分：去水印视频 / 文案 / 图集。"),
                (g("ed_out"), "保存目录", "提取结果的存放位置；文案会额外追加进样本库。"),
            ]),
            "屏幕录制": ("tool_屏幕录制", [
                (g("cb_screen"), "选录制区域", "全屏 / 框选区域 / 指定窗口三种录制方式在此选择。"),
                (g("cb_fps"), "帧率与码率", "选帧率与码率；越高越清晰但文件越大。"),
                (g("ck_subtitle"), "录完生成字幕", "勾选后录完可顺带用 Whisper 生成字幕（需先在隐藏接口管理页下好模型）。"),
                (g("b_run"), "开始录制", "点击开始录制，倒计时后正式录制；录制期间可暂停/停止。"),
            ]),
        }
        spec = specs.get(name)
        if spec:
            feature, steps = spec
            onboarding.maybe_run(dlg, feature, steps)

    def refresh(self):
        pass

    def refresh_sc_hints(self):
        """卡片快捷键提示全量重读（保存/清除后调用，让位的卡片也会变灰空）"""
        seqs = app_state.get("tool_shortcuts") or {}
        for card in self._cards:
            card.set_sc_hint(seqs.get(card.tool_name, ""))
