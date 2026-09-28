"""
tests/test_asr_tool.py —— 「🎙 语音识别」工具的登记一致性（不建 Qt 窗口）

用户诉求：语音识别要在工具中心里看得见的独立工具。这里钉住它确实上架了：
  - gui.tool_panels.PANEL_FACTORIES 有「语音识别」factory；
  - gui.pages_tools.TOOLS 有一条同名卡片，且 factory 名能在 PANEL_FACTORIES 取到
    （取不到就是「规划中」灰卡，用户点不开）；
  - dialogs_asr / asr_widgets 可正常导入（业务链路指向 asr 主体、subtitle 引擎）。
面板构建用 offscreen QApplication（不弹窗、不显示），仅验证 ModelBar 与进度条不冲突。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_asr_tool_registered_and_visible():
    from gui.tool_panels import PANEL_FACTORIES
    from gui.pages_tools import TOOLS
    assert "语音识别" in PANEL_FACTORIES, "PANEL_FACTORIES 缺「语音识别」factory"
    cards = {name: (icon, desc, fac) for name, icon, desc, fac in TOOLS}
    assert "语音识别" in cards, "工具中心 TOOLS 未上架「语音识别」卡片"
    icon, desc, fac = cards["语音识别"]
    assert fac == "语音识别", "卡片 factory 名与登记表不一致"
    assert icon and desc and PANEL_FACTORIES.get(fac) is not None


def test_asr_modules_import_clean():
    # 主体包 + 工具/控件层都能导入，且工具走的是 subtitle 引擎的统一编排
    import video_text_tools.asr as asr
    import gui.dialogs_asr  # noqa: F401
    import gui.asr_widgets  # noqa: F401
    from video_text_tools.subtitle.models import SubtitleOptions
    o = SubtitleOptions()
    assert o.asr_fix is False and o.save_txt is False   # 默认关：不擅自联网/多产文件
    assert hasattr(asr, "apply_fix") and hasattr(asr.transcribe, "model_ready")


def test_asr_panel_model_bar_not_shadowed():
    """回归钉：BasePanel.make_run_row 会把 self.bar 设成进度条；若 AsrPanel 把
    ModelBar 也叫 self.bar，就会被静默覆盖，点「开始识别」时 self.bar.model_size()
    直接 AttributeError。故 ModelBar 必须挂在独立属性 model_bar 上，且能取到档位。"""
    from PySide6.QtWidgets import QApplication, QProgressBar
    from gui.asr_widgets import ModelBar
    from gui.tool_panels import PANEL_FACTORIES
    app = QApplication.instance() or QApplication([])   # noqa: F841
    panel = PANEL_FACTORIES["语音识别"]()
    assert isinstance(panel.model_bar, ModelBar), "ModelBar 被进度条覆盖了"
    assert isinstance(panel.bar, QProgressBar), "进度条 self.bar 丢失"
    assert panel.model_bar.model_size() in ("medium", "small", "base", "tiny")
