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
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QStandardPaths, QPoint
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QMessageBox, QMenu, QScrollArea, QFrame,
                               QFileDialog, QInputDialog, QSpinBox, QComboBox,
                               QKeySequenceEdit)

from core.config import (CONFIG_JSON, USER_NAME,
                         DOWNLOAD_DIR, EXPORT_DIR, MATERIAL_DIR, RUNTIME_DIR,
                         GATEWAY_MODE)
from core import license as lic
from core import naming
from core import tags as tag_lib
from gui.header import page_header
from gui.dialogs_naming import NamingEditor
from gui.dialogs_tags import TagEditor
from gui.pages_tools import TOOLS
from gui.tool_panels import API_MAINTAINER_CODE, MAINTAINER_SHORTCUT, PANEL_FACTORIES
from gui.widgets import VideoPlayerDialog
from store import app_state


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
    lb.setStyleSheet("color:#646A73; background:transparent;")
    return lb


def _field_row(label, widget, hint=""):
    """一行「标签 + 控件 + 可选灰色提示」：设置项统一走这个口径"""
    r = QHBoxLayout()
    r.setSpacing(10)
    r.addWidget(_form_label(label))
    r.addWidget(widget)
    if hint:
        h = QLabel(hint)
        h.setStyleSheet("color:#8F959E; font-size:12px; background:transparent;")
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
    bar.setStyleSheet("background:#3370FF; border-radius:2px;")
    head.addWidget(bar, 0, Qt.AlignmentFlag.AlignVCenter)
    t = QLabel(title)
    t.setStyleSheet("font-size:14px; font-weight:700; color:#1F2329; background:transparent;")
    head.addWidget(t)
    if hint:
        h = QLabel(hint)
        h.setStyleSheet("font-size:12px; color:#8F959E; background:transparent;")
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
        lay.setContentsMargins(24, 12, 24, 24)
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
        card, cv = _section_card("工具快捷键", "全窗口任意页面按组合键即弹出工具窗口，保存即生效")
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
        h.setStyleSheet("color:#8F959E; font-size:12px; background:transparent;")
        kr.addWidget(h)
        kr.addStretch(1)
        cv.addLayout(_field_row("工具", self.tool_combo))
        cv.addLayout(kr)
        self.tool_combo.currentIndexChanged.connect(self._load_tool_key)
        self.tool_key.editingFinished.connect(self._save_tool_shortcut)
        self._refresh_tool_combo()
        self._load_tool_key()
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
        lay.addWidget(card)

        # ---------- 输出目录：默认隐藏，Alt+W（Mac ⌘+W）口令解锁后才出现（不暴露入口） ----------
        # 口令同时放出导航里的「🔌 接口管理」页：一个口令管全部维护人入口
        # 网关模式（终端买家）：线路/接口都在服务端，买家无线路可维护，
        # 但输出目录是个人偏好：直接放出来（不走口令）
        self._dirs_unlocked = False
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
        self.dirs_box.setVisible(False)
        lay.addWidget(card)
        if GATEWAY_MODE:
            self.dirs_box.setVisible(True)
            self._dirs_unlocked = True

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
        # 不自动覆盖用户正在编辑的内容（命名/线路等）

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
            base = self.tool_combo.itemData(i, Qt.ItemDataRole.UserRole + 1)
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
        """编辑完（失焦/回车）即落盘并让主窗口重注册快捷键，不用重启"""
        name = self.tool_combo.currentData()
        if not name:
            return
        seq = self.tool_key.keySequence().toString()
        seqs = dict(app_state.get("tool_shortcuts") or {})
        if seq:
            # 一键一工具：同键其他工具让位，避免一个键弹两个窗口
            for k in [k for k, v in seqs.items() if v == seq and k != name]:
                seqs.pop(k)
            seqs[name] = seq
        else:
            seqs.pop(name, None)
        app_state.set_value("tool_shortcuts", seqs)
        self._refresh_tool_combo()
        w = self.window()
        if hasattr(w, "apply_tool_shortcuts"):
            w.apply_tool_shortcuts()

    # ---------- 配置迁移 / 线路包下拉菜单（各将导出/导入合一） ----------
    def _build_pkg_menu(self):
        m = QMenu(self)
        m.addAction("📤 导出配置包", self._export_pkg)
        m.addAction("📥 一键导入配置", self._import_pkg)
        return m

    def _build_lines_menu(self):
        m = QMenu(self)
        m.addAction("🔗 导出线路小包", self._export_lines)
        m.addAction("📥 导入线路小包", self._import_lines)
        return m

    # ---------- 维护人入口：口令解锁「输出目录」并放出导航里的「接口管理」 ----------
    def _summon_dirs(self):
        if GATEWAY_MODE:                 # 买家模式：输出目录已直接展示，不要口令
            return
        if self._dirs_unlocked:               # 已展开则不重复要口令
            return
        code, ok = QInputDialog.getText(self, "维护人验证", "请输入维护人口令：",
                                        QLineEdit.EchoMode.Password)
        if not ok:
            return
        if code == API_MAINTAINER_CODE:
            self.dirs_box.setVisible(True)
            self._dirs_unlocked = True
            w = self.window()
            if hasattr(w, "reveal_api_page"):
                w.reveal_api_page()       # 「🔌 接口管理」补进导航并直接跳过去
        else:
            QMessageBox.warning(self, "口令错误", "维护人口令不正确")

    def _relock_dirs(self):
        # 只收回输出目录；接口管理页的导航项本次运行期内保持可见
        # （口令都验证过了，再藏没意义）
        self.dirs_box.setVisible(False)
        self._dirs_unlocked = False

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
        if self._dirs_unlocked:            # 保存后收回隐藏，下次再改需重新按口令
            self._relock_dirs()
        QMessageBox.information(self, "已保存", "设置已保存，重启软件后生效")

    # ================= 配置迁移（加密包） =================
    def _export_pkg(self):
        """全部配置加密成一个 .aigccfg 文件（口令内置，见 core/config_package.py）"""
        from core import config_package as cp
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
            info = cp.export_package(path)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
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
