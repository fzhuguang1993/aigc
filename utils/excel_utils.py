"""
excel_utils.py —— Excel 读写（带全局锁）+ 新任务扫描

文件命名不在这里：已整体搬到 core/naming.py（以前两处各写一份，
改规则要同步两个文件，现在只有一处）。
"""
import threading
import pandas as pd

from core.config import (
    EXCEL_PATH, SHEET_TASK, SHEET_NAME_RULE,
    COL_ID, COL_PRODUCT, COL_PROMPT, COL_SCRIPT, COL_STATUS, COL_ACCOUNT,
    COL_JOB_ID, COL_OUTPUT, COL_URL, COL_RUNS, COL_SUCCESS, COL_CANCEL,
    COL_SCRIPT_TEXT, COL_NAME, ASSET_DIR, USER_NAME,
    COL_BK_LINK, COL_BK_HOOK, COL_BK_SHOTS, COL_BK_SCRIPT,
    COL_BK_PROMPT_VISUAL, COL_BK_PROMPT_COPY, COL_BK_PROMPT_SHOOT,
    COL_BK_HOOK_SCORE, COL_BK_FACTORS, COL_BK_EMOTION, COL_BK_FORMULA,
    COL_BK_BLUEPRINT, BREAKDOWN_COLUMNS
)
from core.logger import raw_info, raw_warning, raw_error

_excel_lock = threading.RLock()


def load_tasks():
    with _excel_lock:
        df = pd.read_excel(EXCEL_PATH, sheet_name=SHEET_TASK)
        for col in [COL_ID, COL_PRODUCT, COL_PROMPT, COL_SCRIPT,
                    COL_STATUS, COL_ACCOUNT, COL_JOB_ID,
                    COL_OUTPUT, COL_URL, COL_RUNS, COL_SUCCESS, COL_CANCEL,
                    COL_SCRIPT_TEXT] + BREAKDOWN_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        for col in [COL_ID, COL_PRODUCT, COL_PROMPT, COL_SCRIPT,
                    COL_STATUS, COL_ACCOUNT, COL_JOB_ID,
                    COL_OUTPUT, COL_URL]:
            df[col] = df[col].apply(lambda x: "" if pd.isna(x) else str(x)).astype(object)

        def to_int(x):
            try:
                if pd.isna(x) or str(x).strip() in ("", "nan", "None"):
                    return 0
                return int(float(x))
            except Exception:
                return 0
        for col in [COL_RUNS, COL_SUCCESS, COL_CANCEL]:
            df[col] = df[col].apply(to_int).astype(int)

        return df


def save_tasks(df):
    with _excel_lock:
        with pd.ExcelWriter(EXCEL_PATH, engine="openpyxl",
                            mode="a", if_sheet_exists="replace") as w:
            df.to_excel(w, sheet_name=SHEET_TASK, index=False)


def update_row(row_idx, **fields):
    with _excel_lock:
        df = load_tasks()
        if row_idx >= len(df):
            raw_error(f"update_row 越界：{row_idx}")
            return
        for k, v in fields.items():
            if k in df.columns:
                # 处理多行文本：将换行符替换为空格
                if isinstance(v, str) and '\n' in v:
                    v = v.replace('\n', ' ')
                df.at[row_idx, k] = v
        save_tasks(df)


def load_name_rule():
    # 优先使用本地配置的姓名（首次运行向导 config.json 中填写）
    if USER_NAME:
        return USER_NAME
    with _excel_lock:
        try:
            df = pd.read_excel(EXCEL_PATH, sheet_name=SHEET_NAME_RULE)
            if COL_NAME in df.columns and len(df) > 0:
                v = df[COL_NAME].dropna().iloc[0]
                return str(v).strip()
        except Exception:
            pass
        return ""


_DONE_STATUS = {
    "submitted", "queued", "running", "starting", "cancelling",
    "completed", "failed", "cancelled", "error", "submit_failed",
    "skipped", "timeout",
}


def is_new_row(row):
    prompt = str(row.get(COL_PROMPT, "")).strip()
    if not prompt or prompt in ("nan", "None"):
        return False

    try:
        runs = int(float(row.get(COL_RUNS, 0) or 0))
    except Exception:
        runs = 0

    status = str(row.get(COL_STATUS, "")).strip().lower()
    jid = str(row.get(COL_JOB_ID, "")).strip().lower()

    if runs > 0:
        return False
    if status in _DONE_STATUS:
        return False
    if jid and jid not in ("", "nan", "none"):
        return False
    return True


