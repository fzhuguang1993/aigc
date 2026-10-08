"""
gui/pages_settings.py —— 设置（编辑 config.json，保存后需重启软件生效；
例外：「成品命名」「内容标签」在各自的展开编辑栏里就写盘并立即生效）

线路部署与各接口（翻译/提取凭证）的编辑已整体搬到「接口管理」页
（gui/pages_api.py）：那页默认不进导航，本页口令（Alt+W / Mac ⌘+W）验证
通过后才会出现并自动跳入；本页只留姓名、视频预览、命名/标签展开栏、
输出目录与配置迁移（后者与「保存设置」同行）。
"""
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QStandardPaths, QPoint, QTimer
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QMessageBox, QScrollArea, QFrame,
                               QFileDialog, QInputDialog, QSpinBox, QComboBox,
                               QKeySequenceEdit, QCheckBox, QApplication,
                               QDialog, QDialogButtonBox, QListWidget)

from core.config import (CONFIG_JSON, USER_NAME,
                         DOWNLOAD_DIR, EXPORT_DIR, MATERIAL_DIR, RUNTIME_DIR,
                         GATEWAY_MODE)
from core.paths import DATA_HOME_INFO
from core import license as lic
from core import naming
from core import tags as tag_lib
from core import block_categories
from core import fileindex
from gui.header import page_header
from gui.dialogs_naming import NamingEditor
from gui.dialogs_tags import TagEditor
from gui.dialogs_blocks import BlockCategoryEditor
from gui.maintainer import Gate, app_has_unlocked
from gui.mouse_gesture import (GESTURE_COMMANDS, COMMAND_LABELS,
                              gesture_display, shape_distance, SHAPE_MAX_DIST)
from gui.tools_registry import (TOOLS, set_tool_shortcut, tool_shortcuts,
                                launcher_shortcut, set_launcher_shortcut,
                                home_shortcut, set_home_shortcut,
                                global_hotkey_enabled, set_global_hotkey_enabled,
                                LAUNCHER_TOKEN, HOME_TOKEN,
                                HOME_LABEL, LAUNCHER_LABEL)
from gui import global_hotkey
from gui import tray
from gui.tool_panels import API_MAINTAINER_CODE, MAINTAINER_SHORTCUT, PANEL_FACTORIES
from gui.widgets import VideoPlayerDialog
from gui.menus import StyledMenu
from store import app_state
from gui.theme import tokenize


def _same_path(a, b):
    """判断两个目录字符串是否指向同一处（Windows 忽略大小写/分隔符）。"""
    return os.path.normcase(os.path.normpath(str(a))) == \
        os.path.normcase(os.path.normpath(str(b)))


class _ExpandSection(QWidget):
    """点击标题栏向下展开的编辑栏：平时收起只显示概要，点一下展开内嵌编辑器。
    取代旧的「label + 右侧按钮弹窗」样式（命名/标签都用它）。"""
    def __init__(self, title, editor, icon=""):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self._title = title
        self._icon = icon
        self._summary = ""
        self._open = False
        self._head = QPushButton()
        self._head.setObjectName("AccHead")
        self._head.setCursor(Qt.CursorShape.PointingHandCursor)
        self._head.clicked.connect(self._toggle)
        self._body = QWidget()
        bl = QVBoxLayout(self._body)
        bl.setContentsMargins(2, 4, 2, 2)
        bl.addWidget(editor)
        self._body.setVisible(False)
        v.addWidget(self._head)
        v.addWidget(self._body)
        self._render()

    def set_summary(self, text):
        self._summary = text or ""
        self._render()

    def collapse(self):
        if self._open:
            self._open = False
            self._body.setVisible(False)
            self._render()

    def _toggle(self):
        self._open = not self._open
        self._body.setVisible(self._open)
        self._render()

    def _render(self):
        arrow = "▾" if self._open else "▸"
        self._head.setText(f"{arrow}  {self._icon} {self._title}：{self._summary}")


def _form_label(text, w=110):
    """表单行右对齐标签：所有设置卡共用同一列宽，标签参差是页面显乱的头一个来源"""
    lb = QLabel(text)
    lb.setFixedWidth(w)
    lb.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    lb.setStyleSheet(tokenize("color:#646A73; background:transparent;"))
    return lb


def _field_row(label, widget, hint=""):
    """一行「标签 + 控件 + 可选灰色提示」：设置项统一走这个口径"""
    r = QHBoxLayout()
    r.setSpacing(10)
    r.addWidget(_form_label(label))
    r.addWidget(widget)
    if hint:
        h = QLabel(hint)
        h.setStyleSheet(tokenize("color:#8F959E; font-size:12px; background:transparent;"))
        r.addWidget(h)
    r.addStretch(1)
    return r


def _section_card(title, hint=""):
    """白底圆角分区卡（蓝竖条 + 标题 + 灰色说明，与页头同一视觉语言）：
    设置项按业务分组进卡，不再裸摆在灰底上——商业软件设置页的基本形态"""
    card = QWidget()
    card.setObjectName("Card")
    v = QVBoxLayout(card)
    v.setContentsMargins(18, 14, 18, 16)
    v.setSpacing(12)
    head = QHBoxLayout()
    head.setSpacing(8)
    bar = QLabel()
    bar.setFixedSize(3, 14)
    bar.setStyleSheet(tokenize("background:#3370FF; border-radius:2px;"))
    head.addWidget(bar, 0, Qt.AlignmentFlag.AlignVCenter)
    t = QLabel(title)
    t.setStyleSheet(tokenize("font-size:14px; font-weight:700; color:#1F2329; background:transparent;"))
    head.addWidget(t)
    if hint:
        h = QLabel(hint)
        h.setStyleSheet(tokenize("font-size:12px; color:#8F959E; background:transparent;"))
        head.addWidget(h, 0, Qt.AlignmentFlag.AlignVCenter)
    head.addStretch(1)
    v.addLayout(head)
    return card, v


class SettingsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 整页套一层滚动区：命名/标签展开编辑栏会变高，短窗口下可滚动不裁剪
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(24, 14, 24, 24)
        lay.setSpacing(12)

        head = page_header("设置", "修改保存后重启生效（命名规则除外：保存即生效）", icon="⚙️")
        head.setToolTip(f"配置文件：{CONFIG_JSON}")
        lay.addWidget(head)

        # ---------- 账户与授权 ----------
        card, cv = _section_card("账户与授权", "姓名参与成品文件命名")
        self.name_edit = QLineEdit(USER_NAME)
        self.name_edit.setFixedWidth(240)
        cv.addLayout(_field_row("姓名", self.name_edit))
        # 授权状态（仅网关模式）：到期时间 + 续费入口
        if GATEWAY_MODE:
            lr = QHBoxLayout()
            lr.setSpacing(10)
            self.lbl_license = QLabel()
            self.lbl_license.setObjectName("InlineTip")
            lr.addWidget(_form_label("授权状态"))
            lr.addWidget(self.lbl_license, 1)
            b_renew = QPushButton("💳 输入卡密续费")
            b_renew.setObjectName("GhostBtn")
            b_renew.setToolTip("用一张新卡密续费，剩余天数自动叠加；无需重启")
            b_renew.clicked.connect(self._renew)
            lr.addWidget(b_renew)
            cv.addLayout(lr)
            self._update_license_label()
        lay.addWidget(card)

        # ---------- 界面偏好（UI 偏好，存 ui_state.json：填数字即时写入，下次开播放器即生效） ----------
        card, cv = _section_card("界面偏好", "即时保存，无需重启")
        self.spin_preview = QSpinBox()            # 先连信号、再 setValue，保证初始不落盘
        self.spin_preview.setRange(VideoPlayerDialog.SCALE_MIN, VideoPlayerDialog.SCALE_MAX)
        self.spin_preview.setSingleStep(5)
        self.spin_preview.setSuffix("%")
        self.spin_preview.setFixedWidth(96)
        self.spin_preview.setToolTip(
            f"填数字改大小：{VideoPlayerDialog.SCALE_MIN}%~{VideoPlayerDialog.SCALE_MAX}%，"
            "100%＝默认基准（已整体缩小 30% 后）；数字越大播放器窗口越大，改完下次打开播放器即生效")
        b_pv_reset = QPushButton("恢复默认")
        b_pv_reset.setObjectName("GhostBtn")
        b_pv_reset.setToolTip("回到 100%（即已整体缩小 30% 后的默认基准大小）")
        b_pv_reset.clicked.connect(
            lambda: self.spin_preview.setValue(VideoPlayerDialog.SCALE_DEFAULT))
        self.spin_preview.valueChanged.connect(self._preview_scale_changed)
        self.spin_preview.setValue(self._load_preview_scale())
        pr = QHBoxLayout()
        pr.setSpacing(10)
        pr.addWidget(_form_label("视频预览框大小"))
        pr.addWidget(self.spin_preview)
        pr.addWidget(b_pv_reset)
        pr.addStretch(1)
        cv.addLayout(pr)
        lay.addWidget(card)

        # ---------- 工具快捷键：一键呼出工具中心窗口（存 ui_state，保存即生效） ----------
        card, cv = _section_card("工具快捷键", "按组合键即弹出工具窗口（是否全局生效见下方「托盘与全局快捷键」）")
        self.tool_combo = QComboBox()
        self.tool_combo.setFixedWidth(220)
        for t_name, t_icon, _desc, t_fac in TOOLS:
            if t_fac and t_fac in PANEL_FACTORIES:
                i = self.tool_combo.count()
                self.tool_combo.addItem(f"{t_icon} {t_name}", t_fac)
                # 原名存 UserRole+1：刷新显示时拼上已配键位，不会滚雪球
                self.tool_combo.setItemData(i, f"{t_icon} {t_name}",
                                            Qt.ItemDataRole.UserRole + 1)
        self.tool_key = QKeySequenceEdit()
        self.tool_key.setFixedWidth(220)
        b_key_clear = QPushButton("清空")
        b_key_clear.setObjectName("GhostBtn")
        b_key_clear.setToolTip("清除当前工具的快捷键")
        b_key_clear.clicked.connect(self._clear_tool_key)
        kr = QHBoxLayout()
        kr.setSpacing(10)
        kr.addWidget(_form_label("快捷键"))
        kr.addWidget(self.tool_key)
        kr.addWidget(b_key_clear)
        h = QLabel("示例：F9 / Ctrl+Alt+1；同一键位只归最后一个工具")
        h.setStyleSheet(tokenize("color:#8F959E; font-size:12px; background:transparent;"))
        kr.addWidget(h)
        kr.addStretch(1)
        cv.addLayout(_field_row("工具", self.tool_combo))
        cv.addLayout(kr)
        self.tool_combo.currentIndexChanged.connect(self._load_tool_key)
        self.tool_key.editingFinished.connect(self._save_tool_shortcut)
        self._refresh_tool_combo()
        self._load_tool_key()
        lay.addWidget(card)

        # ---------- 托盘与全局快捷键：收进托盘后还能不能一键到位全靠这里 ----------
        card, cv = _section_card("托盘与全局快捷键", "即时保存，无需重启")
        self.ck_close_tray = QCheckBox("点关闭按钮收进托盘（不退出程序）")
        self.ck_close_tray.setChecked(bool(app_state.get(tray.CLOSE_TO_TRAY_KEY, True)))
        self.ck_min_tray = QCheckBox("点最小化也收进托盘")
        self.ck_min_tray.setChecked(bool(app_state.get(tray.MINIMIZE_TO_TRAY_KEY, False)))
        self.ck_global = QCheckBox("工具快捷键全局生效（焦点在其它软件里也认）")
        self.ck_global.setChecked(global_hotkey_enabled())
        self.ck_auto = QCheckBox("开机自动启动（启动后直接待在托盘）")
        self.ck_auto.setChecked(tray.read_autostart())
        for ck, key in ((self.ck_close_tray, tray.CLOSE_TO_TRAY_KEY),
                        (self.ck_min_tray, tray.MINIMIZE_TO_TRAY_KEY)):
            ck.toggled.connect(lambda on, k=key: self._tray_pref(k, on))
        self.ck_global.toggled.connect(set_global_hotkey_enabled)
        self.ck_global.toggled.connect(lambda _=False: self._refresh_hotkey_status())
        self.ck_auto.toggled.connect(self._toggle_autostart)
        if tray.tray_supported():
            # 三个开关都靠托盘当入口：托盘不可用（被精简掉了、换了三方任务栏）
            # 就把它们灰掉，而不是让用户设了一堆其实不生效的选项
            cv.addWidget(self.ck_close_tray)
            cv.addWidget(self.ck_min_tray)
        else:
            for ck in (self.ck_close_tray, self.ck_min_tray):
                ck.setEnabled(False)
                ck.setToolTip("本机没找到系统托盘，收进托盘不可用")
                cv.addWidget(ck)
        cv.addWidget(self.ck_global)
        cv.addWidget(self.ck_auto)

        lr = QHBoxLayout()
        lr.setSpacing(10)
        self.launch_key = QKeySequenceEdit(QKeySequence(launcher_shortcut()))
        self.launch_key.setFixedWidth(220)
        b_lk_clear = QPushButton("清空")
        b_lk_clear.setObjectName("GhostBtn")
        b_lk_clear.setToolTip("不用总唤出面板，只按每个工具自己的键")
        b_lk_clear.clicked.connect(self._clear_launcher_key)
        lr.addWidget(_form_label(LAUNCHER_LABEL))
        lr.addWidget(self.launch_key)
        lr.addWidget(b_lk_clear)
        lr.addStretch(1)
        cv.addLayout(lr)
        self.launch_key.editingFinished.connect(self._save_launcher_key)

        # 呼出主界面：与总唤出面板分开的两条键（面板是选工具，这条是直接回到界面）
        hr = QHBoxLayout()
        hr.setSpacing(10)
        self.home_key = QKeySequenceEdit(QKeySequence(home_shortcut()))
        self.home_key.setFixedWidth(220)
        b_hk_clear = QPushButton("清空")
        b_hk_clear.setObjectName("GhostBtn")
        b_hk_clear.setToolTip("不配这条：只靠唤出面板或托盘图标回主界面")
        b_hk_clear.clicked.connect(self._clear_home_key)
        hr.addWidget(_form_label(HOME_LABEL))
        hr.addWidget(self.home_key)
        hr.addWidget(b_hk_clear)
        hr.addStretch(1)
        cv.addLayout(hr)
        self.home_key.editingFinished.connect(self._save_home_key)

        self.lbl_hotkey = QLabel()
        self.lbl_hotkey.setWordWrap(True)
        self.lbl_hotkey.setStyleSheet(tokenize(
            "color:#8F959E; font-size:12px; background:transparent;"))
        cv.addWidget(self.lbl_hotkey)
        self._refresh_hotkey_status()
        lay.addWidget(card)

        # ---------- 本地文件搜索：唤出面板下面两段的后端，索引在启动时后台建 ----------
        card, cv = _section_card("本地文件搜索", "在唤出面板里搜本地文件名与文档正文；即时保存，无需重启")
        self._fs_stat_at = 0                # 状态行的取数时间（读 meta 已很快，这层只是防每帧重绘都查库）
        self.ck_filesearch = QCheckBox("启用本地文件搜索")
        self.ck_filesearch.setChecked(fileindex.enabled())
        self.ck_filesearch.setToolTip(
            "文件名索引自动覆盖所有本地固定磁盘（只认固定盘：U 盘/网络盘拔了会让\n"
            "后台扫描反复报错，要搜就在下面“只搜指定目录”里手动加）。\n"
            "扫描时自动跳过 Windows、AppData、node_modules 这些软件目录：\n"
            "本机实测排除后约 95 万个文件，不排除是 173 万个（多出的一般是软件自带的 .py/.pyc）。\n"
            "索引在软件启动后于后台线程建立，不占界面。")
        cv.addWidget(self.ck_filesearch)

        sr = QHBoxLayout()
        sr.setSpacing(10)
        self.combo_fs_scope = QComboBox()
        self.combo_fs_scope.addItems(["全盘（所有本地固定磁盘）", "只搜指定目录"])
        self.combo_fs_scope.setCurrentIndex(1 if fileindex.custom_roots() else 0)
        self.combo_fs_scope.setFixedWidth(260)
        sr.addWidget(_form_label("索引范围"))
        sr.addWidget(self.combo_fs_scope)
        sr.addStretch(1)
        cv.addLayout(sr)
        self.fs_roots_box, self.lst_fs_roots, b_fs_add, b_fs_del = self._dir_box(
            "要纳入索引的目录（一行一个）", fileindex.custom_roots())
        cv.addWidget(self.fs_roots_box)

        self.ck_docsearch = QCheckBox("同时搜索文档正文（txt / md / docx / xlsx / pptx / pdf）")
        self.ck_docsearch.setChecked(fileindex.doc_enabled())
        self.ck_docsearch.setToolTip(
            "正文只抽下面这些文档目录，不抽全盘：本机实测全盘 1.9 万个可抽文档里\n"
            "有 1.5 万个是软件自带的 txt，抽了既慢又搜不到有用的东西。\n"
            "中文按「二元组」建全文索引，所以搜「关节」能命中「关节不舒服」这种两字词。")
        cv.addWidget(self.ck_docsearch)
        self.fs_doc_box = QWidget()
        fv = QVBoxLayout(self.fs_doc_box)
        fv.setContentsMargins(0, 0, 0, 0)
        fv.setSpacing(8)
        self.doc_roots_box, self.lst_doc_roots, b_doc_add, b_doc_del = self._dir_box(
            "要抽正文的文档目录（留空＝用默认：文档/下载/桌面与本软件素材目录）",
            fileindex.doc_roots())
        fv.addWidget(self.doc_roots_box)
        mr = QHBoxLayout()
        mr.setSpacing(10)
        self.spin_doc_mb = QSpinBox()
        self.spin_doc_mb.setRange(1, 500)
        self.spin_doc_mb.setSuffix(" MB")
        self.spin_doc_mb.setValue(int(fileindex.doc_max_bytes() / 1024 / 1024))
        mr.addWidget(_form_label("单文件上限"))
        mr.addWidget(self.spin_doc_mb)
        mr.addWidget(QLabel("超过就不抽（PDF / PPT 只取前 80 页）"))
        mr.addStretch(1)
        fv.addLayout(mr)
        cv.addWidget(self.fs_doc_box)

        # 唤出面板默认视图（列表/中图标/大图标）：存 app_state，唤出时读
        vr = QHBoxLayout()
        vr.setSpacing(10)
        self.combo_fs_view = QComboBox()
        self.combo_fs_view.addItems(["列表", "中图标网格", "大图标网格"])
        _dft_view = app_state.get("launcher_view_default") or "list"
        self.combo_fs_view.setCurrentIndex(
            max(0, ("list", "medium", "large").index(_dft_view)))
        self.combo_fs_view.setFixedWidth(260)
        vr.addWidget(_form_label("默认视图"))
        vr.addWidget(self.combo_fs_view)
        vr.addWidget(QLabel("唤出面板打开时结果用哪种视图（面板内临时切换不写回这里）"))
        vr.addStretch(1)
        cv.addLayout(vr)

        # 桌面悬浮球：点球即弹唤出搜索面板（给不想每次按键呼出的人）
        br = QHBoxLayout()
        br.setSpacing(10)
        self.ck_ball = QCheckBox("在桌面显示唤出悬浮球")
        self.ck_ball.setChecked(bool(app_state.get("launcher_ball_on")))
        self.ck_ball.setToolTip(
            "开启后桌面出现一颗可拖动的小球，点一下就打开搜索面板；右键小球可关闭。")
        br.addWidget(self.ck_ball)
        br.addWidget(QLabel("不想每次按热键呼出时的替代入口：点球即开"))
        br.addStretch(1)
        cv.addLayout(br)

        ar = QHBoxLayout()
        ar.setSpacing(10)
        b_fs_rebuild = QPushButton("🔁 立即重建索引")
        b_fs_rebuild.setObjectName("GhostBtn")
        b_fs_rebuild.setToolTip(
            "清掉整份索引重扫：改了目录、或怀疑索引不对时用。\n"
            "在后台跑，不卡界面；索引库坏了也只是删了重扫，不连累业务数据。")
        b_fs_rebuild.clicked.connect(self._fs_rebuild)
        ar.addWidget(b_fs_rebuild)
        self.lbl_filesearch = QLabel()
        self.lbl_filesearch.setWordWrap(True)
        self.lbl_filesearch.setStyleSheet(tokenize(
            "color:#8F959E; font-size:12px; background:transparent;"))
        self.lbl_filesearch.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        ar.addWidget(self.lbl_filesearch, 1)
        cv.addLayout(ar)
        # 初值灌完再接信号（与「界面偏好」同一口径）：否则 setValue 就当改过、多存一次
        self.fs_roots_box.setVisible(self.combo_fs_scope.currentIndex() == 1)
        self.fs_doc_box.setVisible(fileindex.doc_enabled())
        self.ck_filesearch.toggled.connect(self._fs_set_enabled)
        self.ck_docsearch.toggled.connect(self._fs_set_doc)
        self.combo_fs_scope.currentIndexChanged.connect(self._fs_scope_changed)
        self.spin_doc_mb.valueChanged.connect(self._fs_set_max_mb)
        self.combo_fs_view.currentIndexChanged.connect(self._fs_set_default_view)
        self.ck_ball.toggled.connect(self._set_ball)
        b_fs_add.clicked.connect(lambda _=False: self._dir_add(
            self.lst_fs_roots, self._fs_save_roots))
        b_fs_del.clicked.connect(lambda _=False: self._dir_del(
            self.lst_fs_roots, self._fs_save_roots))
        b_doc_add.clicked.connect(lambda _=False: self._dir_add(
            self.lst_doc_roots, self._doc_save_roots))
        b_doc_del.clicked.connect(lambda _=False: self._dir_del(
            self.lst_doc_roots, self._doc_save_roots))
        for w in (self.ck_filesearch, self.ck_docsearch, self.combo_fs_scope,
                  self.spin_doc_mb, b_fs_rebuild):
            w.setEnabled(fileindex.enabled())
        self._fs_stat_at = 0
        self._refresh_filesearch_status()
        lay.addWidget(card)

        # ---------- 鼠标手势：按住右键划轨迹呼出命令（存 ui_state，保存即生效） ----------
        card, cv = _section_card("鼠标手势", "在软件任意窗口按住右键划一段轨迹，"
                                              "松开即执行命令；同一命令在不同页面自动对应不同行为")
        self._gesture_rows = {}
        gr0 = QHBoxLayout()
        gr0.setSpacing(10)
        self.ck_gesture = QCheckBox("启用鼠标手势")
        self.ck_gesture.setChecked(bool(app_state.get("gesture_enabled", True)))
        self.ck_gesture.stateChanged.connect(
            lambda _s: app_state.set_value("gesture_enabled",
                                           self.ck_gesture.isChecked()))
        b_g_clear = QPushButton("清空全部绑定")
        b_g_clear.setObjectName("GhostBtn")
        b_g_clear.clicked.connect(self._clear_all_gestures)
        gr0.addWidget(self.ck_gesture)
        gr0.addWidget(b_g_clear)
        self.lbl_gesture = QLabel("未绑定的命令不会触发，右键照常弹菜单")
        self.lbl_gesture.setStyleSheet(tokenize("color:#8F959E; font-size:12px; background:transparent;"))
        gr0.addWidget(self.lbl_gesture, 1)
        cv.addLayout(gr0)
        for cid, label, tip in GESTURE_COMMANDS:
            rr = QHBoxLayout()
            rr.setSpacing(10)
            lb = QLabel(label)
            lb.setFixedWidth(170)
            lb.setStyleSheet(tokenize("color:#1F2329; background:transparent;"))
            lb.setToolTip(tip)
            val = QLabel("")
            val.setFixedWidth(110)
            val.setStyleSheet(tokenize("color:#3370FF; background:transparent;"))
            b_rec = QPushButton("✏ 录制手势…")
            b_rec.setObjectName("GhostBtn")
            b_rec.clicked.connect(lambda _=False, c=cid: self._record_gesture(c))
            b_un = QPushButton("解绑")
            b_un.setObjectName("GhostBtn")
            b_un.clicked.connect(lambda _=False, c=cid: self._unbind_gesture(c))
            rr.addWidget(lb)
            rr.addWidget(val)
            rr.addWidget(b_rec)
            rr.addWidget(b_un)
            rr.addStretch(1)
            cv.addLayout(rr)
            self._gesture_rows[cid] = val
        self._gesture_cid = None               # 正在录制的命令（None＝没在录）
        self._gesture_engine = None           # 懒取主窗口引擎并接线一次
        self._refresh_gestures()
        lay.addWidget(card)

        # ---------- 内容规则：命名/标签点击展开的编辑栏（保存即生效，不进下面的「保存设置」） ----------
        card, cv = _section_card("内容规则", "展开编辑，保存即生效")
        self.editor_naming = NamingEditor()
        self.editor_naming.saved.connect(self._on_naming_saved)
        self.sec_naming = _ExpandSection("成品命名", self.editor_naming, icon="🏷")
        cv.addWidget(self.sec_naming)
        self._refresh_naming()

        self.editor_tags = TagEditor()
        self.editor_tags.saved.connect(self._on_tags_saved)
        self.sec_tags = _ExpandSection("内容标签", self.editor_tags, icon="🏷")
        cv.addWidget(self.sec_tags)
        self._refresh_tags()

        self.editor_blocks = BlockCategoryEditor()
        self.editor_blocks.saved.connect(self._on_blocks_saved)
        self.sec_blocks = _ExpandSection("素材类别", self.editor_blocks, icon="🎬")
        cv.addWidget(self.sec_blocks)
        self._refresh_blocks()
        lay.addWidget(card)

        # ---------- 输出目录：默认隐藏，Alt+W（Mac ⌘+W）口令解锁后才出现（不暴露入口） ----------
        # 口令同时放出导航里的「🔌 接口管理」页：一个口令管全部维护人入口
        # 网关模式（终端买家）：线路/接口都在服务端，买家无线路可维护，
        # 但输出目录是个人偏好：直接放出来（不走口令）
        self._dirs_unlocked = app_has_unlocked("settings_dirs") or GATEWAY_MODE
        self._gate_dirs = Gate("settings_dirs")
        # 本会话是否已验证过「接口管理」：与按天解锁标记无关，重启后归 False，
        # 保证每次启动第一次 Alt+W 必须先弹口令框验证，验证通过才放出页面。
        self._api_verified = False
        card, cv = _section_card("输出目录", "留空＝用默认，修改保存后重启生效")
        self.dirs_box = card
        self._dir_edits = {}
        for key, label, cur, dft in (
                ("output", "视频输出", DOWNLOAD_DIR, str(RUNTIME_DIR / "outputs")),
                ("export", "模板/导出", EXPORT_DIR, str(RUNTIME_DIR / "exports")),
                ("material", "素材目录", MATERIAL_DIR, str(RUNTIME_DIR / "material"))):
            ed = QLineEdit("" if _same_path(cur, dft) else cur)
            ed.setPlaceholderText("默认：" + dft)
            ed.setProperty("default", dft)
            b = QPushButton("浏览…")
            b.setObjectName("GhostBtn")
            b.clicked.connect(lambda _=False, e=ed: self._pick_dir(e))
            r = QHBoxLayout()
            r.setSpacing(10)
            r.addWidget(_form_label(label))
            r.addWidget(ed, 1)
            r.addWidget(b)
            cv.addLayout(r)
            self._dir_edits[key] = ed
        # 数据家说明：装进 Program Files 后产物不再落在 exe 旁边，老用户升级上来
        # 第一反应是“我的视频不见了”——把真正在写哪个目录明写在这里
        home = DATA_HOME_INFO.get("home") or str(RUNTIME_DIR)
        mode = {"installed": "安装版数据目录", "portable": "绿色版（随程序目录）",
                "env": "AIGC_HOME 指定", "dev": "开发态（仓库目录）"}.get(
            DATA_HOME_INFO.get("mode"), "数据目录")
        row = QLabel(f"📁 {mode}：{home}")
        row.setStyleSheet(tokenize("color:#8F959E; font-size:12px; background:transparent;"))
        row.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cv.insertWidget(1, row)
        if DATA_HOME_INFO.get("legacy_media"):
            # 旧绿色版把几十 GB 产物堆在 exe 旁：不自动搬（那是别人的盘），
            # 只给一个“把旧路径填进来”的入口，用户点保存才写盘
            n = len(DATA_HOME_INFO["legacy_media"])
            b_old = QPushButton(f"📂 旧目录里还有产物（{n} 个），帮我指过去")
            b_old.setObjectName("GhostBtn")
            b_old.setToolTip(
                f"旧版本把产物放在程序目录：{DATA_HOME_INFO.get('legacy_home') or ''}\n"
                "点一下把旧路径填进下面的输入框，再点「保存设置」并重启才生效。")
            b_old.clicked.connect(self._fill_legacy_dirs)
            cv.insertWidget(2, b_old)
        self.dirs_box.setVisible(self._dirs_unlocked)   # 当天验过口令才默认展开
        lay.addWidget(card)

        # ---------- 底部动作栏：钉在窗口底（不随内容滚动） ----------
        # 迁移入口贴左下角、主操作「保存」贴右下角；只写姓名/输出目录，
        # 线路与各接口归「接口管理」页自己的保存按钮管。
        foot = QWidget()
        foot.setObjectName("SettingsFoot")
        srow = QHBoxLayout(foot)
        srow.setContentsMargins(24, 10, 24, 10)
        srow.setSpacing(10)
        b_pkg = QPushButton("📦 配置迁移 ▾")
        b_pkg.setObjectName("GhostBtn")
        b_pkg.setToolTip(
            "把本机全部配置与产品业务数据（含产品图片、SMB/溯源账密、命名/字段）\n"
            "加密成一个 .aigccfg 整包导出，或从同事的整包一键导入。\n"
            "含凭证与业务数据，只发给信得过的同事；点按钮展开选择导出/导入")
        self._pkg_menu = self._build_pkg_menu()
        b_pkg.clicked.connect(
            lambda: self._pkg_menu.exec(b_pkg.mapToGlobal(QPoint(0, b_pkg.height()))))
        b_lines = QPushButton("🔗 线路小包 ▾")
        b_lines.setObjectName("GhostBtn")
        b_lines.setToolTip(
            "只导出/导入接口线路（名称+地址+并发，.aigcline 加密小包），\n"
            "线路天天换时用这个，其它配置一个字不动；点按钮展开选择导出/导入")
        self._lines_menu = self._build_lines_menu()
        b_lines.clicked.connect(
            lambda: self._lines_menu.exec(b_lines.mapToGlobal(QPoint(0, b_lines.height()))))
        b_pkg.setVisible(not GATEWAY_MODE)
        b_lines.setVisible(not GATEWAY_MODE)
        srow.addWidget(b_pkg)
        srow.addWidget(b_lines)
        srow.addStretch(1)
        b_save = QPushButton("💾 保存设置")
        b_save.clicked.connect(self._save)
        srow.addWidget(b_save)
        outer.addWidget(foot)

        # 卡片顶部对齐：没有这个 stretch，滚动区会把多余空间摊到卡片之间，
        # 页面全是“空洞”（旧版最丑的来源）；内容超高时它自动让位。
        lay.addStretch(1)

        # 维护人入口：Alt+W（Mac ⌘+W）唤出口令框，验证通过才显示「输出目录」；
        # 仅当停在设置页时激活（show/hideEvent 开关），避免别处误触。
        self._sc_dirs = QShortcut(QKeySequence(MAINTAINER_SHORTCUT), self)
        self._sc_dirs.setContext(Qt.ShortcutContext.WindowShortcut)
        self._sc_dirs.activated.connect(self._summon_dirs)
        self._sc_dirs.setEnabled(False)

    def refresh(self):
        if GATEWAY_MODE:
            self._update_license_label()
        self._refresh_filesearch_status()     # 索引进度是后台线程写的，跟着刷一次
        # 不自动覆盖用户正在编辑的内容（命名/线路等）

    # ---------- 本地文件搜索（开关与目录都即时落盘，后台线程下一轮就认） ----------
    def _dir_box(self, hint, paths):
        """一行灰字说明 + 目录列表 + 「添加/移除」：文件名索引与文档正文两份清单
        共用同一形态，各写一遍迟早长歪。返回 (容器, 列表, 添加钮, 移除钮)。"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(24, 0, 0, 0)
        v.setSpacing(4)
        lb = QLabel(hint)
        lb.setStyleSheet(tokenize("color:#8F959E; font-size:12px; background:transparent;"))
        v.addWidget(lb)
        lst = QListWidget()
        lst.setFixedHeight(86)
        for p in paths or []:
            lst.addItem(str(p))
        lst.setToolTip("选中一行再点「移除选中」：只是不再进索引，磁盘上的目录不动")
        v.addWidget(lst)
        r = QHBoxLayout()
        r.setSpacing(8)
        b_add = QPushButton("＋ 添加目录…")
        b_add.setObjectName("GhostBtn")
        b_del = QPushButton("－ 移除选中")
        b_del.setObjectName("GhostBtn")
        r.addWidget(b_add)
        r.addWidget(b_del)
        r.addStretch(1)
        v.addLayout(r)
        return w, lst, b_add, b_del

    def _dir_paths(self, lst):
        return [lst.item(i).text() for i in range(lst.count())]

    def _dir_add(self, lst, save):
        d = QFileDialog.getExistingDirectory(self, "选择要纳入的目录")
        if not d:
            return
        for i in range(lst.count()):            # 同一目录列两遍＝扫两遍，没意义
            if _same_path(d, lst.item(i).text()):
                return
        lst.addItem(os.path.normpath(d))
        lst.setCurrentRow(lst.count() - 1)
        save(self._dir_paths(lst))

    def _dir_del(self, lst, save):
        it = lst.currentItem()
        if it is None:
            self.lbl_filesearch.setText("先在列表里选中一行，再点「移除选中」")
            return
        lst.takeItem(lst.row(it))
        save(self._dir_paths(lst))

    def _fs_save_roots(self, paths):
        fileindex.set_custom_roots(paths)
        self._fs_kick()

    def _doc_save_roots(self, paths):
        fileindex.set_doc_roots(paths)
        self._fs_kick()

    def _fs_kick(self):
        """改了范围/上限就催后台线程提前跑一轮：让人干等那 120 秒不像话"""
        self._fs_stat_at = 0
        try:
            from workers import file_watcher
            file_watcher.start()
            file_watcher.kick()
        except Exception:
            pass

    def _fs_set_enabled(self, on):
        """开关是真能立刻停/起后台线程的：关了还在扫盘就是骗人"""
        fileindex.set_enabled(on)
        self._fs_stat_at = 0
        for w in (self.ck_docsearch, self.combo_fs_scope, self.spin_doc_mb):
            w.setEnabled(on)
        try:
            from workers import file_watcher
            if on:
                file_watcher.start()
                file_watcher.kick()
            else:
                file_watcher.stop()
        except Exception:
            pass
        self._refresh_filesearch_status()

    def _fs_set_doc(self, on):
        fileindex.set_doc_enabled(on)
        self.fs_doc_box.setVisible(on)
        self._fs_kick()

    def _fs_scope_changed(self, i):
        """全盘＝清空自定义清单（回到“所有固定盘”这个默认口径）；
        指定目录＝改用列表里这几条。"""
        self.fs_roots_box.setVisible(i == 1)
        if i == 0:
            self.lst_fs_roots.clear()
            fileindex.set_custom_roots([])
            self._fs_kick()
        elif self.lst_fs_roots.count():
            fileindex.set_custom_roots(self._dir_paths(self.lst_fs_roots))
        # 列表还空着时不落盘：空清单本身就等于“全盘”，此时写下去等于什么都没改

    def _fs_set_max_mb(self, mb):
        fileindex.set_doc_max_mb(mb)
        self._fs_kick()

    def _fs_set_default_view(self, _i=None):
        """唤出面板默认视图：写 app_state，下次 summon 生效（不改当次已开的面板）。"""
        app_state.set_value(
            "launcher_view_default",
            ("list", "medium", "large")[self.combo_fs_view.currentIndex()])

    def _set_ball(self, on):
        """唤出悬浮球开关：落盘 + 主窗口立即显示/收起（不必重启）。"""
        app_state.set_value("launcher_ball_on", bool(on))
        w = self.window()
        if hasattr(w, "apply_launcher_ball"):
            w.apply_launcher_ball()

    def _fs_rebuild(self):
        """清库重扫：整条链丢给后台线程。首扫实测几十秒（冷盘 45 秒），
        放在界面上就是点了按钮之后整个软件假死。"""
        if QMessageBox.question(
                self, "重建本地文件索引",
                "清掉整份索引并重扫一遍（后台执行，不影响继续使用）。\n"
                "本机首扫实测约 45 秒，期间搜索仍能命中已经收录的部分。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes) != QMessageBox.StandardButton.Yes:
            return
        self.lbl_filesearch.setText("正在重建索引…（后台跑，稍后自动更新）")
        self._fs_stat_at = time.time() + 30       # 刚清完的空库不该马上刷出“0 个文件”
        threading.Thread(target=self._fs_rebuild_worker,
                         daemon=True, name="file-index-rebuild").start()

    def _fs_rebuild_worker(self):
        from workers import file_watcher
        try:
            fileindex.rebuild()
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
            QTimer.singleShot(0, lambda: self.lbl_filesearch.setText(
                "⚠ 重建失败：%s｜索引库：%s（它坏了就手动删掉再重建）"
                % (err, fileindex.DB_PATH)))
            return
        try:
            file_watcher.start()
            file_watcher.kick()
        except Exception:
            pass
        self._fs_stat_at = 0
        QTimer.singleShot(0, self._refresh_filesearch_status)

    def _refresh_filesearch_status(self):
        """状态行：已收录多少、上次扫到什么时候、下一轮多久后。
        status() 只读 meta 里的缓存计数（不再对 95 万行做 COUNT(*)——那一下要
        4.6 秒，正好卡在界面上），所以 2 秒一刷也没负担；这里只留 5 秒下限
        避免页面重绘一次就开一次库。"""
        if not hasattr(self, "lbl_filesearch"):
            return
        now = time.time()
        if self._fs_stat_at and now - self._fs_stat_at < 5:
            return
        self._fs_stat_at = now
        try:
            st = fileindex.status()
        except Exception as e:
            self.lbl_filesearch.setText("⚠ 索引库暂不可用：%s" % type(e).__name__)
            return

        def _when(ts):
            return (datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                    if ts else "还没扫过")

        from workers import file_watcher
        ws = file_watcher.stats()
        # 间隔是自适应的（一轮越慢下一轮推得越远），所以报“下轮多久后”而不是硬编 120 秒
        nxt = int(ws.get("next_sec") or file_watcher.ROUND_SEC)
        last = float(ws.get("last_sec") or 0)
        self.lbl_filesearch.setText(
            "已收录 %s 个文件 / %s 篇文档｜上次扫描 %s｜正文更新 %s｜后台%s（上轮 %.1f 秒，下轮 %d 秒后）"
            % ("{:,}".format(int(st.get("files") or 0)),
               "{:,}".format(int(st.get("docs") or 0)),
               _when(st.get("last_scan") or 0), _when(st.get("last_docs") or 0),
               "正在索引" if file_watcher.running() else "未运行",
               last, nxt))

    # ---------- 授权/续费（网关模式） ----------
    def _update_license_label(self):
        left = lic.days_remaining()
        exp = lic.expire_at()
        when = datetime.fromtimestamp(exp).strftime("%Y-%m-%d") if exp else "-"
        if left <= 0:
            self.lbl_license.setText("⚠ 授权已到期，请续费")
            self.lbl_license.setStyleSheet("color: #E5484D;")
        elif left <= 3:
            self.lbl_license.setText(f"🟡 剩余 {left} 天（{when} 到期），请尽快续费")
            self.lbl_license.setStyleSheet("color: #FF8D19;")
        else:
            self.lbl_license.setText(f"🟢 剩余 {left} 天（{when} 到期）")
            self.lbl_license.setStyleSheet("")

    def _renew(self):
        from gui.dialogs_license import LicenseDialog   # 只在打开时导入，减启动开销
        if LicenseDialog(self, mode="renew").exec():
            self._update_license_label()

    # ---------- 命名规则（展开编辑栏内保存即生效） ----------
    def _refresh_naming(self):
        """把当前生效的规则写成一句概要显示在折叠标题上"""
        self.sec_naming.set_summary(
            f"{naming.describe()}　→　例如 {naming.preview()}")

    def _on_naming_saved(self):
        self._refresh_naming()
        self.sec_naming.collapse()

    # ---------- 内容标签词库（同样：展开编辑栏内保存即生效） ----------
    def _refresh_tags(self):
        self.sec_tags.set_summary("、".join(tag_lib.load()))

    def _on_tags_saved(self):
        self._refresh_tags()
        self.sec_tags.collapse()
        w = self.window()
        page = getattr(w, "page_tasks", None)            # 让任务表筛选下拉同步新词库
        if page is not None and hasattr(page, "refresh"):
            page.refresh()

    # ---------- 素材板块类别（同样：展开编辑栏内保存即生效） ----------
    def _refresh_blocks(self):
        self.sec_blocks.set_summary("、".join(block_categories.load()))

    def _on_blocks_saved(self):
        self._refresh_blocks()
        self.sec_blocks.collapse()
        w = self.window()
        page = getattr(w, "page_material", None)         # 素材库「类型」下拉同步新类别
        if page is not None and hasattr(page, "rebuild_filters"):
            page.rebuild_filters()
            page._reload()

    # ---------- 视频预览框大小（存 ui_state，与播放器共享同一个键） ----------
    def _load_preview_scale(self):
        try:
            v = int(app_state.get(VideoPlayerDialog.SCALE_KEY)
                    or VideoPlayerDialog.SCALE_DEFAULT)
        except (TypeError, ValueError):
            v = VideoPlayerDialog.SCALE_DEFAULT
        return max(VideoPlayerDialog.SCALE_MIN,
                   min(VideoPlayerDialog.SCALE_MAX, v))

    def _preview_scale_changed(self, val):
        app_state.set_value(VideoPlayerDialog.SCALE_KEY, int(val))

    # ---------- 工具快捷键（存 ui_state：tool_shortcuts = {工具名: 键序列}） ----------
    def _refresh_tool_combo(self):
        """下拉项文本实时拼上已配键位：一眼看出哪些工具设过快捷键"""
        seqs = app_state.get("tool_shortcuts") or {}
        for i in range(self.tool_combo.count()):
            name = self.tool_combo.itemData(i)
            # 没附基名的项（后续新增加的入口、外部 addItem）回退到当前文本：
            # 这里一旦拿到 None，整个设置页刷新就直接 TypeError
            base = self.tool_combo.itemData(i, Qt.ItemDataRole.UserRole + 1) or name or ""
            sc = seqs.get(name) or ""
            self.tool_combo.setItemText(i, base + (f"　[{sc}]" if sc else ""))

    def _load_tool_key(self):
        """切工具时把已存键位回填到输入框（没配就清空）"""
        name = self.tool_combo.currentData()
        seq = (app_state.get("tool_shortcuts") or {}).get(name or "", "")
        self.tool_key.setKeySequence(QKeySequence(seq))

    def _clear_tool_key(self):
        self.tool_key.clear()
        self._save_tool_shortcut()

    def _save_tool_shortcut(self):
        """编辑完（失焦/回车）即落盘并让主窗口重注册快捷键，不用重启；
        与工具卡片右键共用 set_tool_shortcut（互斥/清理由它统一维护）。
        半截组合键不落盘：否则旧键位被释放而新键位注册不上。"""
        name = self.tool_combo.currentData()
        if not name:
            return
        seq = self._key_seq_or_hint(self.tool_key)
        if seq is None:
            return
        set_tool_shortcut(name, seq)
        self._refresh_tool_combo()
        self._refresh_hotkey_status()

    # ---------- 托盘与全局快捷键：开关都即时生效，只有开机自启要写注册表 ----------
    def _tray_pref(self, key, on):
        """写偏好并让主窗口按新规矩接管 ✕ / 最小化（不必重启）"""
        app_state.set_value(key, bool(on))
        w = self.window()
        if hasattr(w, "refresh_tray_prefs"):
            w.refresh_tray_prefs()

    def _hotkey_label(self, token):
        """热键表里的 token 翻成人话：工具名直接用，两个保留位是内部标识"""
        return {LAUNCHER_TOKEN: LAUNCHER_LABEL,
                HOME_TOKEN: HOME_LABEL}.get(token, str(token))

    def _refresh_hotkey_status(self):
        """如实回报系统热键注册结果。

        键位被微信/QQ 抢了，RegisterHotKey 只会返回 FALSE，界面不说就是一句
        “设了键就是不灵”——用户只能靠猜，我们也只能靠日志翻。"""
        if not hasattr(self, "lbl_hotkey"):
            return
        hk = global_hotkey.manager
        seqs = [s for s in (tool_shortcuts() or {}).values() if s]
        if launcher_shortcut():
            seqs.append(launcher_shortcut())
        if home_shortcut():
            seqs.append(home_shortcut())
        if not hk.available():
            self._set_hotkey_text("#FF8D19", "⚠ 本机不支持系统级热键（仅 Windows）："
                                             "快捷键只在软件窗口内生效")
            return
        if not global_hotkey_enabled():
            self._set_hotkey_text("#8F959E", "未开启全局生效：快捷键只在本软件窗口内认，"
                                             "焦点在其它软件里按不动")
            return
        bad = dict(hk.failures)
        bound = len(hk.bound_tokens())
        if bad:
            detail = "；".join(f"{self._hotkey_label(t)}：{r}"
                            for t, r in bad.items())
            self._set_hotkey_text("#FF8D19", f"⚠ {detail}（这些键已退回“窗口内快捷键”，"
                                          "软件开着才认；换一个组合键即可）")
        elif bound:
            self._set_hotkey_text("#3370FF", f"✓ 已注册 {bound} 个系统级热键："
                                          "软件收进托盘、焦点在其它软件里也能一键弹工具")
        elif seqs:
            self._set_hotkey_text("#FF8D19", "已开启全局生效，但一个键都没注册上："
                                          "到上方重新按一次组合键重试")
        else:
            self._set_hotkey_text("#8F959E", "还没配快捷键：到上方「工具快捷键」"
                                          "或本卡「总唤出快捷键」设一个")

    def _set_hotkey_text(self, color, text):
        self.lbl_hotkey.setText(text)
        self.lbl_hotkey.setStyleSheet(tokenize(
            f"color:{color}; font-size:12px; background:transparent;"))

    def _toggle_autostart(self, checked):
        """开机自启写的是注册表，不是本地偏好：写不进去必须把勾退回并说清原因，
        否则用户以为开机会有软件，第二天找不到还以为被安全软件删了。"""
        ok, msg = tray.write_autostart(checked)
        if ok:
            w = self.window()
            if hasattr(w, "refresh_tray_prefs"):
                w.refresh_tray_prefs()        # 托盘菜单上的勾选态跟着同步
            return
        self.ck_auto.blockSignals(True)
        self.ck_auto.setChecked(not checked)
        self.ck_auto.blockSignals(False)
        QMessageBox.warning(self, "开机自启", msg or "写入注册表失败")

    def _key_seq_or_hint(self, ed):
        """取输入框里的键位；只按了半截（比如手里只剩修饰键）就当没按完：返回 None。

        QKeySequenceEdit 在 Ctrl+Alt+… 这种半截状态也会把变更发出来，而注册层是先释放
        旧键再注新键：拿半截串落盘＝旧键位白丢、新键位又解析不出来，界面上一眼看去
        还像设好了（本机日志里那几条“键位不可用（需带修饰键）”就是这么来的）。
        判定口径直接用注册层的 parse_sequence，界面与注册不会两边不一致。"""
        seq = ed.keySequence().toString()
        if seq and global_hotkey.parse_sequence(seq) is None:
            self._set_hotkey_text("#FF8D19", "⚠ 组合键没按完（现在只有修饰键）："
                                             "请连着按出一个完整组合，旧键位暂时不变")
            return None
        return seq

    def _save_launcher_key(self):
        """总唤出键：落盘 + 主窗口立即重注册，失败原因回到下方状态行"""
        seq = self._key_seq_or_hint(self.launch_key)
        if seq is None:
            return
        set_launcher_shortcut(seq)
        self._refresh_hotkey_status()

    def _clear_launcher_key(self):
        self.launch_key.clear()
        self._save_launcher_key()

    def _save_home_key(self):
        """呼出主界面的键：落盘 + 主窗口立即重注册，失败原因回到下方状态行。

        与总唤出键同口径：“半截组合”不落盘（见 _key_seq_or_hint），同键的
        另一个槽位会让位（见 set_home_shortcut），不会出现两个动作抢一个键。"""
        seq = self._key_seq_or_hint(self.home_key)
        if seq is None:
            return
        set_home_shortcut(seq)
        self._refresh_hotkey_status()

    def _clear_home_key(self):
        self.home_key.clear()
        self._save_home_key()

    # ---------- 鼠标手势：录制 / 绑定 / 解绑 ----------
    def _engine(self):
        """懒取主窗口上的手势引擎，并把录制信号接过来（只接一次）"""
        eng = getattr(self.window(), "gesture", None)
        if eng is not None and self._gesture_engine is None:
            eng.record_finished.connect(self._on_gesture_recorded)
            eng.record_cancelled.connect(self._on_gesture_cancelled)
            self._gesture_engine = eng
        return eng

    def _refresh_gestures(self):
        bound = 0
        gmap = app_state.get("gesture_map") or {}
        for cid, val in self._gesture_rows.items():
            ent = gmap.get(cid)
            seq = ent.get("seq") if isinstance(ent, dict) else ent   # 兼容旧纯串格式
            if seq:
                val.setText(gesture_display(seq))
                bound += 1
            else:
                val.setText("未绑定")
                val.setStyleSheet(tokenize("color:#8F959E; background:transparent;"))
                continue
            val.setStyleSheet(tokenize("color:#3370FF; background:transparent;"))
        self.lbl_gesture.setText(
            f"已绑定 {bound} 条：软件内按住右键划出轨迹（形状相近即触发，不必划得一模一样）"
            if bound else "未绑定的命令不会触发，右键照常弹菜单")

    def _record_gesture(self, cid):
        eng = self._engine()
        if eng is None:
            QMessageBox.warning(self, "不可用", "手势引擎还没初始化（不在主窗口里？）")
            return
        if self._gesture_cid is not None:
            return                                   # 已经在录另一条：忽略本次
        self._gesture_cid = cid
        self.lbl_gesture.setText(
            f"✏ 请按住鼠标右键划出「{COMMAND_LABELS[cid]}」的轨迹，松开即绑定；"
            "原地点松开取消")
        eng.start_record()

    def _on_gesture_recorded(self, seq, shape):
        cid, self._gesture_cid = self._gesture_cid, None
        if cid is None:
            return
        gmap = dict(app_state.get("gesture_map") or {})

        def _pts(ent):
            return ent.get("pts") if isinstance(ent, dict) else None

        # 与其命令形状太像（距离≤阈值）的 = 会被本次抢走，先拎出来确认
        clash = [c for c, ent in gmap.items()
                 if c != cid and _pts(ent) and shape
                 and shape_distance(shape, _pts(ent)) <= SHAPE_MAX_DIST]
        owner = clash[0] if clash else None
        if owner and QMessageBox.question(
                self, "轨迹相近",
                f"这个形状与已绑定的「{COMMAND_LABELS.get(owner, owner)}」很相似，\n"
                f"改绑到「{COMMAND_LABELS[cid]}」吗？（旧命令会被解绑）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes) != QMessageBox.StandardButton.Yes:
            self.lbl_gesture.setText("保持原绑定：未对任何命令做修改")
            return
        for c in clash:
            gmap.pop(c)
        gmap[cid] = {"seq": seq, "pts": shape}
        app_state.set_value("gesture_map", gmap)
        self._refresh_gestures()
        self.lbl_gesture.setText(
            f"✓ 已绑定「{COMMAND_LABELS[cid]}」← {gesture_display(seq)}（之后再划出相近形状即可触发）")

    def _on_gesture_cancelled(self):
        if self._gesture_cid is not None:
            self._gesture_cid = None
            self.lbl_gesture.setText("没划出轨迹：录制已取消，再点「录制手势」重来")

    def _unbind_gesture(self, cid):
        gmap = dict(app_state.get("gesture_map") or {})
        if gmap.pop(cid, None) is not None:
            app_state.set_value("gesture_map", gmap)
        self._refresh_gestures()

    def _clear_all_gestures(self):
        if not (app_state.get("gesture_map") or {}):
            return
        if QMessageBox.question(
                self, "清空手势", "确定解绑全部鼠标手势吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            app_state.set_value("gesture_map", {})
            self._refresh_gestures()

    # ---------- 配置迁移 / 线路包下拉菜单（各将导出/导入合一） ----------
    def _build_pkg_menu(self):
        m = StyledMenu(self)
        m.addAction("📤 导出配置包", self._export_pkg)
        m.addAction("📥 一键导入配置", self._import_pkg)
        return m

    def _build_lines_menu(self):
        m = StyledMenu(self)
        m.addAction("🔗 导出线路小包", self._export_lines)
        m.addAction("📥 导入线路小包", self._import_lines)
        return m

    # ---------- 维护人入口：Alt+W 先弹口令框，验证通过才放出「接口管理」（并展开输出目录） ----------
    def _summon_dirs(self):
        if GATEWAY_MODE:                 # 买家模式：输出目录已直接展示，接口管理不对买家开放
            return
        w = self.window()
        # 本会话已经验证过：再按 Alt+W 直接跳回「接口管理」，不重复弹框；
        # （新开会话 _api_verified 重置为 False，会重新要求验证）
        if self._api_verified:
            self.dirs_box.setVisible(True)
            if hasattr(w, "reveal_api_page"):
                w.reveal_api_page()
            return
        # 本会话首次：强制弹口令框（force=True 绕过「当天已验」缓存），验证通过才放出页面
        if not self._gate_dirs.ask(self, force=True):
            return
        self.dirs_box.setVisible(True)
        self._dirs_unlocked = True
        self._api_verified = True
        if hasattr(w, "reveal_api_page"):
            w.reveal_api_page()           # 「🔌 接口管理」补进导航并直接跳过去

    def showEvent(self, event):
        super().showEvent(event)
        self._sc_dirs.setEnabled(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sc_dirs.setEnabled(False)

    def _pick_dir(self, ed):
        start = ed.text().strip() or ed.property("default") or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "选择目录", start)
        if d:
            ed.setText(d)

    def _fill_legacy_dirs(self):
        """把旧版程序目录里的产物路径填进上面的输入框（只填还空着的）。

        只填不存：填完用户还能换个盘，点「保存设置」才写盘——旧产物动辄几十 GB，
        自动改配置或自动拷文件都不是好主意。"""
        keys = {"outputs": "output", "exports": "export", "material": "material"}
        filled = []
        for d in DATA_HOME_INFO.get("legacy_media") or []:
            key = keys.get(Path(d).name.lower())
            ed = self._dir_edits.get(key)
            if ed is None or ed.text().strip():
                continue
            ed.setText(str(d))
            filled.append(Path(d).name)
        if filled:
            QMessageBox.information(
                self, "已填好路径",
                "已把旧目录填进上面的输入框：" + "、".join(filled) + "\n"
                "确认无误后点「保存设置」并重启软件生效。")
        else:
            QMessageBox.information(
                self, "不需要修改",
                "旧目录要么已经是当前设置，要么对应输入框里已有路径；\n"
                "想换位置直接在输入框里改就行。")

    def _save(self):
        data = {}
        if CONFIG_JSON.exists():        # 先读旧配置，合并写回，不丢其它字段
            try:
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["user_name"] = self.name_edit.text().strip()
        # 输出目录：只存与默认不同的自定义值；全空则删除 paths 键（回到默认）
        paths = {}
        for key, ed in self._dir_edits.items():
            v = ed.text().strip()
            if v and not _same_path(v, ed.property("default")):
                paths[key] = v
        if paths:
            data["paths"] = paths
        else:
            data.pop("paths", None)
        CONFIG_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        # 保存后不再收回输出目录：同一功能当天验过一次就不再问（跨天才重新验）
        QMessageBox.information(self, "已保存", "设置已保存，重启软件后生效")

    # ================= 配置迁移（加密包） =================
    def _pick_export_items(self, cp):
        """导出前勾选要打包的部分：每个已登记条目一行复选框，默认全选。
        返回选中的 id 列表；取消或未选返回 None（调用方据此中止）。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("选择要导出的配置")
        dlg.setMinimumWidth(420)
        v = QVBoxLayout(dlg)
        v.setSpacing(8)
        tip = QLabel("勾选要打包进配置包的部分（默认全部）：\n"
                     "含接口凭证与产品业务数据，只发给信得过的同事。")
        tip.setObjectName("InlineTip")
        tip.setWordWrap(True)
        v.addWidget(tip)
        boxes = []
        for it in cp.ITEMS:
            cb = QCheckBox(it.title)
            cb.setChecked(True)
            v.addWidget(cb)
            boxes.append((it.id, cb))
        quick = QHBoxLayout()
        b_all = QPushButton("全选")
        b_all.setObjectName("GhostBtn")
        b_none = QPushButton("全不选")
        b_none.setObjectName("GhostBtn")
        b_all.clicked.connect(lambda: [cb.setChecked(True) for _, cb in boxes])
        b_none.clicked.connect(lambda: [cb.setChecked(False) for _, cb in boxes])
        quick.addWidget(b_all)
        quick.addWidget(b_none)
        quick.addStretch(1)
        v.addLayout(quick)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("下一步：选保存位置")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if not dlg.exec():
            return None
        chosen = [iid for iid, cb in boxes if cb.isChecked()]
        if not chosen:
            QMessageBox.information(self, "提示", "没有勾选任何要导出的内容")
            return None
        return chosen

    def _export_pkg(self):
        """先勾选要导出的部分，再选保存位置，加密成一个 .aigccfg 文件
        （口令内置，见 core/config_package.py）"""
        from core import config_package as cp
        chosen = self._pick_export_items(cp)
        if chosen is None:
            return
        here = (QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DesktopLocation) or str(Path.home()))
        default = str(Path(here) / f"AIGC配置_{time.strftime('%m%d')}{cp.SUFFIX}")
        path, _ = QFileDialog.getSaveFileName(self, "导出配置包（加密）", default,
                                              "AIGC 配置包 (*" + cp.SUFFIX + ")")
        if not path:
            return
        if not path.endswith(cp.SUFFIX):
            path += cp.SUFFIX
        try:
            info = cp.export_package(path, only_ids=chosen)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        if not info["items"]:
            QMessageBox.warning(self, "导出失败",
                                "勾选的部分本机没有可导出的内容（如本机还没有配置）")
            return
        QMessageBox.information(
            self, "已导出",
            "已把 {n} 个文件（{items}）加密导出到：\n{p}\n\n"
            "文件已用软件内置口令加密，只有装了本软件的机器能导入；\n"
            "但里面含接口凭证和业务数据，请只发给信得过的同事。".format(
                n=info["files"], items="、".join(info["items"]), p=path))

    def _import_pkg(self):
        """选包 → 解密（内置口令优先，不对再问一次）→ 确认 → 逐条目覆盖并备份"""
        from core import config_package as cp
        path, _ = QFileDialog.getOpenFileName(
            self, "选择配置包", str(Path.home()),
            "AIGC 配置包 (*" + cp.SUFFIX + ");;所有文件 (*)")
        if not path:
            return
        try:
            pkg = cp.read_package(path)          # 正常同事间互发：零输入
        except ValueError as e:
            if "口令" not in str(e):
                # 根本不是包/内容坏了：问口令也没用，直接报
                QMessageBox.warning(self, "导入失败", str(e))
                return
            # 口令不是内置那个（老包/改过口令）：问一句，别把文件判死
            text, ok = QInputDialog.getText(
                self, "需要口令", f"{e}\n\n如果这个包用了自定义口令，请在下面输入：")
            if not ok:
                return
            try:
                pkg = cp.read_package(path, text.strip())
            except ValueError as e2:
                QMessageBox.warning(self, "导入失败", str(e2))
                return
        known, unknown = cp.plan_import(pkg)
        lines = [f"・{t}" for t, _ in known]
        if unknown:
            lines.append("（本机版本不认识、将跳过：" + "、".join(unknown) + "）")
        if not known:
            QMessageBox.warning(self, "导入失败", "配置包里没有任何本机可导入的内容")
            return
        if QMessageBox.question(
                self, "确认导入",
                "将覆盖本机以下内容：\n" + "\n".join(lines) + "\n\n"
                "配置文件旧值会备份成 *.bak-时间戳；产品表整表替换；"
                "任务列表不受影响。\n确定导入吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            result = cp.apply_package(pkg)
        except OSError as e:
            QMessageBox.warning(self, "导入失败", f"写入失败：{e}")
            return
        # 界面立刻跟着新配置走：注意不能吃 config.ACCOUNTS（那是启动期缓存），
        # 线路与各接口在「接口管理」页，让它直接读刚落盘的 config.json 重建
        try:
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            self._refresh_api_page()
            self.name_edit.setText(str(data.get("user_name") or ""))
            self._refresh_naming()
        except Exception:
            pass            # 刷界面失败不要紧：盘上已生效，重启就好
        done = "、".join(result["applied"])
        QMessageBox.information(self, "导入完成",
                                f"已导入：{done}。\n"
                                "旧配置已备份，产品页切过去即是新数据；\n"
                                "接口线路等重启软件后全部生效。")

    # ---------- 线路小包：地址日更的轻量进出口 ----------
    def _refresh_api_page(self):
        """让「接口管理」页按盘上的 config.json 重建线路表与接口字段

        那些控件已不在本页，隔着 window 递话过去；页内自己读盘重建，
        不能吃启动缓存 ACCOUNTS（导入刚写完盘它还是旧值）。"""
        page = getattr(self.window(), "page_api", None)
        if page is not None:
            page.reload_from_disk()

    def _export_lines(self):
        """只把线路列表加密成 .aigcline 小文件（默认桌面，文件名带日期）"""
        from core import config_package as cp
        here = (QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DesktopLocation) or str(Path.home()))
        default = str(Path(here) / f"线路_{time.strftime('%m%d')}{cp.LINES_SUFFIX}")
        path, _ = QFileDialog.getSaveFileName(self, "导出线路包（加密）", default,
                                              "AIGC 线路包 (*" + cp.LINES_SUFFIX + ")")
        if not path:
            return
        if not path.endswith(cp.LINES_SUFFIX):
            path += cp.LINES_SUFFIX
        try:
            info = cp.export_lines(path)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        QMessageBox.information(
            self, "已导出",
            f"已把 {info['count']} 条线路加密导出到：\n{path}\n\n"
            "只有装了本软件的机器能打开；同事拿到后「📥 导入线路」即可。\n"
            "提醒：接口地址本身就是访问凭证，只发给内部同事。")

    def _import_lines(self):
        """选线路包 → 解密 → 确认（只列名字不露地址）→ 只替换 accounts 段"""
        from core import config_package as cp
        path, _ = QFileDialog.getOpenFileName(
            self, "选择线路包", str(Path.home()),
            "AIGC 线路包 (*" + cp.LINES_SUFFIX + ");;所有文件 (*)")
        if not path:
            return
        try:
            pkg = cp.read_lines(path)          # 内置口令：同事间互发零输入
        except ValueError as e:
            if "口令" not in str(e):
                # 拿错文件/坏文件：问口令也没用，直接报
                QMessageBox.warning(self, "导入失败", str(e))
                return
            text, ok = QInputDialog.getText(
                self, "需要口令", f"{e}\n\n如果这个包用了自定义口令，请在下面输入：")
            if not ok:
                return
            try:
                pkg = cp.read_lines(path, text.strip())
            except ValueError as e2:
                QMessageBox.warning(self, "导入失败", str(e2))
                return
        rows = pkg["accounts"]
        names = "、".join(r["name"] for r in rows[:8])
        if len(rows) > 8:
            names += f" …等 {len(rows)} 条"
        if QMessageBox.question(
                self, "导入线路",
                f"线路包里有 {len(rows)} 条：{names}\n"
                f"导出时间：{pkg['exported_at'] or '未知'}\n\n"
                "将替换本机当前部署的全部线路；"
                "其它配置一律不动。\n确定导入吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            n = cp.apply_lines(rows)
        except OSError as e:
            QMessageBox.warning(self, "导入失败", f"写入失败：{e}")
            return
        self._refresh_api_page()
        QMessageBox.information(
            self, "导入完成",
            f"已导入 {n} 条线路，旧 config.json 已备份。\n"
            "重启软件后调度器按新线路跑；正在进行的任务不受影响。")
