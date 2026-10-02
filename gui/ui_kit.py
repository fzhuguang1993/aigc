"""
gui/ui_kit.py —— 全站视觉「设计令牌」单一真源（颜色 / 字号 / 圆角 / 间距）

为什么要这一份：配色、字号、圆角、间距历史上散落在 theme.py 的大段 QSS、header.py 的
FS_* 常量、数据中台的 _STATUS_COLOR/KpiCard、menus.py 的 RADIUS、各页内联样式里，同一个
主蓝写了六七遍——想整体调个色得满项目找。这里把它们收敛成一份 TOKENS，theme.py 的 QSS
改为引用本表（改一处全站生效），各页需要内联配色时也应 from gui.ui_kit import COLORS。

约定：
- 值只在这里定义，别处只引用；新增语义色请在本表补一条，不要再散写十六进制。
- rgba()/tint() 给「软色底/描边」这类按主色派生的场景用（KPI 徽章、字段胶囊都吃这个）。
- 想快速看全所有令牌与组件效果，开「UI 组件库」画廊页（gui/pages_ui_kit.py）。
"""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen


# --------------------------------------------------------------------
# 语义色：一份 hex，全站引用。名字带语义，改值不改名。
# --------------------------------------------------------------------
COLORS = {
    # 品牌主色（飞书蓝三态 + 软底）
    "primary":        "#3370FF",
    "primary_hover":  "#5A8BFF",
    "primary_press":  "#2457D9",
    "primary_soft":   "#EAF1FF",   # 选中/悬停淡蓝底
    "primary_bg":     "#F2F6FF",   # 更淡的一层蓝（幽灵钮按下、快捷筛选底）

    # 语义状态色
    "success":        "#00B96B",
    "warning":        "#FF8D19",
    "danger":         "#F54A45",
    "danger_text":    "#D83931",   # 危险文字（比纯 danger 稳一点的深红）
    "danger_bg":      "#FFF1F0",   # 危险钮淡红底
    "danger_border":  "#FFCCC7",
    "info":           "#0FB5AE",   # 青（生成时长等中性信息）
    "purple":         "#7F3FBF",   # 成功率等强调
    "violet_soft":    "#B57EDF",

    # 线路状态灯（商务低饱和版，header.FS_*）：区别于高饱和 success/warning/danger
    "line_ok":        "#2F9E77",   # 墨绿
    "line_alive":     "#C88A2E",   # 赭石
    "line_down":      "#C14B4B",   # 干枯玫瑰红

    # 文字层级
    "text":           "#1F2329",   # 正文
    "sub":            "#646A73",   # 辅文
    "weak":           "#8F959E",   # 弱文 / 占位 / 禁用文字
    "placeholder":    "#8F959E",
    "on_primary":     "#FFFFFF",   # 主色底上的文字

    # 边框 / 分隔
    "border":         "#DEE0E3",   # 常规控件描边
    "border_strong":  "#C9CDD4",   # 需要更明显时 / 滚动条滑块
    "border_hover":   "#A9AFB8",   # 滚动条滑块悬停
    "divider":        "#EFF0F1",   # 表格行分隔 / 菜单分隔
    "border_popup":   "#E5E7EB",   # 弹层/表格/树外框
    "card_border":    "#ECEEF1",   # 卡片外框（比弹层更淡一档）
    "disabled_border":"#E9EBEF",   # 禁用控件描边

    # 背景层级
    "bg":             "#F2F3F5",   # 窗口底
    "bg_soft":        "#FAFBFC",   # 略深一层的容器底
    "bg_field":       "#F7F8FA",   # 悬停行 / 表头浅底
    "disabled_bg":    "#F5F6F7",   # 禁用控件底 / 表头底
    "card":           "#FFFFFF",   # 卡片 / 弹层白底
    "track":          "#EFF1F5",   # 进度条轨道

    # 表格表头文字
    "table_head":     "#475569",

    # 侧栏（深色导航）
    "sidebar":        "#17212F",
    "sidebar_item":   "#243144",   # 侧栏项悬停 / SideBtn 底
    "sidebar_border": "#33455E",   # SideBtn 描边
    "sidebar_text":   "#D5E0F0",
    "sidebar_status": "#8FA3BF",
    "ghost_text":     "#37445A",   # 幽灵钮常态文字

    # 快捷筛选胶囊边
    "chip_border":    "#D6E4FF",
    "chip_hover":     "#E1ECFF",

    # 日志控制台（深底）
    "log_bg":         "#10151C",
    "log_text":       "#B7C4D6",
}


# 数据中台状态色：状态名 → hex（pages_dashboard 用同名，收敛到这一处）
STATUS_COLORS = {
    "成功": COLORS["success"],
    "失败": COLORS["danger"],
    "错误": COLORS["danger"],
    "取消": COLORS["weak"],
    "运行中": COLORS["warning"],
}

# KPI 卡配色序列：图标软块色（数据中台那排卡按此取色）
KPI_PALETTE = [
    COLORS["primary"], COLORS["success"], COLORS["purple"], COLORS["danger"],
    COLORS["weak"], COLORS["warning"], COLORS["info"],
]