def scan_new_rows():
    df = load_tasks()
    out = []
    for i in range(len(df)):
        row = df.iloc[i]
        if is_new_row(row):
            out.append((i, {
                "编号": row.get(COL_ID, ""),
                "品名": row.get(COL_PRODUCT, ""),
                "提示词": row.get(COL_PROMPT, ""),
            }))
    return out


# ====================================================================
# 爆款拆解回写：BreakdownResult → 12 列（多行文本原样保留，失败阶段显式标注）
#   不复用 update_row：它会把 \n 折成空格，分镜/提示词这类多行内容会被压成一行。
# ====================================================================
def _fmt_ts(sec):
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def _fmt_shots(result):
    """分镜表：每帧一行【时间】景别/运镜/构图/转场/文字/情绪；解析不出字段就退回原始文本。"""
    lines = []
    for a in result.frame_analyses:
        bits = []
        for label, val in (("景别", a.shot_size), ("运镜", a.camera),
                           ("构图", a.composition), ("转场", a.transition),
                           ("文字", a.on_screen_text), ("情绪", a.emotion)):
            if val:
                bits.append(f"{label}:{val}")
        if not bits:
            bits.append(a.raw or "")
        lines.append(f"【{_fmt_ts(a.ts)}】" + " ".join(bits))
    return "\n".join(lines)


def _fmt_prompts(result, attr):
    return "\n".join(f"【{s.time_range}】{getattr(s, attr)}"
                     for s in result.segments if getattr(s, attr, ""))


def breakdown_cells(result):
    """把 BreakdownResult 映射成 12 列文本 dict。失败阶段写“XX失败：原因”，不静默。"""
    from video_text_tools.breakdown.models import (
        STAGE_ACQUIRE, STAGE_TRANSCRIBE, STAGE_VISION, STAGE_PROMPTS)

    def fail(stage):
        v = str(result.stage_status.get(stage, ""))
        return v[5:] if v.startswith("fail:") else ""

    ov = result.overall
    cells = {COL_BK_LINK: result.link}
    # 拆解钩子 / 整体分析字段
    cells[COL_BK_HOOK] = ov.hook_desc
    cells[COL_BK_HOOK_SCORE] = ov.hook_score
    cells[COL_BK_FACTORS] = ov.factors
    cells[COL_BK_EMOTION] = ov.emotion_curve
    cells[COL_BK_FORMULA] = ov.formula
    cells[COL_BK_BLUEPRINT] = ov.blueprint
    # 分镜（视觉分析）/口播（转写）：失败就在对应列标注
    vf = fail(STAGE_VISION)
    cells[COL_BK_SHOTS] = f"画面分析失败：{vf}" if vf else _fmt_shots(result)
    tf = fail(STAGE_TRANSCRIBE)
    cells[COL_BK_SCRIPT] = f"口播转写失败：{tf}" if tf else result.transcript_text
    pf = fail(STAGE_PROMPTS)
    if pf:
        msg = f"提示词生成失败：{pf}"
        cells[COL_BK_PROMPT_VISUAL] = msg
        cells[COL_BK_PROMPT_COPY] = msg
        cells[COL_BK_PROMPT_SHOOT] = msg
    else:
        cells[COL_BK_PROMPT_VISUAL] = _fmt_prompts(result, "visual_prompt")
        cells[COL_BK_PROMPT_COPY] = _fmt_prompts(result, "copy_prompt")
        cells[COL_BK_PROMPT_SHOOT] = _fmt_prompts(result, "shoot_prompt")
    # 解析下载失败：整表以“失败”落第一列，其余留空（半成品本就不该写，GUI 已拦）
    af = fail(STAGE_ACQUIRE)
    if af:
        cells = {c: "" for c in BREAKDOWN_COLUMNS}
        cells[COL_BK_LINK] = f"解析下载失败：{af}"
    return cells


def write_breakdown(row_idx, result):
    """把一条拆解结果写进任务表的 12 列并保存（保留换行）。

    row_idx 为 None/负数/越界 → 追加新行。返回实际落位的行号。"""
    cells = breakdown_cells(result)
    with _excel_lock:
        df = load_tasks()
        if row_idx is None or row_idx < 0 or row_idx >= len(df):
            df.loc[len(df)] = {c: "" for c in df.columns}
            row_idx = len(df) - 1
        for k, v in cells.items():
            if k not in df.columns:
                df[k] = ""
            df.at[row_idx, k] = v if isinstance(v, str) else str(v)
        save_tasks(df)
    return row_idx
