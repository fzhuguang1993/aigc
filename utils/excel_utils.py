"""
excel_utils.py —— Excel 读写（带全局锁）+ 命名规则 + 扫描
"""
import re
import threading
import pandas as pd
from pathlib import Path
from datetime import datetime

from core.config import (
    EXCEL_PATH, SHEET_TASK, SHEET_NAME_RULE,
    COL_ID, COL_PRODUCT, COL_PROMPT, COL_SCRIPT, COL_STATUS, COL_ACCOUNT,
    COL_JOB_ID, COL_OUTPUT, COL_URL, COL_RUNS, COL_SUCCESS, COL_CANCEL,
    COL_SCRIPT_TEXT, COL_NAME, ASSET_DIR, DOWNLOAD_DIR, DEFAULT_PRODUCT,
    USER_NAME
)
from core.logger import raw_info, raw_warning, raw_error

_excel_lock = threading.RLock()


def load_tasks():
    with _excel_lock:
        df = pd.read_excel(EXCEL_PATH, sheet_name=SHEET_TASK)
        for col in [COL_ID, COL_PRODUCT, COL_PROMPT, COL_SCRIPT,
                    COL_STATUS, COL_ACCOUNT, COL_JOB_ID,
                    COL_OUTPUT, COL_URL, COL_RUNS, COL_SUCCESS, COL_CANCEL,
                    COL_SCRIPT_TEXT]:
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


def sanitize(s):
    return re.sub(r'[\\/:*?"<>|]', "_", str(s).strip())


def next_seq(prefix):
    base = Path(DOWNLOAD_DIR)
    if not base.exists():
        return 1
    max_seq = 0
    pat = re.compile(re.escape(prefix) + r"(\d+)_")
    for p in base.rglob(f"{prefix}*"):
        m = pat.match(p.name)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def build_filename(num, product, complete_time, name, seq):
    product = sanitize(product) or DEFAULT_PRODUCT
    name = sanitize(name) or "未知姓名"
    date_str = complete_time.strftime("%m%d")
    return f"{int(num):03d}_{product}_{date_str}_{seq:02d}_{name}.mp4"
