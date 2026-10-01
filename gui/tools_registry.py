"""
gui/tools_registry.py —— 工具中心「登记表 + 固定项 + 快捷键」共享底座

为什么要单独一层：TOOLS 这份工具登记表被「工具中心」(pages_tools) 和
「设置页」(pages_settings，给每个工具配快捷键) 两处共用，快捷键读写
(set_tool_shortcut) 也被两个页面反向调用——以前都寄居在 pages_tools 里，
于是形成 pages_tools ↔ pages_settings 的模块级循环（靠函数内延迟 import
勉强断开）。把纯数据与共享行为抽到本模块后，两个页面都只依赖这里、互不
import，循环从根上消失。

本模块只碰数据层 (store.app_state) 与面板工厂表 (gui.tool_panels)，
不引用任何具体页面类，是被页面依赖的「下层」，自身不依赖上层。

新增工具两步：
  1. 在 gui/tool_panels.py 的 PANEL_FACTORIES 里登记 factory；
  2. 在下方 TOOLS 里加一条卡片信息（factory 传 None 显示为「规划中」）。
"""
from PySide6.QtWidgets import QApplication

from gui.tool_panels import PANEL_FACTORIES
from store import app_state

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


def set_tool_shortcut(name, seq):
    """写 app_state 并让主窗口重注册（设置页与工具卡片右键共用）；返回生效值"""
    seq = (seq or "").strip()
    seqs = dict(app_state.get("tool_shortcuts") or {})
    if seq:
        # 一键一工具：同键其他工具让位，避免一个键弹两个窗口
        for k in [k for k, v in seqs.items() if v == seq and k != name]:
            seqs.pop(k)
        seqs[name] = seq
    else:
        seqs.pop(name, None)
    app_state.set_value("tool_shortcuts", seqs)
    w = QApplication.instance().activeWindow()
    if hasattr(w, "apply_tool_shortcuts"):
        w.apply_tool_shortcuts()
    return seq
