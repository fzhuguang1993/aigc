"""
gui/pages_api.py —— 接口管理（维护人页：默认不进导航，设置页口令解锁后出现并跳入）

这里集中全软件所有「对外接口」的部署与状态：
- 线路部署（API 服务地址）：一个地址＝一个账号，调度按它们分负载
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
                               QHeaderView, QMessageBox, QApplication,
                               QAbstractItemView, QCheckBox, QFormLayout, QPlainTextEdit,
                               QScrollArea, QFrame, QFileDialog)

from core.config import CONFIG_JSON, ACCOUNTS, TRANSLATE
from core.api_client import health
from core.setup_wizard import _normalize_base
from gui.header import page_header, Card
from utils.desktop_utils import open_path
from gui.theme import tokenize

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
        # 整页套一层滚动区：本页卡片多（线路/翻译/Whisper/豆包/纠错+词库），内容比
        # 屏幕高时旧版直接被 QStackedWidget 裁掉、下面的卡片点不到。与设置页同款：
        # 外层不留边距，滚动的 inner 自带内边距，页面随内容变长而可滚。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(24, 14, 24, 24)
        lay.setSpacing(12)

        head = page_header("接口管理", "线路部署与各大接口的配置（维护人）—— 改完点「💾 保存部署」，重启生效",
                           icon="🔌")
        head.setToolTip(f"配置文件：{CONFIG_JSON}")
        lay.addWidget(head)

        # ---------- 接口总览：一眼看清哪些配了、哪些没配（收进白卡）----------
        ov = Card(margins=(16, 10, 16, 10))
        ovh = QHBoxLayout()
        self.lbl_status = QLabel()
        self.lbl_status.setObjectName("InlineTip")
        self.lbl_status.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_status.setWordWrap(True)
        b_re = QPushButton("🔄 重新检查")
        b_re.setObjectName("GhostBtn")
        b_re.setToolTip("重算本地就绪（任务表/模型/豆包/纠错/词库）并重新检测线路连通性")
        b_re.clicked.connect(self._recheck)
        ovh.addWidget(self.lbl_status, 1)
        ovh.addWidget(b_re)
        ov.v.addLayout(ovh)
        lay.addWidget(ov)

        # ---------- 线路部署（原设置页「API 服务地址」区整体搬来，收进白卡）----------
        line_card = Card(margins=(16, 14, 16, 12))
        line_card.v.setSpacing(10)
        line_card.v.addWidget(QLabel("线路部署（一个地址 = 一个账号，双击单元格可编辑；"
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
        # 套了滚动区后表格不再有“ extra 空间”自动撑高，给个下限免得塌成一行
        self.acc_table.setMinimumHeight(180)
        self.acc_table.setStyleSheet(
            tokenize("QTableWidget::item { padding: 3px 8px; border-bottom: 1px solid #EFF0F1; }"
            "QTableWidget::item:selected { background: #EAF1FF; color: #1F2329; }"))
        # 整行选中 + 连续多选：鼠标按住拖就能圈好几行，配合批量删除
        self.acc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.acc_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        line_card.v.addWidget(self.acc_table, 1)

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
        line_card.v.addLayout(bar)
        lay.addWidget(line_card, 1)

        for a in ACCOUNTS:
            self._add_row(a["name"], a["base"], a["concurrency"])
        if self.acc_table.rowCount() == 0:
            self._add_row()
        self._checker = None

        # ---------- 机器翻译接口（火山引擎 MT，「提示词中文对照」用） ----------
        tr_card = Card(margins=(16, 14, 16, 12))
        tr_card.v.setSpacing(10)
        tr_card.v.addWidget(QLabel("机器翻译接口（火山引擎文本翻译 —— 任务弹窗「提示词中文对照」用；"
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
        tr_card.v.addLayout(trow)
        tr2 = QHBoxLayout()
        b_tr_test = QPushButton("🌐 测试翻译")
        b_tr_test.setObjectName("GhostBtn")
        b_tr_test.setToolTip("用上面填的 AK/SK 现场翻一句，验证密钥对不对（不用先保存）")
        b_tr_test.clicked.connect(self._test_translate)
        tr2.addWidget(b_tr_test)
        tr2.addStretch(1)
        tr_card.v.addLayout(tr2)
        lay.addWidget(tr_card)

        # ---------- 语音识别/纠错：模型下载 + 豆包 + DeepSeek + 词库（保存即生效） ----------
        self._build_asr_cards(lay)

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
        tr = "已配置" if (self.ed_tr_ak.text().strip() and self.ed_tr_sk.text().strip()) \
            else ("未配置（弹窗点翻译会提示）" if not TRANSLATE.get("ak")
                  else "由本机 config_local 兜底（这里填了会覆盖）")
        try:
            from core.config import CONFIG_DIR
            api_cfg = (str(CONFIG_DIR) + "/api_text/api_config.json")
            ex = "已配置" if os.path.isfile(api_cfg) else "缺 api_config.json"
        except Exception:
            ex = "未知"
        parts = [
            f"生成线路 <b>{len(lines)}</b> 条",
            f"机器翻译：{tr}",
            f"素材提取接口：{ex}",
            self._chk_task_excel(),
            self._chk_whisper(),
            self._chk_doubao(),
            self._chk_asrfix(),
        ]
        self._refresh_model_labels()
        self.lbl_status.setText("　·　".join(parts))

    # ================= 配置检查：各项就绪判定（本地即时） =================
    @staticmethod
    def _chk_task_excel():
        try:
            from core.config import EXCEL_PATH
            if os.path.isfile(EXCEL_PATH):
                return "任务表：✅ 存在"
            return f"任务表：⚠ 未生成（{EXCEL_PATH}）"
        except Exception:
            return "任务表：未知"

    @staticmethod
    def _chk_doubao():
        try:
            from core.config import doubao_vision_ready
            return "豆包 Vision：" + ("✅ 已配置" if doubao_vision_ready() else "⚠ 未配置")
        except Exception:
            return "豆包 Vision：未知"

    @staticmethod
    def _chk_asrfix():
        try:
            from core.config import asr_fix_ready, asr_glossary_load
            n = len(asr_glossary_load())
            s = "✅ 已配置" if asr_fix_ready() else "⚠ 未配置"
            return f"语音纠错(DeepSeek)：{s}　·　词库 {n} 词"
        except Exception:
            return "语音纠错：未知"

    @staticmethod
    def _chk_whisper():
        try:
            from video_text_tools.asr import transcribe as tr
            segs = " ".join(("✅" if tr.model_ready(s) else "⚠") + s for s in tr.WHISPER_SIZES)
            best = tr.best_ready_size()
            tail = f"（识别优先用 {best}）" if best else "（尚无就绪模型，请下载）"
            return "Whisper：" + segs + tail
        except Exception:
            return "Whisper：未知"

    def _refresh_model_labels(self):
        if not getattr(self, "_model_lbls", None):
            return
        try:
            from video_text_tools.asr import transcribe as tr
        except Exception:
            return
        for size, (lbl, btn) in self._model_lbls.items():
            try:
                ready = tr.model_ready(size)
            except Exception:
                ready = False
            lbl.setText("已就绪 ✅" if ready else "未下载")
            btn.setEnabled(not ready)

    def _recheck(self):
        """重新检查：本地就绪即时重算 + 起后台线路连通检测。"""
        self._update_status()
        self._check()

    # ================= 语音模型 / 豆包 / DeepSeek / 词库 三卡 =================
    def _build_asr_cards(self, lay):
        from core.config import (doubao_vision_config, asr_fix_config, asr_glossary_load)
        self._dl_worker = None

        tip = QLabel("以下“语音相关”配置保存即生效、无需重启（上方线路/翻译仍需“保存部署”后重启）。")
        tip.setObjectName("InlineTip")
        lay.addWidget(tip)

        # ---- 卡 1：Whisper 模型下载 ----
        m_card = Card(margins=(16, 14, 16, 12))
        m_card.v.setSpacing(8)
        m_card.v.addWidget(QLabel("语音识别模型（Whisper，只下不打包；实时识别会自动优先用已就绪里最好的档）："))
        from video_text_tools.asr import transcribe as tr
        self._model_lbls = {}
        for size in tr.WHISPER_SIZES:              # medium, small, base, tiny
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{size}："))
            lbl = QLabel()
            btn = QPushButton("⬇ 下载")
            btn.setObjectName("GhostBtn")
            btn.clicked.connect(lambda _=False, s=size: self._download_whisper(s))
            row.addWidget(lbl, 1)
            row.addWidget(btn)
            m_card.v.addLayout(row)
            self._model_lbls[size] = (lbl, btn)
        mfoot = QHBoxLayout()
        self.ck_mirror = QCheckBox("用国内镜像(hf-mirror)")
        b_openm = QPushButton("📂 模型目录")
        b_openm.setObjectName("GhostBtn")
        b_openm.clicked.connect(self._open_models_dir)
        mfoot.addWidget(self.ck_mirror)
        mfoot.addWidget(b_openm)
        mfoot.addStretch(1)
        m_card.v.addLayout(mfoot)
        lay.addWidget(m_card)

        # ---- 卡 2：豆包 Vision ----
        d_card = Card(margins=(16, 14, 16, 12))
        d_card.v.setSpacing(8)
        d_card.v.addWidget(QLabel("豆包 Vision（爆款拆解画面分析必需；api_key 按姓名加密落盘）："))
        df = QFormLayout()
        df.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ed_d_key = QLineEdit()
        self.ed_d_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_d_key.setPlaceholderText("API Key（留空=不改动已存的）")
        self.ed_d_ep = QLineEdit()
        self.ed_d_ep.setPlaceholderText("推理接入点 ID，形如 ep-xxxx")
        _dc = doubao_vision_config()
        self.ed_d_ep.setText(_dc.get("endpoint") or "")
        self.ed_d_base = QLineEdit(_dc.get("base_url") or "https://ark.cn-beijing.volces.com/api/v3")
        b_ds = QPushButton("💾 保存豆包")
        b_ds.setObjectName("GhostBtn")
        b_ds.clicked.connect(self._save_doubao_page)
        df.addRow("API Key：", self.ed_d_key)
        df.addRow("端点 ID：", self.ed_d_ep)
        df.addRow("Base URL：", self.ed_d_base)
        df.addRow("", b_ds)
        d_card.v.addLayout(df)
        lay.addWidget(d_card)

        # ---- 卡 3：语音纠错（DeepSeek）+ 词库 ----
        f_card = Card(margins=(16, 14, 16, 12))
        f_card.v.setSpacing(8)
        f_card.v.addWidget(QLabel("✨ 语音纠错（OpenAI 兼容 chat 端点，如 DeepSeek；识别后按语义改同音字）："))
        ff = QFormLayout()
        ff.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ed_fix_key = QLineEdit()
        self.ed_fix_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_fix_key.setPlaceholderText("API Key（留空=不改动已存的）")
        _fc = asr_fix_config()
        self.ed_fix_model = QLineEdit(_fc.get("model") or "deepseek-chat")
        self.ed_fix_base = QLineEdit(_fc.get("base_url") or "https://api.deepseek.com/v1")
        b_fs = QPushButton("💾 保存纠错配置")
        b_fs.setObjectName("GhostBtn")
        b_fs.clicked.connect(self._save_asr_fix_page)
        b_ft = QPushButton("🌐 测试")
        b_ft.setObjectName("GhostBtn")
        b_ft.setToolTip("用当前填写的 key/model/base 现场纠正一句，验证连通（不用先保存）")
        b_ft.clicked.connect(self._test_asr_fix)
        fbtns = QHBoxLayout()
        fbtns.addWidget(b_fs)
        fbtns.addWidget(b_ft)
        fbtns.addStretch(1)
        ff.addRow("API Key：", self.ed_fix_key)
        ff.addRow("Model：", self.ed_fix_model)
        ff.addRow("Base URL：", self.ed_fix_base)
        ff.addRow("", fbtns)
        f_card.v.addLayout(ff)
        f_card.v.addWidget(QLabel("领域词库（一行一个正确写法；既喂实时识别少写同音字，又作为纠错时的正确词清单。"
                             "词多时用右侧「导入 / 导出」走 Excel / CSV / JSON 文件维护）："))
        self.ed_gloss = QPlainTextEdit()
        self.ed_gloss.setPlaceholderText("一行一个词，例如：\n钙片\n骨密度\n骨质疏松\n"
                                         "（也可点右侧从 Excel/CSV/JSON 文件导入）")
        self.ed_gloss.setMinimumHeight(120)
        self.ed_gloss.setPlainText("\n".join(asr_glossary_load()))
        f_card.v.addWidget(self.ed_gloss)
        b_gs = QPushButton("💾 保存词库")
        b_gs.setObjectName("GhostBtn")
        b_gs.clicked.connect(self._save_glossary)
        b_gi = QPushButton("📥 从 Excel/CSV/JSON 导入")
        b_gi.setObjectName("GhostBtn")
        b_gi.setToolTip("选一个词表文件读入（Excel/CSV 逐格取词、JSON 取字符串列表），"
                        "合并去重后填进上方文本框，确认无误再点「保存词库」生效")
        b_gi.clicked.connect(self._import_glossary)
        b_ge = QPushButton("📤 导出为文件")
        b_ge.setObjectName("GhostBtn")
        b_ge.setToolTip("把当前词库另存为 .xlsx / .csv / .json（单列、逐行一个词），方便用 Excel 批量编辑")
        b_ge.clicked.connect(self._export_glossary)
        gsr = QHBoxLayout()
        gsr.addWidget(b_gs)
        gsr.addWidget(b_gi)
        gsr.addWidget(b_ge)
        gsr.addStretch(1)
        f_card.v.addLayout(gsr)
        lay.addWidget(f_card)
        self._refresh_model_labels()

    # ---- 模型下载（后台 ToolWorker，进度走悬浮球） ----
    def _download_whisper(self, size):
        from video_text_tools.asr import transcribe as tr
        from core.config import MODELS_DIR
        from gui.speed_ball import show_speed_ball
        from gui.tool_panels import ToolWorker
        if getattr(self, "_dl_worker", None) is not None and self._dl_worker.isRunning():
            QMessageBox.information(self, "提示", "已有一个下载在进行，请等它结束")
            return
        mirror = self.ck_mirror.isChecked()
        show_speed_ball(self, MODELS_DIR, f"下载「{size}」模型")

        def fn(log, progress, should_stop):
            try:
                import faster_whisper  # noqa: F401
            except ImportError:
                from video_text_tools.asr.types import DepMissing
                raise DepMissing("缺少 faster-whisper，无法下载模型")
            tr.download_model_auto(size, mirror=mirror, log=log,
                                   on_progress=lambda p: progress(max(p, 0), 100, ""))
            return size

        self._dl_worker = ToolWorker(fn, self)
        self._dl_worker.done.connect(self._on_whisper_done)
        self._dl_worker.start()

    def _on_whisper_done(self, res):
        from gui.speed_ball import hide_speed_ball
        hide_speed_ball()
        self._dl_worker = None
        if isinstance(res, Exception):
            QMessageBox.warning(self, "下载失败", str(res))
        else:
            QMessageBox.information(self, "下载完成", f"Whisper「{res}」模型已就绪。")
        self._update_status()

    def _open_models_dir(self):
        from core.config import MODELS_DIR
        try:
            os.makedirs(MODELS_DIR, exist_ok=True)
            open_path(MODELS_DIR)
        except Exception as e:
            QMessageBox.warning(self, "打不开", str(e))

    # ---- 豆包 / 纠错 / 词库 保存与测试 ----
    def _save_doubao_page(self):
        from core.config import save_doubao_vision, doubao_vision_config
        key = self.ed_d_key.text().strip() or (doubao_vision_config().get("api_key") or "")
        ep = self.ed_d_ep.text().strip()
        if not key or not ep:
            QMessageBox.information(self, "提示", "API Key 与端点 ID 都要填（首次配置不能留空 Key）")
            return
        try:
            save_doubao_vision(key, ep, self.ed_d_base.text().strip())
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.ed_d_key.clear()
        self._update_status()
        QMessageBox.information(self, "已保存", "豆包配置已加密落盘，即时生效。")

    def _save_asr_fix_page(self):
        from core.config import save_asr_fix, asr_fix_config
        key = self.ed_fix_key.text().strip() or (asr_fix_config().get("api_key") or "")
        model = self.ed_fix_model.text().strip()
        if not key or not model:
            QMessageBox.information(self, "提示", "API Key 与 Model 都要填（首次配置不能留空 Key）")
            return
        try:
            save_asr_fix(key, model, self.ed_fix_base.text().strip())
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.ed_fix_key.clear()
        self._update_status()
        QMessageBox.information(self, "已保存", "语音纠错配置已加密落盘，即时生效。")

    def _test_asr_fix(self):
        """拿当前输入框里的 key/model/base 现场纠正一句（允许未保存先测）。"""
        from core.config import asr_fix_config
        cfg = dict(asr_fix_config())
        if self.ed_fix_key.text().strip():
            cfg["api_key"] = self.ed_fix_key.text().strip()
        if self.ed_fix_model.text().strip():
            cfg["model"] = self.ed_fix_model.text().strip()
        if self.ed_fix_base.text().strip():
            cfg["base_url"] = self.ed_fix_base.text().strip()
        from video_text_tools.asr.fixer import AsrFixer
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        ok = False
        try:
            out = AsrFixer(cfg).correct_segments(["长期吃盖片可以预防骨松"])
            msg = f"连接成功，示例纠正：\n长期吃盖片可以预防骨松 → {out[0] if out else ''}"
            ok = True
        except Exception as e:
            msg = f"失败：{type(e).__name__}: {e}"
        finally:
            QApplication.restoreOverrideCursor()
        (QMessageBox.information if ok else QMessageBox.warning)(self, "语音纠错测试", msg)

    def _save_glossary(self):
        from core.config import asr_glossary_save
        saved = asr_glossary_save(self.ed_gloss.toPlainText().splitlines())
        self.ed_gloss.setPlainText("\n".join(saved))
        self._update_status()
        QMessageBox.information(self, "已保存", f"词库已保存（{len(saved)} 个词），即时生效。")

    def _import_glossary(self):
        """从 Excel/CSV/JSON 文件读词库，合并进当前文本框（去重、不自动落盘，让用户先审）。"""
        from core.config import glossary_from_file
        path, _ = QFileDialog.getOpenFileName(
            self, "导入词库文件", "",
            "词库文件 (*.xlsx *.xls *.csv *.json);;Excel (*.xlsx *.xls);;CSV (*.csv);;JSON (*.json)")
        if not path:
            return
        try:
            terms = glossary_from_file(path)
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"读取 {os.path.basename(path)} 失败：\n{e}")
            return
        if not terms:
            QMessageBox.information(self, "没读到词",
                                    "文件里没解析出任何词条（确认 Excel/CSV 每格一个词、或 JSON 是词列表）")
            return
        cur = [s.strip() for s in self.ed_gloss.toPlainText().splitlines() if s.strip()]
        seen, merged = set(), []
        for s in cur + terms:                      # 现有在前、新导入在后，整体去重保序
            if s and s not in seen:
                seen.add(s)
                merged.append(s)
        self.ed_gloss.setPlainText("\n".join(merged))
        QMessageBox.information(
            self, "已导入",
            f"从文件读入 {len(terms)} 词，合并去重后共 {len(merged)} 词（已填进上方文本框）。\n"
            "确认无误后点「💾 保存词库」才正式生效。")

    def _export_glossary(self):
        """把当前文本框里的词库另存为 Excel/CSV/JSON（按选定后缀）。"""
        from core.config import glossary_to_file
        path, _ = QFileDialog.getSaveFileName(
            self, "导出词库文件", "词库.xlsx",
            "Excel (*.xlsx);;CSV (*.csv);;JSON (*.json)")
        if not path:
            return
        terms = self.ed_gloss.toPlainText().splitlines()
        try:
            saved = glossary_to_file(path, terms)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"写入文件失败：\n{e}")
            return
        QMessageBox.information(self, "已导出", f"已导出 {len(saved)} 词到：\n{path}")

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

        不能吃启动缓存 ACCOUNTS（导入刚写完盘它还是旧值），
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
