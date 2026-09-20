"""
worker_scan.py —— 手动扫描新行
"""
from utils.excel_utils import scan_new_rows as scan_new_rows_impl
from registry.manager import REG


class ScanState:
    def __init__(self):
        self.asked = {}
        self.ignored = set()
        self.submitted = set()

SCAN = ScanState()


def mark_submitted(row_idx):
    SCAN.submitted.add(row_idx)


def scan_new_rows():
    rows = scan_new_rows_impl()
    out = []
    for row_idx, info in rows:
        if row_idx in SCAN.ignored:
            continue
        if row_idx in SCAN.submitted:
            continue
        if REG.get_by_row(row_idx):
            continue
        out.append((row_idx, info))
    return out


def print_new_rows():
    rows = scan_new_rows()
    if not rows:
        print("没有新项目")
        return []
    print(f"\n检测到 {len(rows)} 个新项目：")
    for i, (row_idx, info) in enumerate(rows, 1):
        prompt_short = str(info["提示词"])[:50].replace("\n", " ")
        print(f"  {i}. 行{row_idx + 1}  品名={info['品名']}  提示词={prompt_short}")
    return rows
