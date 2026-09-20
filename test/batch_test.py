#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
batch_test.py - 批量测试脚本
一次性运行所有 24 个测试场景，全部 5 秒视频
"""

import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from core.config import DEFAULT_DURATION, MATERIAL_DIR, KOL_DIR
from utils.excel_utils import load_tasks, update_row, COL_STATUS, COL_PRODUCT, COL_PROMPT
from registry.manager import REG
from workers.submit import do_submit
from console.app import select_product_and_kol
from core.logger import Ctx, raw_info, raw_warning


# ============================================================
# 测试配置
# ============================================================

# 设置全局变量
selected_product = "诺特兰德益生菌"
selected_kol = None

# 素材路径（从 test 文件夹读取）
MATERIAL_ROOT = "/Users/leiliang/PycharmProjects/aigc/test"
PRODUCT_IMG = f"{MATERIAL_ROOT}/product.png"
KOL_DIR = f"{MATERIAL_ROOT}/kol"
VIDEO_DIR = f"{MATERIAL_ROOT}/videos"
AUDIO_DIR = f"{MATERIAL_ROOT}/audios"

# 统一台词
DIALOGUE_CN = "大家好，我是贾乃亮。这款产品非常好用！"
DIALOGUE_EN = "Hello everyone, I'm Jacky. This product is very good!"


# ============================================================
# 测试脚本定义
# ============================================================

TEST_SCRIPTS = [
    # Group 1: 产品 + KOL (1+1)
    {"name": "1.1 产品 + kol_1.png",
     "kol_img": f"{KOL_DIR}/kol_1.png",
     "subject": "<Subject 1> is a celebrity endorsement scene featuring the product. The celebrity should hold the product naturally and speak directly to camera with confident expression.",
     "duration": 5},
    {"name": "1.2 产品 + kol_2.png",
     "kol_img": f"{KOL_DIR}/kol_2.png",
     "subject": "<Subject 1> is a celebrity endorsement scene featuring the product. The celebrity should present the product professionally with enthusiastic tone.",
     "duration": 5},
    {"name": "1.3 产品 + kol_3.png",
     "kol_img": f"{KOL_DIR}/kol_3.png",
     "subject": "<Subject 1> is a celebrity testimonial scene. The celebrity holds the product close to camera and speaks with genuine satisfaction.",
     "duration": 5},
    
    # Group 2: 产品 + 视频 (1+1)
    {"name": "2.1 产品 + videos/video_1.mp4",
     "video_file": f"{VIDEO_DIR}/video_1.mp4",
     "subject": "<Subject 1> is a product demonstration scene with dynamic video background. The product is prominently displayed while the video shows usage scenarios.",
     "duration": 5},
    {"name": "2.2 产品 + videos/video_2.mp4",
     "video_file": f"{VIDEO_DIR}/video_2.mp4",
     "subject": "<Subject 1> is a product tutorial scene. The product appears in foreground while video demonstrates step-by-step usage.",
     "duration": 5},
    {"name": "2.3 产品 + videos/video_3.mp4",
     "video_file": f"{VIDEO_DIR}/video_3.mp4",
     "subject": "<Subject 1> is a product feature showcase with video clips. Product is highlighted while video shows key features in action.",
     "duration": 5},
    
    # Group 3: 产品 + 音频 (1+1)
    {"name": "3.1 产品 + audios/audio_1.mp3",
     "audio_file": f"{AUDIO_DIR}/audio_1.mp3",
     "subject": "<Subject 1> is a product advertisement with voiceover. The product is shown prominently while audio provides commentary.",
     "duration": 5},
    {"name": "3.2 产品 + audios/audio_2.mp3",
     "audio_file": f"{AUDIO_DIR}/audio_2.mp3",
     "subject": "<Subject 1> is a testimonial video with product focus. Product is displayed while audio shares user experience.",
     "duration": 5},
    {"name": "3.3 产品 + audios/audio_3.mp3",
     "audio_file": f"{AUDIO_DIR}/audio_3.mp3",
     "subject": "<Subject 1> is a promotional video featuring the product. Product is highlighted while audio delivers special offer message.",
     "duration": 5},
    
    # Group 4: 产品 + KOL + KOL (1+2)
    {"name": "4.1 产品 + kol_1.png + kol_2.png",
     "kols_img": [f"{KOL_DIR}/kol_1.png", f"{KOL_DIR}/kol_2.png"],
     "subject": "<Subject 1> is a joint celebrity endorsement scene with two celebrities. Both should appear naturally and discuss the product together.",
     "duration": 5},
    {"name": "4.2 产品 + kol_2.png + kol_3.png",
     "kols_img": [f"{KOL_DIR}/kol_2.png", f"{KOL_DIR}/kol_3.png"],
     "subject": "<Subject 1> is a team recommendation scene with multiple celebrities. Celebrities interact naturally while promoting the product.",
     "duration": 5},
    {"name": "4.3 产品 + kol_1.png + kol_3.png",
     "kols_img": [f"{KOL_DIR}/kol_1.png", f"{KOL_DIR}/kol_3.png"],
     "subject": "<Subject 1> is a celebrity duet endorsement. Two celebrities share their experience with the product in a conversational manner.",
     "duration": 5},
    
    # Group 5: 产品 + 视频 + 视频 (1+2)
    {"name": "5.1 产品 + video_1.mp4 + video_2.mp4",
     "videos_file": [f"{VIDEO_DIR}/video_1.mp4", f"{VIDEO_DIR}/video_2.mp4"],
     "subject": "<Subject 1> is a multi-scene product showcase with dynamic video backgrounds. Product remains prominent while videos show different usage scenarios.",
     "duration": 5},
    {"name": "5.2 产品 + video_2.mp4 + video_3.mp4",
     "videos_file": [f"{VIDEO_DIR}/video_2.mp4", f"{VIDEO_DIR}/video_3.mp4"],
     "subject": "<Subject 1> is a comprehensive tutorial with multiple video clips. Product is featured throughout while videos demonstrate various aspects.",
     "duration": 5},
    {"name": "5.3 产品 + video_1.mp4 + video_3.mp4",
     "videos_file": [f"{VIDEO_DIR}/video_1.mp4", f"{VIDEO_DIR}/video_3.mp4"],
     "subject": "<Subject 1> is a highlight reel featuring the product. Product is showcased alongside key moments from multiple videos.",
     "duration": 5},
    
    # Group 6: 产品 + 音频 + 音频 (1+2)
    {"name": "6.1 产品 + audio_1.mp3 + audio_2.mp3",
     "audios_file": [f"{AUDIO_DIR}/audio_1.mp3", f"{AUDIO_DIR}/audio_2.mp3"],
     "subject": "<Subject 1> is a dual voiceover presentation with product focus. Product is displayed prominently while two audio tracks provide complementary information.",
     "duration": 5},
    {"name": "6.2 产品 + audio_2.mp3 + audio_3.mp3",
     "audios_file": [f"{AUDIO_DIR}/audio_2.mp3", f"{AUDIO_DIR}/audio_3.mp3"],
     "subject": "<Subject 1> is a testimonial combo with product showcase. Product remains visible while audio combines market feedback and promotion.",
     "duration": 5},
    {"name": "6.3 产品 + audio_1.mp3 + audio_3.mp3",
     "audios_file": [f"{AUDIO_DIR}/audio_1.mp3", f"{AUDIO_DIR}/audio_3.mp3"],
     "subject": "<Subject 1> is a mixed media ad featuring the product. Product is highlighted while audio delivers benefits message followed by purchase guide.",
     "duration": 5},
    
    # Group 7: 产品 + KOL + 视频 (1+1+1)
    {"name": "7.1 产品 + kol_1.png + video_1.mp4",
     "kol_img": f"{KOL_DIR}/kol_1.png",
     "video_file": f"{VIDEO_DIR}/video_1.mp4",
     "subject": "<Subject 1> is a celebrity video endorsement with product focus. Celebrity appears alongside product while video demonstrates usage.",
     "duration": 5},
    {"name": "7.2 产品 + kol_2.png + video_2.mp4",
     "kol_img": f"{KOL_DIR}/kol_2.png",
     "video_file": f"{VIDEO_DIR}/video_2.mp4",
     "subject": "<Subject 1> is a tutorial with celebrity and product showcase. Celebrity guides viewers through product usage with visual demonstration.",
     "duration": 5},
    {"name": "7.3 产品 + kol_3.png + video_3.mp4",
     "kol_img": f"{KOL_DIR}/kol_3.png",
     "video_file": f"{VIDEO_DIR}/video_3.mp4",
     "subject": "<Subject 1> is a feature showcase with star and product. Celebrity explains key features while video shows them in action.",
     "duration": 5},
    
    # Group 8: 产品 + KOL + 音频 (1+1+1)
    {"name": "8.1 产品 + kol_1.png + audio_1.mp3",
     "kol_img": f"{KOL_DIR}/kol_1.png",
     "audio_file": f"{AUDIO_DIR}/audio_1.mp3",
     "subject": "<Subject 1> is a celebrity voiceover ad with product focus. Celebrity appears on screen while audio provides promotional message.",
     "duration": 5},
    {"name": "8.2 产品 + kol_2.png + audio_2.mp3",
     "kol_img": f"{KOL_DIR}/kol_2.png",
     "audio_file": f"{AUDIO_DIR}/audio_2.mp3",
     "subject": "<Subject 1> is a testimonial with celebrity and audio. Celebrity shares experience while audio reinforces key message.",
     "duration": 5},
    {"name": "8.3 产品 + kol_3.png + audio_3.mp3",
     "kol_img": f"{KOL_DIR}/kol_3.png",
     "audio_file": f"{AUDIO_DIR}/audio_3.mp3",
     "subject": "<Subject 1> is a promotional spot with star and product. Celebrity delivers special offer message synchronized with audio.",
     "duration": 5},
]


# ============================================================
# 核心功能
# ============================================================

def build_prompt(test_case):
    """构建提示词"""
    parts = []
    
    # 添加主体描述
    parts.append(test_case["subject"])
    
    # 添加时长
    parts.append(f"Duration: {test_case['duration']} seconds.")
    
    # 添加对话
    parts.append("dialogue:")
    parts.append(f"<d>[Chinese] {DIALOGUE_CN}</d>")
    parts.append(f"<d>[English] {DIALOGUE_EN}</d>")
    
    return "\n\n".join(parts)


def run_test(index, test_case):
    """运行单个测试"""
    test_name = test_case["name"]
    duration = test_case.get("duration", 5)
    
    # 构建提示词
    prompt = build_prompt(test_case)
    
    # 创建行数据（从第 25 行开始）
    row_idx = index  # 0-based index
    
    print(f"\n{'='*60}")
    print(f"测试 {index+1}/{len(TEST_SCRIPTS)}: {test_name}")
    print(f"{'='*60}")
    print(f"时长：{duration}秒")
    print(f"品名：{selected_product}")
    
    if test_case.get("kol_img"):
        print(f"KOL: {test_case['kol_img']}")
    if test_case.get("kols_img"):
        print(f"KOLs: {', '.join(test_case['kols_img'])}")
    if test_case.get("video_file"):
        print(f"视频：{test_case['video_file']}")
    if test_case.get("videos_file"):
        print(f"视频：{', '.join(test_case['videos_file'])}")
    if test_case.get("audio_file"):
        print(f"音频：{test_case['audio_file']}")
    if test_case.get("audios_file"):
        print(f"音频：{', '.join(test_case['audios_file'])}")
    
    # 提交任务
    ctx = Ctx(row=row_idx, account="batch_test", job_id="")
    try:
        jid, err, acc_name = do_submit(row_idx, selected_product, prompt)
        
        if jid:
            print(f"✅ 提交成功 [{acc_name}] job_id={jid[:8]}...")
            return True
        else:
            print(f"❌ 提交失败：{err}")
            return False
            
    except Exception as e:
        raw_warning(f"测试 {test_name} 异常：{e}")
        return False


def main():
    """主函数"""
    print("\n" + "="*60)
    print("AIGC 批量测试脚本")
    print("运行所有 24 个测试场景，全部 5 秒视频")
    print("="*60)
    
    # 显示素材路径
    print(f"\n素材根目录：{MATERIAL_ROOT}")
    print(f"产品图：{PRODUCT_IMG}")
    print(f"KOL 目录：{KOL_DIR}")
    print(f"视频目录：{VIDEO_DIR}")
    print(f"音频目录：{AUDIO_DIR}")
    
    # 确认执行
    print(f"\n准备运行 {len(TEST_SCRIPTS)} 个测试")
    
    confirm = input("\n确认执行？[y/N]: ").strip().lower()
    if confirm != "y":
        print("已取消")
        return
    
    # 运行测试
    success_count = 0
    fail_count = 0
    
    for i, test_case in enumerate(TEST_SCRIPTS):
        print(f"\n[{i+1}/{len(TEST_SCRIPTS)}] {test_case['name']}")
        
        if run_test(i, test_case):
            success_count += 1
        else:
            fail_count += 1
        
        # 短暂延迟，避免请求过快
        import time
        time.sleep(1)
    
    # 统计结果
    print("\n" + "="*60)
    print("测试完成!")
    print(f"成功：{success_count}个")
    print(f"失败：{fail_count}个")
    print(f"总计：{len(TEST_SCRIPTS)}个")
    print("="*60)
    
    if fail_count > 0:
        print(f"\n⚠️ 有 {fail_count} 个测试失败，请查看日志")


if __name__ == "__main__":
    main()
