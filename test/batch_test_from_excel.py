#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
batch_test_from_excel.py - 从 Excel 读取提示词并批量测试
根据参考组合自动从 test 文件夹读取素材
"""

import sys
import re
from pathlib import Path
import pandas as pd

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.logger import Ctx, raw_info, raw_warning
from workers.submit import do_submit, build_payload
from core.config import MODE, ASSET_DIR, DEFAULT_DURATION
from core.api_client import upload_asset, submit_job
from registry.manager import REG, invalidate_load_cache, get_account, pick_account
from utils.excel_utils import load_tasks, update_row, COL_RUNS, COL_STATUS, COL_ACCOUNT, COL_JOB_ID


# ============================================================
# 配置
# ============================================================

EXCEL_FILE = "/Users/leiliang/PycharmProjects/aigc/多模态交叉测试提示词.xlsx"
MATERIAL_ROOT = "/Users/leiliang/PycharmProjects/aigc/test"

selected_product = "诺特兰德益生菌"


# ============================================================
# 素材读取逻辑
# ============================================================

def parse_reference_combination(ref_text):
    """
    解析参考组合文本，提取素材类型和编号
    例如：<Picture 1>产品 + <Picture 2>明星 A -> {'pictures': [1], 'kols': ['A'], ...}
    """
    result = {
        'pictures': [],  # 图片编号
        'kols': [],      # 明星编号
        'videos': [],    # 视频编号
        'audios': []     # 音频编号
    }
    
    # 提取 Picture N
    picture_matches = re.findall(r'<Picture\s*(\d+)>', ref_text)
    result['pictures'] = [int(n) for n in picture_matches]
    
    # 提取明星 (明星 A/B/C)
    star_matches = re.findall(r'明星 ([ABC])', ref_text)
    result['kols'] = star_matches
    
    # 提取 Video N
    video_matches = re.findall(r'<Video\s*(\d+)>', ref_text)
    result['videos'] = [int(n) for n in video_matches]
    
    # 提取 Audio N
    audio_matches = re.findall(r'<Audio\s*(\d+)>', ref_text)
    result['audios'] = [int(n) for n in audio_matches]
    
    return result


def get_material_path(material_type, index, name=None):
    """
    根据素材类型和编号获取文件路径
    material_type: 'product', 'kol', 'video', 'audio'
    index: 编号 (1, 2, 3...)
    name: 可选的名称 (如 'A', 'B', 'C')
    """
    if material_type == 'product':
        # 产品图只有一个
        return f"{MATERIAL_ROOT}/product.png"
    
    elif material_type == 'kol':
        # KOL 图在 kol 目录下
        kol_dir = f"{MATERIAL_ROOT}/kol"
        # 尝试找对应的文件 (如果有命名规则)
        if name:
            # 尝试匹配明星 A/B/C 对应的文件
            for f in Path(kol_dir).glob("*.jpg"):
                fname = f.name.lower()
                if name.upper() in fname or f.stem.lower() in fname:
                    return str(f)
        # 否则返回第一个 jpg 文件
        for f in Path(kol_dir).glob("*.jpg"):
            return str(f)
        return None
    
    elif material_type == 'video':
        # 视频在 videos 目录下
        video_dir = f"{MATERIAL_ROOT}/videos"
        files = list(Path(video_dir).glob("*.mp4"))
        if index <= len(files):
            return str(files[index - 1])
        return None
    
    elif material_type == 'audio':
        # 音频在 audios 目录下
        audio_dir = f"{MATERIAL_ROOT}/audios"
        files = list(Path(audio_dir).glob("*.mp3"))
        if index <= len(files):
            return str(files[index - 1])
        return None
    
    return None


def collect_materials(ref_text):
    """
    根据参考组合文本收集所有需要的素材路径
    返回：[素材路径 1, 素材路径 2, ...]
    """
    parsed = parse_reference_combination(ref_text)
    materials = []
    
    # 先加产品图 (总是第一个)
    product_path = get_material_path('product', 1)
    if product_path and Path(product_path).exists():
        materials.append(product_path)
    
    # 加明星图
    for kol_name in parsed['kols']:
        kol_path = get_material_path('kol', 1, kol_name)
        if kol_path and Path(kol_path).exists():
            materials.append(kol_path)
    
    # 加视频
    for vid_idx in parsed['videos']:
        vid_path = get_material_path('video', vid_idx)
        if vid_path and Path(vid_path).exists():
            materials.append(vid_path)
    
    # 加音频
    for aud_idx in parsed['audios']:
        aud_path = get_material_path('audio', aud_idx)
        if aud_path and Path(aud_path).exists():
            materials.append(aud_path)
    
    return materials


def upload_materials(materials, acc):
    """
    上传素材并返回 asset IDs
    """
    ref_ids = []
    ctx = Ctx(row=0, account=acc.name)
    
    for mat_path in materials:
        p = Path(mat_path)
        if not p.exists():
            ctx.warning(f"素材不存在：{p}")
            continue
        
        aid = upload_asset(acc.base, p, "image" if mat_path.endswith(('.png', '.jpg')) else "video")
        ref_ids.append(aid)
        ctx.debug(f"上传素材 {p.name} -> {aid[:20]}...")
    
    return ref_ids


def submit_with_materials(row_idx, product, prompt, ref_text):
    """
    根据参考组合提交任务，自动上传素材
    """
    acc = pick_account()
    ctx = Ctx(row=row_idx, account=acc.name)
    
    # 收集素材
    materials = collect_materials(ref_text)
    
    print(f"\n{'='*60}")
    print(f"测试 {row_idx+1}: {ref_text}")
    print(f"{'='*60}")
    print(f"品名：{product}")
    print(f"需要素材:")
    for i, mat in enumerate(materials, 1):
        print(f"  [{i}] {mat}")
    
    # 上传素材
    ref_ids = upload_materials(materials, acc)
    
    if not ref_ids:
        print(f"❌ 没有成功上传任何素材")
        return False
    
    print(f"✅ 成功上传 {len(ref_ids)} 个素材")
    
    # 构建 payload
    params = {
        "width": 768, "height": 1376,
        "duration": DEFAULT_DURATION, "seed": -1,
        "loras": [{"name": ""}],  # 使用默认 lora
    }
    
    inputs = {"prompt": prompt}
    if ref_ids:
        inputs["reference_images"] = ref_ids
    
    payload = {
        "feature": "minimax-h3", 
        "mode": MODE,
        "inputs": inputs, 
        "parameters": params
    }
    
    # 提交任务
    for attempt in range(3):
        try:
            resp = submit_job(acc.base, payload)
            job_id = resp["job_id"]
            ctx.job_id = job_id
            ctx.info("提交成功")
            REG.add(job_id, row_idx, acc.name, prompt, product)
            invalidate_load_cache(acc.name)
            mark_submitted(row_idx)

            df = load_tasks()
            runs = int(df.at[row_idx, COL_RUNS] or 0) + 1
            update_row(row_idx,
                       **{COL_RUNS: runs,
                          COL_STATUS: "submitted",
                          COL_ACCOUNT: acc.name,
                          COL_JOB_ID: job_id})
            print(f"✅ 提交成功 [{acc.name}] job_id={job_id[:8]}...")
            return True
        except Exception as e:
            ctx.error(f"提交失败：{e}")
        
        if attempt < 2:
            next_acc = pick_account()
            if next_acc.name != acc.name:
                ctx.info(f"换账号 {next_acc.name} 重试")
                acc = next_acc
                ctx.account = acc.name
    
    print(f"❌ 提交失败：all retries failed")
    return False


def mark_submitted(row_idx):
    """标记为已提交"""
    pass  # 简化版本，不更新 Excel


# ============================================================
# 核心功能
# ============================================================

def main():
    """主函数"""
    print("\n" + "="*60)
    print("AIGC 多模态交叉测试脚本")
    print("从 Excel 读取提示词并批量执行 (自动读取素材)")
    print("="*60)
    
    # 加载 Excel
    print(f"\n正在加载 Excel: {EXCEL_FILE}")
    try:
        xl = pd.ExcelFile(EXCEL_FILE)
    except Exception as e:
        print(f"❌ 加载 Excel 失败：{e}")
        return
    
    # 统计
    total = 0
    all_tests = {}
    for sheet_name in xl.sheet_names:
        df = pd.read_excel(xl, sheet_name=sheet_name)
        tests = []
        for _, row in df.iterrows():
            test = {
                "sheet": sheet_name,
                "编号": row["编号"],
                "参考组合": row["参考组合"],
                "完整提示词": row["完整提示词"]
            }
            tests.append(test)
            total += 1
        all_tests[sheet_name] = tests
    
    print(f"✓ 共加载 {total} 个测试")
    for sheet, tests in all_tests.items():
        print(f"  - {sheet}: {len(tests)}个")
    
    # 确认执行
    confirm = input("\n确认执行？[y/N]: ").strip().lower()
    if confirm != "y":
        print("已取消")
        return
    
    # 运行测试
    success_count = 0
    fail_count = 0
    current_index = 0
    
    for sheet_name, tests in all_tests.items():
        print(f"\n{'='*60}")
        print(f"开始执行：{sheet_name}")
        print(f"{'='*60}")
        
        for test in tests:
            print(f"\n[{current_index+1}/{total}] {test['sheet']} - 编号{test['编号']}")
            
            if submit_with_materials(current_index, selected_product, test['完整提示词'], test['参考组合']):
                success_count += 1
            else:
                fail_count += 1
            
            current_index += 1
            
            # 短暂延迟
            import time
            time.sleep(1)
    
    # 统计结果
    print("\n" + "="*60)
    print("测试完成!")
    print(f"成功：{success_count}个")
    print(f"失败：{fail_count}个")
    print(f"总计：{total}个")
    print("="*60)
    
    if fail_count > 0:
        print(f"\n⚠️ 有 {fail_count} 个测试失败，请查看日志")


if __name__ == "__main__":
    main()
