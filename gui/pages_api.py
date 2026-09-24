"""
gui/pages_api.py —— 接口管理（维护人页：默认不进导航，设置页口令解锁后出现并跳入）

这里集中全软件所有「对外接口」的部署与状态：
- 线路部署（API 服务地址）：一个地址＝一个账号，调度按它们分负载
- 脚本 AI 检测接口：「🧐 口播规范检测」用的 OpenAI 兼容接口（可选）
- 机器翻译接口：火山引擎 MT，任务弹窗「提示词中文对照」用
- 素材提取接口：api_text/api_config.json 凭证（外部维护文件，只给状态与入口）

为什么整页藏起来：接口地址和 key 本身就是访问凭证，日常界面不露；
普通使用者走加密的「线路包 / 配置包」分发（设置页），只有维护人在设置页
按口令（Alt+W / Mac ⌘+W）验证通过，导航里才有这一项并直接跳进来。
改完点本页「💾 保存部署」写回 config.json，重启生效。
"""
import json
import os

from PySide6.QtCore import Qt, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QHeaderView, QMessageBox, QCheckBox, QApplication,
                               QAbstractItemView)

from core.config import CONFIG_JSON, ACCOUNTS, SCRIPT_CHECK, TRANSLATE
from core.api_client import health
from core.setup_wizard import _normalize_base
from gui.header import page_header
from utils.desktop_utils import open_path

# 线路表列号：末列是行内「打开接口」按钮（setCellWidget，不进 _rows/不写配置）
COL_NAME, COL_BASE, COL_CONC, COL_OPEN = range(4)

# 行高（px）：微软雅黑 13px 光行距就接近 26px，再叠上全局样式表 item 的
# 上下 padding，25px 默认行高会把地址文字上下各切一刀；36px 仍顶到边，
# 现按使用者体感再抬 25%。
ROW_H = 45

# 「🌐 打开选中」超过这个数量就先问一句：再多会把浏览器卡住、也看不过来
OPEN_MAX = 8


def _browser_url(base):
    """线路地址是给程序打的（带 /api/v1），浏览器要开的是根地址

    直接开 …/api/v1 只会看到一堆接口报错页；补上 http:// 前缀，否则浏览器
    会把“192.168.0.5:7860”当关键词去搜。"""
    url = str(base or "").strip().rstrip("/")
    if url.endswith("/api/v1"):
        url = url[:-len("/api/v1")]
    if "://" not in url:
        url = "http://" + url
    return url


def dedupe_browser_urls(bases):
    """一组接口地址 -> 要去浏览器里打开的链接（先去掉空行，同一地址只留一个）

    保留输入顺序：“选中几行开几个标签”得跟人看到的顺序对得上，
    否则同时看七个 Gradio 页面时分不清谁是谁。空地址要先拦下再交给
    `_browser_url`（它给空串会拼出个“http://”，不拦就会真去开一个废标签）。"""
    urls = []
    for base in bases:
        if not str(base or "").strip():
            continue
        url = _browser_url(base)
        if url and url not in urls:
            urls.append(url)
    return urls


class _CheckWorker(QThread):
    """后台连通性检测：逐个请求在线程里跑，不阻塞界面"""
    done = Signal(list)

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def run(self):
        results = []
        for a in self.rows:
            try:
                health(a["base"])
                results.append(f"🟢 {a['name']} 连通正常")
            except Exception as e:
                results.append(f"🔴 {a['name']} 无法连通：{type(e).__name__}")
        self.done.emit(results)


