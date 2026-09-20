"""
batch_extract_scripts.py - 批量提取所有行的口播脚本到 Excel
"""
from utils.excel_utils import load_tasks, update_row
from processors.script_extractor import extract_script_from_prompt
from core.config import COL_PROMPT, COL_SCRIPT


def batch_extract_all():
    """批量提取所有行的口播脚本"""
    
    print("=" * 80)
    print("批量提取口播脚本")
    print("=" * 80)
    
    # 1. 加载 Excel
    try:
        df = load_tasks()
        total_rows = len(df)
        print(f"\n✅ Excel 加载成功，共 {total_rows} 行")
    except Exception as e:
        print(f"\n❌ Excel 加载失败：{e}")
        return False
    
    # 2. 统计信息
    success_count = 0
    skip_count = 0
    error_count = 0
    
    # 3. 遍历每一行
    for idx in range(total_rows):
        row = df.iloc[idx]
        prompt = str(row.get(COL_PROMPT, "")).strip()
        
        # 跳过没有提示词的行
        if not prompt or prompt in ("nan", "None"):
            skip_count += 1
            print(f"\n[{idx + 1}/{total_rows}] 第 {idx + 1} 行 ⚠️ 无提示词，跳过")
            continue
        
        print(f"\n[{idx + 1}/{total_rows}] 处理第 {idx + 1} 行...")
        print(f"   提示词长度：{len(prompt)} 字符")
        
        try:
            # 4. 提取口播
            script = extract_script_from_prompt(prompt)
            
            if script:
                # 5. 写入 Excel
                update_row(idx, **{COL_SCRIPT: script})
                
                lines_count = len(script.split('\n'))
                print(f"   ✅ 提取成功：{len(script)} 字，{lines_count} 句")
                print(f"   ✅ 已写入'脚本'列 (第{idx + 1}行)")
                success_count += 1
                
                # 显示前两句作为预览
                lines = script.split('\n')
                for i, line in enumerate(lines[:2], 1):
                    preview = line[:60] + "..." if len(line) > 60 else line
                    print(f"      {i}. {preview}")
                if len(lines) > 2:
                    print(f"      ... 还有 {len(lines) - 2} 句")
            else:
                print(f"   ⚠️ 未找到可提取的口播")
                skip_count += 1
                
        except Exception as e:
            print(f"   ❌ 处理失败：{e}")
            error_count += 1
    
    # 6. 总结
    print("\n" + "=" * 80)
    print("批量处理结果汇总:")
    print(f"  - 总行数：{total_rows}")
    print(f"  - 成功提取：{success_count} 行 ✅")
    print(f"  - 跳过（无提示词）：{skip_count} 行 ⚠️")
    print(f"  - 处理失败：{error_count} 行 ❌")
    print(f"  - 成功率：{success_count/total_rows*100:.1f}%")
    print("=" * 80)
    
    return True


if __name__ == "__main__":
    batch_extract_all()
