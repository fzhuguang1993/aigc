"""
video_text_tools/breakdown/report.py —— 拆解结果导出 Word（.docx）报告

拆解成功（非半成品）后，把 BreakdownResult 排成一份人能直接读的 Word 文档，落在
视频所在目录、与视频同名（骨胶原-9.27-142_11.mp4 → 同名 .docx），取代此前「回写任务
表 12 列」的 Excel 交付方式（Excel 可读性差、拆一条塞一行不便看）。

纯 python-docx 排版：无 Qt、无网络、不碰任何全局状态。docx 是「爆款拆解」的可选
重依赖，一律延迟导入——没装时 write_report 抛 DepMissing，由调用方（GUI）明确提示
「装 requirements-breakdown.txt」，绝不在这里崩。任何空字段都安全跳过，保证哪怕只
解析出一部分结果，也能出一份完整、不缺行的文档。
"""
from pathlib import Path

# 文件名非法字符（Windows）：命中的统一替换为下划线，避免标题带 : * 时落盘失败
_ILLEGAL = '\\/:*?"<>|'


def _safe_stem(result):
    """文档主名：优先用视频文件名（与视频同名最好认），退回标题，再退回固定词。"""
    base = ""
    if result.video_path:
        base = Path(result.video_path).stem
    if not base:
        base = (result.title or "").strip()
    if not base:
        base = "爆款拆解报告"
    for ch in _ILLEGAL:
        base = base.replace(ch, "_")
    return base.strip() or "爆款拆解报告"


def _clean(v):
    """把 None / 空白统一成可判定的字符串；换行/制表压成单空格，便于逐行排版。"""
    return str(v).strip() if v is not None else ""


def _add_field(doc, label, value):
    """一行「字段名：值」；值为空则整行不写。字段名加粗。"""
    value = _clean(value)
    if not value:
        return
    p = doc.add_paragraph()
    p.add_run(f"{label}：").bold = True
    p.add_run(value)


def _add_meta(doc, result):
    """标题页：链接 / 时长 / 分镜数 / 豆包成本，概览一眼看清。"""
    cost = result.cost or {}
    _add_field(doc, "参考链接", result.link)
    if result.duration:
        _add_field(doc, "视频时长", f"{result.duration:.0f} 秒")
    if result.shot_count:
        _add_field(doc, "分镜数量", str(result.shot_count))
    calls = cost.get("vision_calls")
    tokens = cost.get("tokens")
    if calls or tokens:
        _add_field(doc, "豆包用量", f"{calls or 0} 次调用 / {tokens or 0} token")


def _add_overall(doc, ov):
    """一、整体分析：钩子、评分、爆点因素、情绪曲线、内容公式、复刻蓝图。"""
    doc.add_heading("一、整体分析", level=1)
    _add_field(doc, "钩子描述", ov.hook_desc)
    _add_field(doc, "钩子评分", ov.hook_score)
    _add_field(doc, "爆点因素", ov.factors)
    _add_field(doc, "情绪曲线", ov.emotion_curve)
    _add_field(doc, "内容公式", ov.formula)
    _add_field(doc, "复刻蓝图", ov.blueprint)


def _add_transcript(doc, result):
    """二、口播逐字稿：有分段就逐句带起始秒数，否则整段纯文本。全空则不写本章节。"""
    lines = []
    for seg in (result.transcript or []):
        txt = _clean(getattr(seg, "text", ""))
        if not txt:
            continue
        start = getattr(seg, "start", None)
        prefix = f"[{start:.1f}s] " if isinstance(start, (int, float)) else ""
        lines.append(prefix + txt)
    if not lines and _clean(result.transcript_text):
        lines = [ln for ln in result.transcript_text.splitlines() if _clean(ln)]
    if not lines:
        return
    doc.add_heading("二、口播逐字稿", level=1)
    for ln in lines:
        doc.add_paragraph(ln)


def _add_shots(doc, analyses):
    """三、分镜画面表：序号｜时间｜景别｜运镜｜构图｜转场｜屏幕文字｜情绪。"""
    if not analyses:
        return
    doc.add_heading("三、分镜画面表", level=1)
    headers = ["序号", "时间", "景别", "运镜", "构图", "转场", "屏幕文字", "情绪"]
    table = doc.add_table(rows=1, cols=len(headers))
    try:
        table.style = "Table Grid"          # 内置样式，默认模板必有
    except Exception:
        pass
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = h
    for a in analyses:
        cells = table.add_row().cells
        vals = [_clean(getattr(a, "idx", "")), f"{getattr(a, 'ts', 0):.0f}s",
                a.shot_size, a.camera, a.composition, a.transition,
                a.on_screen_text, a.emotion]
        for i, v in enumerate(vals):
            cells[i].text = _clean(v)


def _add_prompts(doc, segments):
    """四、三类复刻提示词：逐分镜列画面/文案/复刻三段（有概要先写概要）。"""
    if not segments:
        return
    doc.add_heading("四、三类复刻提示词", level=1)
    for s in segments:
        rng = _clean(s.time_range)
        head = f"分镜 {s.index}（{rng}）" if rng else f"分镜 {s.index}"
        p = doc.add_paragraph()
        p.add_run(head).bold = True
        summary = _clean(s.summary)
        if summary:
            sp = doc.add_paragraph()
            sp.add_run("概要：").bold = True
            sp.add_run(summary)
        for label, val in (("画面", s.visual_prompt), ("文案", s.copy_prompt),
                           ("复刻", s.shoot_prompt)):
            if _clean(val):
                doc.add_paragraph(f"{label}：{_clean(val)}", style="List Bullet")


def write_report(result, out_dir=None, log=None):
    """把一条拆解结果导出为 Word 文档，返回落盘路径（str）。

    out_dir 为空时落「视频所在目录」（无视频路径则当前目录）。文件名取视频主名，
    与下载的视频同名。docx 未安装 → 抛 DepMissing（消息给人看的安装指引）。
    """
    def _say(msg):
        if log:
            log(msg)

    try:
        from docx import Document
    except ImportError as e:                 # 延迟导入：没装不影响拆解本身
        from .models import DepMissing
        raise DepMissing(
            "导出 Word 报告需要 python-docx：pip install -r "
            "requirements-breakdown.txt") from e

    if out_dir:
        target_dir = Path(out_dir)
    elif result.video_path:
        target_dir = Path(result.video_path).parent
    else:
        target_dir = Path(".")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{_safe_stem(result)}.docx"

    doc = Document()
    doc.add_heading(result.title or "爆款拆解报告", level=0)
    _add_meta(doc, result)
    _add_overall(doc, result.overall)
    _add_transcript(doc, result)
    _add_shots(doc, result.frame_analyses)
    _add_prompts(doc, result.segments)

    doc.save(str(path))
    _say(f"✓ 已导出 Word 拆解文档：{path}")
    return str(path)