def parse_lines(text):
    """剪贴板文本 -> 线路列表，供「粘贴导入」用。

    接受三种写法：「📋 复制线路」的输出 {"accounts": [...]}、纯数组 [...]、
    以及完整的 config.json（同样取其中的 accounts 字段）。
    接口地址已归一化补上 /api/v1 后缀；并发数非法时回到 1。
    解析不出来返回 None，调用方据此保持表格原样不动。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    if isinstance(data, dict):
        data = data.get("accounts")
    if not isinstance(data, list):
        return None

    rows = []
    for item in data:
        if isinstance(item, str):                      # 允许只贴一串地址
            item = {"base": item}
        if not isinstance(item, dict):
            continue
        base = str(item.get("base", "")).strip()
        if not base:
            continue
        try:
            conc = int(float(item.get("concurrency", 1) or 1))
        except (TypeError, ValueError):
            conc = 1
        name = str(item.get("name", "")).strip() or f"acc{len(rows) + 1}"
        rows.append({"name": name, "base": _normalize_base(base),
                     "concurrency": max(conc, 1)})
    return rows or None


class ApiManagerPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)

        head = page_header("接口管理", "线路部署与各大接口的配置（维护人）—— 改完点「💾 保存部署」，重启生效",
                           icon="🔌")
        head.setToolTip(f"配置文件：{CONFIG_JSON}")
        lay.addWidget(head)

        # ---------- 接口总览：一眼看清哪些配了、哪些没配 ----------
        self.lbl_status = QLabel()
        self.lbl_status.setObjectName("InlineTip")
        self.lbl_status.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.lbl_status)

        # ---------- 线路部署（原设置页「API 服务地址」区整体搬来） ----------
        lay.addWidget(QLabel("线路部署（一个地址 = 一个账号，双击单元格可编辑；"
                             "拖动/Ctrl 可多选行，删除只弹一次确认；点行末「🌐 打开」看"
                             "单条线路，要一次对比几条就用下方「🌐 打开选中」）："))
        self.acc_table = QTableWidget(0, 4)
        self.acc_table.setHorizontalHeaderLabels(["账号名", "接口地址", "并发数", "打开"])
        self.acc_table.horizontalHeader().setSectionResizeMode(COL_BASE,
                                                               QHeaderView.ResizeMode.Stretch)
        self.acc_table.setColumnWidth(COL_NAME, 110)
        self.acc_table.setColumnWidth(COL_CONC, 70)
        self.acc_table.setColumnWidth(COL_OPEN, 86)
        # 行高见 ROW_H；本表再把 item 上下 padding 收到 3px，给文字留出富余
        self.acc_table.verticalHeader().setDefaultSectionSize(ROW_H)
        self.acc_table.setStyleSheet(
            "QTableWidget::item { padding: 3px 8px; border-bottom: 1px solid #EFF0F1; }"
            "QTableWidget::item:selected { background: #EAF1FF; color: #1F2329; }")
        # 整行选中 + 连续多选：鼠标按住拖就能圈好几行，配合批量删除
        self.acc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.acc_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self.acc_table, 1)

        bar = QHBoxLayout()
        b_add = QPushButton("＋ 添加地址")
        b_add.setObjectName("GhostBtn")
        b_add.clicked.connect(lambda: self._add_row())
        b_del = QPushButton("🗑 删除所选行")
        b_del.setObjectName("GhostBtn")
        b_del.setToolTip("可先用鼠标拖动多选几行；删除只弹一次确认，一次删掉全部选中行")
        b_del.clicked.connect(self._del_rows)
        b_open_sel = QPushButton("🌐 打开选中")
        b_open_sel.setObjectName("GhostBtn")
        b_open_sel.setToolTip(
            "一次用浏览器打开选中这几条线路（没选中则问一句后开全部），\n"
            "方便横向对比哪台机器最忙；地址相同的只开一个标签\n"
            f"超过 {OPEN_MAX} 个标签时会先问一句，免得把浏览器卡住")
        b_open_sel.clicked.connect(self._open_selected)
        b_check = QPushButton("🔌 测试连通性")
        b_check.setObjectName("GhostBtn")
        b_check.clicked.connect(self._check)
        self.b_check = b_check
        b_copy = QPushButton("📋 复制线路")
        b_copy.setObjectName("GhostBtn")
        b_copy.setToolTip("把下面的线路列表复制成一段文本，微信/飞书发给同事，"
                          "对方点「📥 粘贴导入」即可，不必每人手敲 7 个地址")
        b_copy.clicked.connect(self._copy_lines)
        b_paste = QPushButton("📥 粘贴导入")
        b_paste.setObjectName("GhostBtn")
        b_paste.setToolTip("读取剪贴板里的线路配置覆盖当前表格（改完记得保存）；"
                           "支持「📋 复制线路」的输出、纯地址数组、或整份 config.json")
        b_paste.clicked.connect(self._paste_lines)
        bar.addWidget(b_add)
        bar.addWidget(b_del)
        bar.addWidget(b_open_sel)
        bar.addWidget(b_check)
        bar.addWidget(b_copy)
        bar.addWidget(b_paste)
        bar.addStretch(1)
        lay.addLayout(bar)

        for a in ACCOUNTS:
            self._add_row(a["name"], a["base"], a["concurrency"])
        if self.acc_table.rowCount() == 0:
            self._add_row()
        self._checker = None

        # ---------- 脚本 AI 检测接口（规范卡口播检测用，可留空） ----------
        lay.addWidget(QLabel("脚本 AI 检测接口（可选 —— 「🧐 口播规范检测」的 AI 智能检测用，不配置也能用本地规则检测）："))
        crow = QHBoxLayout()
        self.ck_ai = QCheckBox("启用")
        self.ck_ai.setChecked(bool(SCRIPT_CHECK.get("enabled")))
        crow.addWidget(self.ck_ai)
        crow.addWidget(QLabel("地址"))
        self.ed_ai_url = QLineEdit(SCRIPT_CHECK.get("url", ""))
        self.ed_ai_url.setPlaceholderText("https://.../v1/chat/completions（OpenAI 兼容格式）")
        crow.addWidget(self.ed_ai_url, 2)
        crow.addWidget(QLabel("Key"))
        self.ed_ai_key = QLineEdit(SCRIPT_CHECK.get("api_key", ""))
        self.ed_ai_key.setEchoMode(QLineEdit.EchoMode.Password)
        crow.addWidget(self.ed_ai_key, 1)
        crow.addWidget(QLabel("模型"))
        self.ed_ai_model = QLineEdit(SCRIPT_CHECK.get("model", ""))
        self.ed_ai_model.setPlaceholderText("gpt-4o-mini")
        self.ed_ai_model.setFixedWidth(140)
        crow.addWidget(self.ed_ai_model)
        lay.addLayout(crow)

        # ---------- 机器翻译接口（火山引擎 MT，「提示词中文对照」用） ----------
        lay.addWidget(QLabel("机器翻译接口（火山引擎文本翻译 —— 任务弹窗「提示词中文对照」用；"
                             "也可留空，留空时界面点翻译会明确提示未配置）："))
        trow = QHBoxLayout()
        trow.addWidget(QLabel("AK"))
        self.ed_tr_ak = QLineEdit(str(TRANSLATE.get("ak") or ""))
        self.ed_tr_ak.setPlaceholderText("Volcano Engine Access Key")
        trow.addWidget(self.ed_tr_ak, 1)
        trow.addWidget(QLabel("SK"))
        self.ed_tr_sk = QLineEdit(str(TRANSLATE.get("sk") or ""))
        self.ed_tr_sk.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_tr_sk.setPlaceholderText("Secret Key")
        trow.addWidget(self.ed_tr_sk, 1)
        lay.addLayout(trow)
        tr2 = QHBoxLayout()
        b_tr_test = QPushButton("🌐 测试翻译")
        b_tr_test.setObjectName("GhostBtn")
        b_tr_test.setToolTip("用上面填的 AK/SK 现场翻一句，验证密钥对不对（不用先保存）")
        b_tr_test.clicked.connect(self._test_translate)
        tr2.addWidget(b_tr_test)
        tr2.addStretch(1)
        lay.addLayout(tr2)

        # ---------- 保存：只写接口相关段，姓名/命名规则仍在设置页 ----------
        srow = QHBoxLayout()
        b_save = QPushButton("💾 保存部署")
        b_save.clicked.connect(self._save)
        srow.addWidget(b_save)
        b_opendir = QPushButton("📂 提取接口凭证目录")
        b_opendir.setObjectName("GhostBtn")
        b_opendir.setToolTip("打开 api_text/：素材提取接口的凭证与直链白名单是外部维护的\n"
                             "文本文件（api_config.json / image.txt / video.txt），改完即时生效")
        b_opendir.clicked.connect(self._open_api_text)
        srow.addWidget(b_opendir)
        srow.addStretch(1)
        lay.addLayout(srow)

        self._update_status()

    # ================= 状态总览 =================
    def refresh(self):
        self._update_status()      # 2 秒定时刷只重画状态行，不碰正在编辑的控件

    def _update_status(self):
        lines = self._rows()
        ai = "已启用" if (self.ck_ai.isChecked() and self.ed_ai_url.text().strip()) \
            else ("地址没填" if self.ck_ai.isChecked() else "未启用（本地规则照常可用）")
        tr = "已配置" if (self.ed_tr_ak.text().strip() and self.ed_tr_sk.text().strip()) \
            else ("未配置（弹窗点翻译会提示）" if not TRANSLATE.get("ak")
                  else "由本机 config_local 兜底（这里填了会覆盖）")
        try:
            from core.config import CONFIG_DIR
            api_cfg = (str(CONFIG_DIR) + "/api_text/api_config.json")
            ex = "已配置" if os.path.isfile(api_cfg) else "缺 api_config.json"
        except Exception:
            ex = "未知"
        self.lbl_status.setText(
            f"生成线路 <b>{len(lines)}</b> 条　·　脚本 AI 检测：{ai}　·　"
            f"机器翻译：{tr}　·　素材提取接口：{ex}")

    def _open_api_text(self):
        from core.config import CONFIG_DIR
        d = f"{CONFIG_DIR}/api_text"
        try:
            os.makedirs(d, exist_ok=True)
            open_path(d)
        except Exception as e:
            QMessageBox.warning(self, "打不开", str(e))

    def _test_translate(self):
        """拿当前输入框里的 AK/SK 现场翻一句：错了马上知道，不用保存重启再试"""
        ak = self.ed_tr_ak.text().strip()
        sk = self.ed_tr_sk.text().strip()
        if not (ak and sk):
            QMessageBox.warning(self, "还没填", "先把翻译的 AK / SK 都填上再测")
            return
        from core import translate
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            out = translate.probe(ak, sk)
        except Exception as e:
            out = None
            err = f"{type(e).__name__}: {e}"
        finally:
            QApplication.restoreOverrideCursor()
        if out:
            QMessageBox.information(self, "翻译成功", f"hello world → {out}")
        else:
            QMessageBox.warning(self, "翻译失败", f"密钥或网络有问题：\n{err}")

    # ================= 外部刷新（设置页导入配置/线路包后调用） =================
    def reload_from_disk(self):
        """按盘上的 config.json 重建线路表与接口字段

        不能吃启动缓存 ACCOUNTS/SCRIPT_CHECK（导入刚写完盘它们还是旧值），
        也不能往表里追加（会跟启动加的那批叠成重复行）——清空重建。"""
        try:
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        except Exception:
            return
        self.acc_table.setRowCount(0)
        for a in (data.get("accounts") or []):
            self._add_row(a.get("name", ""),
                          _normalize_base(a.get("base", "")),
                          a.get("concurrency", 1))
        if self.acc_table.rowCount() == 0:
            self._add_row()
        sc = data.get("script_check") or {}
        self.ck_ai.setChecked(bool(sc.get("enabled")))
        self.ed_ai_url.setText(str(sc.get("url", "")))
        self.ed_ai_key.setText(str(sc.get("api_key", "")))
        self.ed_ai_model.setText(str(sc.get("model", "")))
        tr = data.get("translate") or {}
        self.ed_tr_ak.setText(str(tr.get("ak", "")))
        self.ed_tr_sk.setText(str(tr.get("sk", "")))
        self._update_status()

    # ================= 线路表（原设置页整套搬来，行为不变） =================
    def _add_row(self, name="acc1", base="", conc=1):
        r = self.acc_table.rowCount()
        self.acc_table.insertRow(r)
        self.acc_table.setItem(r, COL_NAME, QTableWidgetItem(name))
        self.acc_table.setItem(r, COL_BASE, QTableWidgetItem(base))
        self.acc_table.setItem(r, COL_CONC, QTableWidgetItem(str(conc)))
        self._set_open_btn(r)

    def _set_open_btn(self, r):
        """每行一个「🌐 打开」：用默认浏览器看这条线路（看服务健不健康、有没有重启）

        按钮不记行号也不记地址：行会被删、地址会被双击改，两个都可能在创建后变，
        所以点击时现查自己在哪一行、那一行的地址是什么。"""
        btn = QPushButton("🌐 打开")
        btn.setObjectName("GhostBtn")
        btn.setToolTip("用浏览器打开本行接口（自动去掉地址末尾的 /api/v1）")
        btn.setStyleSheet("padding: 2px 6px;")      # 行内用紧凑内边距，不被全局按钮样式顶大
        btn.setFixedHeight(30)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Tab 不该跳进行里抢焦点
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _=False, b=btn: self._open_api(b))
        self.acc_table.setCellWidget(r, COL_OPEN, btn)
        return btn

    def _selected_rows(self):
        """选中行号（升序去重）：整行拖选与逐格点选都算

        `selectedRows()` 要求整行（全部列）都选中才计数，而末列是按钮（只有 widget
        没有 item）——选区没铺满列时它就返回空，表现为“点了按钮没反应”。
        批删与批量打开共用这个口径，不各写一遍。"""
        sel = self.acc_table.selectionModel()
        return sorted({idx.row() for idx in sel.selectedRows()}
                      | {idx.row() for idx in sel.selectedIndexes()})

    def _row_base(self, r):
        """某行当前填的接口地址（没填返回空串；行越界也不抛）"""
        item = self.acc_table.item(r, COL_BASE)
        return item.text().strip() if (r >= 0 and item) else ""

    def _open_api(self, btn):
        """按按钮所在行取当前地址开浏览器（地址改过就开改后的，不是创建时那一份）"""
        r = -1
        for i in range(self.acc_table.rowCount()):
            if self.acc_table.cellWidget(i, COL_OPEN) is btn:
                r = i
                break
        base = self._row_base(r)
        if not base:
            QMessageBox.information(self, "提示", "这一行还没填接口地址")
            return
        url = _browser_url(base)
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "打不开", f"系统没能打开浏览器：\n{url}")

    def _open_selected(self):
        """「🌐 打开选中」：一次把选中几条线路全开成浏览器标签

        行内按钮只能一条条点，线路多了对比“谁最忙”要点七下；没选中时
        不静默也不猜：问一句“要开全部 N 条吗”，免得误以为坏了。"""
        rows = self._selected_rows()
        if not rows:
            total = self.acc_table.rowCount()
            if not total:
                QMessageBox.information(self, "提示", "线路表是空的，先添加接口地址")
                return
            if QMessageBox.question(
                    self, "打开全部线路",
                    f"没有选中行，要打开表里全部 {total} 条线路吗？\n"
                    "（会在浏览器里一次弹多个标签页）") \
                    != QMessageBox.StandardButton.Yes:
                return
            rows = list(range(total))
        urls = dedupe_browser_urls(self._row_base(r) for r in rows)
        if not urls:
            QMessageBox.information(self, "提示", "这些行都还没填接口地址")
            return
        if len(urls) > OPEN_MAX and QMessageBox.question(
                self, "一次开太多标签",
                f"要一次打开 {len(urls)} 个页面（上限 {OPEN_MAX} 个），\n"
                "浏览器可能明显卡顿。确定继续？") != QMessageBox.StandardButton.Yes:
            return
        fails = [u for u in urls if not QDesktopServices.openUrl(QUrl(u))]
        if fails:
            QMessageBox.warning(
                self, "部分没打开",
                f"已打开 {len(urls) - len(fails)} 个，这几个系统没能打开：\n"
                + "\n".join(fails[:5]))

    def _del_rows(self):
        """批量删除：选中几删几，一次确认全删（旧实现无确认且只能删当前一行）"""
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "提示", "先用鼠标点选/拖选要删的线路行")
            return
        names = "、".join(self.acc_table.item(r, COL_NAME).text() or f"第{r + 1}行"
                          for r in rows[:6]) + ("…" if len(rows) > 6 else "")
        if QMessageBox.question(
                self, "删除线路",
                f"确定删除选中的 {len(rows)} 条线路？（{names}）\n"
                "删除后仍需点「💾 保存部署」才写入配置。") \
                != QMessageBox.StandardButton.Yes:
            return
        for r in reversed(rows):              # 从大到小删，行号不位移
            self.acc_table.removeRow(r)

    def _rows(self):
        out = []
        for r in range(self.acc_table.rowCount()):
            name = self.acc_table.item(r, COL_NAME).text().strip()
            base = self.acc_table.item(r, COL_BASE).text().strip()
            try:
                conc = int(self.acc_table.item(r, COL_CONC).text())
            except (TypeError, ValueError):
                conc = 1
            if base:
                out.append({"name": name or f"acc{r + 1}",
                            "base": _normalize_base(base),
                            "concurrency": max(conc, 1)})
        return out

    # ================= 连通性检测 / 复制粘贴 =================
    def _check(self):
        rows = self._rows()
        if not rows:
            QMessageBox.warning(self, "提示", "请先填写接口地址")
            return
        if self._checker is not None and self._checker.isRunning():
            return
        self.b_check.setEnabled(False)
        self.b_check.setText("⏳ 检测中…")
        self._checker = _CheckWorker(rows)
        self._checker.done.connect(self._check_done)
        self._checker.start()

    def _check_done(self, results):
        self.b_check.setEnabled(True)
        self.b_check.setText("🔌 测试连通性")
        QMessageBox.information(self, "连通性检测", "\n".join(results))

    def _copy_lines(self):
        """线路列表 -> 剪贴板 JSON，同事粘贴导入即可，免去逐台手敲地址"""
        rows = self._rows()
        if not rows:
            QMessageBox.warning(self, "提示", "没有可复制的线路，请先填写接口地址")
            return
        QApplication.clipboard().setText(
            json.dumps({"accounts": rows}, ensure_ascii=False, indent=2))
        QMessageBox.information(
            self, "已复制",
            f"已复制 {len(rows)} 条线路到剪贴板，发给同事后在对方电脑\n"
            f"点「📥 粘贴导入」即可（导入后仍需点「💾 保存部署」并重启）。\n\n"
            "提醒：接口地址本身就是访问凭证，只发给内部同事。")

    def _paste_lines(self):
        """剪贴板 JSON -> 表格（覆盖前先确认，解析失败不碰现有内容）"""
        rows = parse_lines(QApplication.clipboard().text())
        if not rows:
            QMessageBox.warning(
                self, "格式不对",
                "剪贴板里不是可识别的线路配置。\n\n"
                "支持：本软件「📋 复制线路」的输出、纯接口地址数组、或整份 config.json")
            return
        if self.acc_table.rowCount():
            ret = QMessageBox.question(
                self, "导入线路",
                f"将用剪贴板里的 {len(rows)} 条线路替换当前 "
                f"{self.acc_table.rowCount()} 条，确定吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
        self.acc_table.setRowCount(0)
        for r in rows:
            self._add_row(r["name"], r["base"], r["concurrency"])
        QMessageBox.information(self, "已导入",
                                f"已导入 {len(rows)} 条线路，点「💾 保存部署」并重启生效")

    # ================= 保存 =================
    def _save(self):
        accounts = self._rows()
        if not accounts:
            QMessageBox.warning(self, "提示", "至少保留一个接口地址")
            return
        data = {}
        if CONFIG_JSON.exists():        # 读旧合并写回：不伤姓名/命名规则/smb 等其它段
            try:
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["accounts"] = accounts
        data["script_check"] = {
            "enabled": self.ck_ai.isChecked(),
            "url": self.ed_ai_url.text().strip(),
            "api_key": self.ed_ai_key.text().strip(),
            "model": self.ed_ai_model.text().strip(),
        }
        tr = dict(data.get("translate") or {})   # region/project 等键原样留着
        tr["ak"] = self.ed_tr_ak.text().strip()
        tr["sk"] = self.ed_tr_sk.text().strip()
        data["translate"] = tr
        try:
            tmp = CONFIG_JSON.with_name(CONFIG_JSON.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(CONFIG_JSON)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self._update_status()
        QMessageBox.information(self, "已保存",
                                "线路与各接口部署已写入 config.json，重启软件后生效。\n"
                                "要给同事分发，回设置页用「🔗 导出线路 / 📤 导出配置包」。")