# 重命名/显示名弹窗的字段主题色（dialogs_rename 复用；一字段一色）
FIELD_COLORS = {
    "seq": COLORS["primary"], "stem": COLORS["purple"],
    "product": COLORS["success"], "tag": COLORS["warning"],
    "block_type": COLORS["info"], "date": "#2EA6FF", "date_full": "#5B8DEF",
    "time": COLORS["violet_soft"], "task_id": COLORS["weak"],
}


# --------------------------------------------------------------------
# 圆角 / 间距 / 字号刻度：都走「比例尺」，别再零散写魔数
# --------------------------------------------------------------------
RADIUS = {
    "sm": 6,        # 菜单项、小徽标
    "md": 8,        # 输入框、按钮、卡片内小元素
    "lg": 10,       # 菜单容器、日志框
    "card": 12,     # 卡片 / 表格 / 树
    "pill": 14,     # 胶囊（快捷筛选、字段胶囊）
}

SPACE = {           # 留白比例尺（px）：4 的基，配 6/10 过渡
    "xs": 4, "sm": 6, "md": 8, "lg": 12, "xl": 14, "2xl": 16, "3xl": 18,
}

FONT = {
    "family": '"Microsoft YaHei UI", "Microsoft YaHei"',
    "mono": 'Consolas, monospace',
    "xs": 11, "sm": 12, "base": 13, "md": 14,
    "title": 17, "page_title": 20, "dialog_title": 18, "kpi": 26,
}


# --------------------------------------------------------------------
# 派生工具：按主色生成「软底 / 描边 / 悬停」，KPI 徽章与字段胶囊都吃这个
# --------------------------------------------------------------------
def rgba(color, alpha):
    """#RRGGBB + 0~1 透明度 → 'rgba(r,g,b,a)'。color 非 #hex 时按原名返回。"""
    c = QColor(color)
    if not c.isValid():
        return color
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


# tint 是 rgba 的语义别名，调用处读起来更直白（软色底常写作 tint(primary, .12)）
tint = rgba


def color(name, default=None):
    """按语义名取色；给画廊/各页统一入口，避免拼错 key 直接 KeyError 崩 UI。"""
    return COLORS.get(name, default if default is not None else COLORS["text"])


def draw_rounded_card(pr, rect, radius, margin, fill=None, border=None):
    """在透明顶层上自绘一张圆角白卡 + 向外逐层变淡的软阴影（弹层家族共用：
    菜单 / 圆角浮层 / tooltip 气泡 / 无边框对话框 / 全文预览浮层都吃这一个）。
    margin 为四周留白（也是阴影活动带），内容布局需用同宽边距避开圆角。
    为什么放这儿：这套画法是「弹层圆角」的单一真源，历史上长在 kit.py，
    但 widgets.py 反向被 kit.py 依赖、没法 import 回来；提到令牌层，两侧都能复用。
    pr 需已开启 Antialiasing；radius 缺省取 RADIUS['lg']（与菜单容器刻度对齐）。"""
    if radius is None:
        radius = RADIUS["lg"]
    card = QRectF(rect).adjusted(margin, margin, -margin, -margin)
    sc = QColor(COLORS["text"])
    pr.setPen(Qt.PenStyle.NoPen)
    steps = 6
    # 软阴影：整体调淡一档（太重白底上一眼看出“发灰”），保留最内层一点点层次即可——
    # 描边已能勾出轮廓，阴影只负责把卡片从背景里轻轻托起来。
    for i in range(steps, 0, -1):
        off = i * (margin / steps)
        alpha = 3 + int(13 * (1 - i / steps))
        pr.setBrush(QColor(sc.red(), sc.green(), sc.blue(), alpha))
        pr.drawRoundedRect(card.adjusted(-off, -off, off, off),
                           radius + off, radius + off)
    pr.setBrush(QColor(fill or COLORS["card"]))
    pr.setPen(QPen(QColor(border or COLORS["border_popup"]), 1))
    pr.drawRoundedRect(card, radius, radius)


def swatches():
    """画廊配色板用：返回 (分类, [(名, hex), ...])，按语义分组好读。"""
    groups = {
        "品牌 / 主色": ["primary", "primary_hover", "primary_press",
                        "primary_soft", "primary_bg"],
        "状态语义": ["success", "warning", "danger", "danger_text",
                     "danger_bg", "info", "purple"],
        "文字层级": ["text", "sub", "weak", "table_head", "ghost_text"],
        "边框 / 分隔": ["border", "border_strong", "border_hover",
                        "divider", "border_popup"],
        "背景层级": ["bg", "bg_soft", "bg_field", "card", "track"],
        "侧栏（深色）": ["sidebar", "sidebar_item", "sidebar_text",
                         "sidebar_status"],
    }
    return [(g, [(k, COLORS[k]) for k in keys if k in COLORS])
            for g, keys in groups.items()]
