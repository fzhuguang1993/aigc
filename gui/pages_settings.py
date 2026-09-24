"""
gui/pages_settings.py —— 设置（编辑 config.json，保存后需重启软件生效；
例外：「🏷 命名规则」在自己的对话框里就写盘并立即生效）

线路部署与各接口（AI 检测/翻译/提取凭证）的编辑已整体搬到「接口管理」页
（gui/pages_api.py）：那页默认不进导航，本页口令（Alt+W / Mac ⌘+W）验证
通过后才会出现并自动跳入；本页只留姓名、命名规则、输出目录与配置迁移。
"""
import json
import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, QStandardPaths
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QMessageBox,
                               QFileDialog, QInputDialog, QSlider)

from core.config import (CONFIG_JSON, USER_NAME,
                         DOWNLOAD_DIR, EXPORT_DIR, MATERIAL_DIR, RUNTIME_DIR)
from core import naming
from gui.header import page_header
from gui.tool_panels import API_MAINTAINER_CODE, MAINTAINER_SHORTCUT
from gui.widgets import VideoPlayerDialog
from store import app_state


def _same_path(a, b):
    """判断两个目录字符串是否指向同一处（Windows 忽略大小写/分隔符）。"""
    return os.path.normcase(os.path.normpath(str(a))) == \
        os.path.normcase(os.path.normpath(str(b)))


class SettingsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)

        head = page_header("设置", "修改保存后重启生效（命名规则除外：保存即生效）", icon="⚙️")
        head.setToolTip(f"配置文件：{CONFIG_JSON}")
        lay.addWidget(head)

        row = QHBoxLayout()
        row.addWidget(QLabel("姓名："))
        self.name_edit = QLineEdit(USER_NAME)
        self.name_edit.setFixedWidth(220)
        row.addWidget(self.name_edit)
        row.addStretch(1)
        lay.addLayout(row)

        # ---------- 成品命名规则（唯一「保存即生效」的一项，不进下面的「保存设置」） ----------
        nrow = QHBoxLayout()
        nrow.addWidget(QLabel("成品命名："))
        self.lbl_naming = QLabel()
        self.lbl_naming.setObjectName("InlineTip")
        self.lbl_naming.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        b_naming = QPushButton("🏷 自定义规则…")
        b_naming.setObjectName("GhostBtn")
        b_naming.clicked.connect(self._edit_naming)
        nrow.addWidget(self.lbl_naming, 1)
        nrow.addWidget(b_naming)
        lay.addLayout(nrow)
        self._refresh_naming()

        # ---------- 视频预览框大小（UI 偏好，存 ui_state.json，拖动即时生效、不用重启） ----------
        prow = QHBoxLayout()
        prow.addWidget(QLabel("视频预览框大小："))
        self.sl_preview = QSlider(Qt.Orientation.Horizontal)
        self.sl_preview.setRange(VideoPlayerDialog.SCALE_MIN, VideoPlayerDialog.SCALE_MAX)
        self.sl_preview.setSingleStep(5)
        self.sl_preview.setPageStep(10)
        self.sl_preview.setFixedWidth(200)
        self.lbl_preview_scale = QLabel()          # 先连信号、再 setValue，保证初始不落盘
        b_pv_reset = QPushButton("恢复默认")
        b_pv_reset.setObjectName("GhostBtn")
        b_pv_reset.setToolTip("回到 100%（即已整体缩小 30% 后的默认基准大小）")
        b_pv_reset.clicked.connect(
            lambda: self.sl_preview.setValue(VideoPlayerDialog.SCALE_DEFAULT))
        self.sl_preview.valueChanged.connect(self._preview_scale_changed)
        self.sl_preview.setValue(self._load_preview_scale())
        prow.addWidget(self.sl_preview)
        prow.addWidget(self.lbl_preview_scale)
        prow.addWidget(b_pv_reset)
        prow.addWidget(QLabel("（向左更小、向右更大；相对默认缩小 30% 后的基准等比缩放，下次打开播放器即生效）"))
        prow.addStretch(1)
        lay.addLayout(prow)

        # ---------- 输出目录：默认隐藏，Alt+W（Mac ⌘+W）口令解锁后才出现（不暴露入口） ----------
        # 口令同时放出导航里的「🔌 接口管理」页：一个口令管全部维护人入口
        self._dirs_unlocked = False
        self.dirs_box = QWidget()
        dbox = QVBoxLayout(self.dirs_box)
        dbox.setContentsMargins(0, 0, 0, 0)
        dbox.setSpacing(8)
        dbox.addWidget(QLabel("输出目录（留空＝用默认，修改保存后重启生效）："))
        self._dir_edits = {}
        for key, label, cur, dft in (
                ("output", "视频输出", DOWNLOAD_DIR, str(RUNTIME_DIR / "outputs")),
                ("export", "模板/导出", EXPORT_DIR, str(RUNTIME_DIR / "exports")),
                ("material", "素材目录", MATERIAL_DIR, str(RUNTIME_DIR / "material"))):
            r = QHBoxLayout()
            r.addWidget(QLabel(label + "："))
            ed = QLineEdit("" if _same_path(cur, dft) else cur)
            ed.setPlaceholderText("默认：" + dft)
            ed.setProperty("default", dft)
            b = QPushButton("浏览…")
            b.setObjectName("GhostBtn")
            b.clicked.connect(lambda _=False, e=ed: self._pick_dir(e))
            r.addWidget(ed, 1)
            r.addWidget(b)
            dbox.addLayout(r)
            self._dir_edits[key] = ed
        self.dirs_box.setVisible(False)
        lay.addWidget(self.dirs_box)

        # 线路部署与 AI 检测/翻译接口已搬到「接口管理」页（gui/pages_api.py）；
        # 接口地址本身就是访问凭证，日常两个界面都不露：普通使用者只走
        # 「线路包 / 配置包」分发，编辑入口对维护人保留（Alt+W 解锁后跳页）。
        # 锁着的时候给一句人话：告诉使用者线路从哪来，但不暴露解锁入口
        tip = QLabel("线路与各接口地址由维护人管理：换线路时找维护人要一份线路包（或配置包），\n"
                     "用下方「📥 导入线路」或「📥 一键导入配置」导入即可，不用手敲任何地址。")
        tip.setStyleSheet("color:#6B7280;")
        lay.addWidget(tip)

        # ---------- 配置迁移（加密包）：接口/命名/字段/SMB/溯源/产品风控图片一键搬家 ----------
        lay.addWidget(QLabel(
            "配置迁移（加密包：线路与各接口、SMB/溯源账密、命名规则、任务界面字段、"
            "产品与规范卡（含产品图片）、风控政策全部在内，换新机器 / 交给同事时用）："))
        mrow = QHBoxLayout()
        b_pkg_exp = QPushButton("📤 导出配置包")
        b_pkg_exp.setObjectName("GhostBtn")
        b_pkg_exp.setToolTip(
            "把本机全部配置与产品/风控业务数据（含产品图片）加密成一个 .aigccfg 文件\n"
            "（默认存到桌面）；包内容清单见上方说明。\n"
            "文件用软件内置口令加密，外人拿到看不到接口地址和 key；\n"
            "里面含凭证和业务数据，只发给信得过的同事")
        b_pkg_exp.clicked.connect(self._export_pkg)
        b_pkg_imp = QPushButton("📥 一键导入配置")
        b_pkg_imp.setObjectName("GhostBtn")
        b_pkg_imp.setToolTip(
            "选一个同事导出的 .aigccfg 配置包，软件自动解密逐条目覆盖本机配置\n"
            "（导入前列清单让你确认；旧配置文件备份成 *.bak-时间戳；"
            "产品/风控表整表替换，任务列表不动）；导入后重启软件生效")
        b_pkg_imp.clicked.connect(self._import_pkg)
        mrow.addWidget(b_pkg_exp)
        mrow.addWidget(b_pkg_imp)
        mrow.addStretch(1)
        lay.addLayout(mrow)
        # 线路天天变：单独进出口的小包，只碰线路，其它配置不动
        lrow = QHBoxLayout()
        b_line_exp = QPushButton("🔗 导出线路")
        b_line_exp.setObjectName("GhostBtn")
        b_line_exp.setToolTip(
            "只导接口线路（名称+地址+并发），加密成一个 .aigcline 小文件\n"
            "（默认存桌面，文件名带日期如 线路_0924.aigcline）。\n"
            "线路地址天天换时用这个，不用动整套配置；文件名自带日期，发群里好认新旧")
        b_line_exp.clicked.connect(self._export_lines)
        b_line_imp = QPushButton("📥 导入线路")
        b_line_imp.setObjectName("GhostBtn")
        b_line_imp.setToolTip(
            "选一个同事发来的 .aigcline 线路包，只替换本机线路，\n"
            "其它配置（命名/字段/SMB/产品…）一个字不动；重启后生效\n"
            "（正在跑的任务不受影响）")
        b_line_imp.clicked.connect(self._import_lines)
        lrow.addWidget(b_line_exp)
        lrow.addWidget(b_line_imp)
        lrow.addStretch(1)
        lay.addLayout(lrow)

        # ---------- 保存：常驻底部 ----------
        # 只写姓名/输出目录；线路与各接口归「接口管理」页自己的保存按钮管，
        # 这边合并写回不碰那些段，不会把同事导进配置的线路弄丢。
        srow = QHBoxLayout()
        b_save = QPushButton("💾 保存设置")
        b_save.clicked.connect(self._save)
        srow.addWidget(b_save)
        srow.addStretch(1)
        lay.addLayout(srow)

        # 维护人入口：Alt+W（Mac ⌘+W）唤出口令框，验证通过才显示「输出目录」；
        # 仅当停在设置页时激活（show/hideEvent 开关），避免别处误触。
        self._sc_dirs = QShortcut(QKeySequence(MAINTAINER_SHORTCUT), self)
        self._sc_dirs.setContext(Qt.ShortcutContext.WindowShortcut)
        self._sc_dirs.activated.connect(self._summon_dirs)
        self._sc_dirs.setEnabled(False)

    def refresh(self):
        pass  # 不自动覆盖用户正在编辑的内容

    # ---------- 命名规则 ----------
    def _refresh_naming(self):
        """把当前生效的规则写成一句话：下载成品时就按它拼文件名"""
        self.lbl_naming.setText(f"{naming.describe()}　→　例如 {naming.preview()}")
        self.lbl_naming.setToolTip(
            "下载成品时的文件命名规则，点右侧「🏷 自定义规则…」可重新排列组合。\n"
            "这一项保存后立即生效（不用重启），且只影响之后新下载的视频。")

    def _edit_naming(self):
        from gui.dialogs_naming import NamingRuleDialog   # 只在打开时导入，减启动开销
        if NamingRuleDialog(self).exec():
            self._refresh_naming()

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
        val = int(val)
        app_state.set_value(VideoPlayerDialog.SCALE_KEY, val)
        self.lbl_preview_scale.setText(f"{val}%")

    # ---------- 维护人入口：口令解锁「输出目录」并放出导航里的「接口管理」 ----------
    def _summon_dirs(self):
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
                "配置文件旧值会备份成 *.bak-时间戳；产品/风控表整表替换；"
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
                                "旧配置已备份，产品/风控页切过去即是新数据；\n"
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
