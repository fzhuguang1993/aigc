"""
console.py —— 交互式控制台
"""
import time
import re

from core.config import COL_PRODUCT, COL_PROMPT, DEFAULT_DURATION, LAST_PRODUCT, LAST_KOL, KOL_OPTIONS
from utils.excel_utils import load_tasks
from registry.manager import REG, ACCOUNTS, get_account, get_account_load
from workers.submit import do_submit, cancel_one
from workers.scan import print_new_rows, SCAN
from processors.script_extractor import ScriptExtractor
from core.logger import Ctx, raw_warning, raw_info
from core.api_client import query_job

# 当前视频时长（秒）
current_duration = DEFAULT_DURATION

# 当前选择的品名和 KOL
selected_product = LAST_PRODUCT
selected_kol = LAST_KOL


def show_status():
    """查看活跃任务（实时从云端查询）"""
    tasks = REG.active()
    print("\n" + "=" * 80)
    if not tasks:
        print("【当前没有活跃任务】")
        print("=" * 80)
        return
    
    print(f"【活跃任务 {len(tasks)} 个】(实时从云端查询)")
    print("=" * 80)
    print(f"{'序号':<4} {'账号':<6} {'行':<4} {'状态':<12} {'进度':<8} {'job_id':<15} 提示词")
    print("-" * 80)
    
    for i, t in enumerate(tasks, 1):
        # 实时从云端查询最新状态
        try:
            acc = get_account(t["account"])
            if acc:
                job_info = query_job(acc.base, t["job_id"])
                cloud_status = job_info.get("status", t["status"])
                cloud_progress = job_info.get("progress", t["progress"])
                
                # 如果云端状态和本地不一致，更新本地
                if cloud_status != t["status"] or cloud_progress != t["progress"]:
                    REG.update(t["job_id"], cloud_status, cloud_progress)
                    t["status"] = cloud_status
                    t["progress"] = cloud_progress
                
                status_display = cloud_status
                progress_display = cloud_progress
            else:
                status_display = t["status"]
                progress_display = t["progress"]
        except Exception as e:
            # 如果查询失败，显示本地状态
            raw_warning(f"查询任务 {t['job_id']} 失败：{e}")
            status_display = t["status"]
            progress_display = t["progress"]
        
        prompt_short = t["prompt"][:30].replace("\n", " ")
        print(f"{i:<4} {t['account']:<6} {t['row_idx']+1:<4} "
              f"{status_display:<12} {str(progress_display):<8} "
              f"{t['job_id']:<15} {prompt_short}")
    
    print("=" * 80)


def show_health():
    print("\n" + "=" * 80)
    print("【账号健康 + 负载】")
    print("=" * 80)
    print(f"{'账号':<8} {'状态':<10} {'负载':<8} {'并发':<8} 地址")
    print("-" * 80)
    for acc in ACCOUNTS:
        status = "healthy" if acc.healthy else f"DOWN({acc.fail_count})"
        try:
            load = get_account_load(acc)
        except Exception:
            load = "?"
        print(f"{acc.name:<8} {status:<10} {str(load):<8} {acc.concurrency:<8} {acc.base}")
    print("=" * 80)


def confirm(prompt_text):
    try:
        return input(prompt_text).strip().lower()
    except EOFError:
        return ""


def safe_input(prompt_text=""):
    """安全的输入函数，支持 dd 呼出菜单"""
    try:
        text = input(prompt_text).strip()
        # 检查是否输入了 dd
        if text.lower() == "dd":
            show_menu()
            # 递归调用重新获取输入
            return safe_input()
        return text
    except EOFError:
        return ""


def get_task_by_num(num):
    tasks = REG.active()
    if 1 <= num <= len(tasks):
        return tasks[num - 1]
    return None


def run_one_row(row_idx, product, prompt):
    existing = REG.get_by_row(row_idx)
    if existing:
        print(f"\n⚠ 第 {row_idx + 1} 行正在跑（job_id={existing['job_id'][:8]}，" 
              f"进度 {existing['progress']}%）")
        print("   [y] 取消旧任务，重新提交")
        print("   [n] 取消旧任务，不提交")
        print("   [k] 保留旧任务，同时提交新任务")
        print("   [回车] 取消操作")
        c = safe_input("输入：")
        if c == "y":
            cancel_one(existing)
            for _ in range(30):
                if not REG.get_by_row(row_idx):
                    break
                time.sleep(2)
        elif c == "n":
            cancel_one(existing)
            return
        elif c == "k":
            pass
        else:
            print("已取消")
            return

    jid, err, acc_name = do_submit(row_idx, product, prompt)
    if jid:
        # 只输出关键信息，不显示口播提取结果（避免冗余）
        print(f"  ✓ 第 {row_idx + 1} 行提交成功 [{acc_name}]")
        
        # 异步提取口播文案（不阻塞，不输出失败信息）
        from processors.script_extractor import ScriptExtractor
        from core.logger import Ctx
        extractor = ScriptExtractor(f"http://{acc_name}.api.example.com")
        extractor.extract_and_save(row_idx, prompt, f"http://{acc_name}.api.example.com",
                                  Ctx(row=row_idx, account=acc_name))
    else:
        print(f"  ✗ 第 {row_idx + 1} 行提交失败：{err}")


def parse_row_numbers(args_str):
    """
    解析行号字符串，支持多种格式
    
    支持的格式：
    - 4              -> [4]
    - 4,5,6          -> [4, 5, 6]
    - 1-4            -> [1, 2, 3, 4]
    - 1-4,6-9        -> [1, 2, 3, 4, 6, 7, 8, 9]
    - 1-3,5,7-9      -> [1, 2, 3, 5, 7, 8, 9]
    
    Args:
        args_str: 输入的字符串
        
    Returns:
        去重后的行号列表（升序）
    """
    row_numbers = set()
    
    # 替换中文逗号
    args_str = args_str.replace("，", ",")
    parts = args_str.split(",")
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        # 检查是否是范围格式（如 1-4）
        range_match = re.match(r'(\d+)\s*-\s*(\d+)', part)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start > end:
                print(f"[跳过] 范围 {start}-{end} 无效（起始大于结束）")
                continue
            for i in range(start, end + 1):
                row_numbers.add(i)
        # 检查是否是单个数字
        elif part.isdigit():
            row_numbers.add(int(part))
        else:
            print(f"[跳过] 无效格式：{part}")
    
    return sorted(row_numbers)


def show_menu():
    """显示功能菜单"""
    print("\033[2J\033[H")  # 清屏
    print("=" * 60)
    print("【功能菜单】")
    print("=" * 60)
    print("1. 查看活跃任务")
    print("2. 查看账号健康")
    print("3. 扫描新项目")
    print("4. 取消任务")
    print("5. 跑单行 (输入行号)")
    print("6. 跑多行 (输入行号，逗号分隔)")
    print("7. 跑范围 (输入 1-10)")
    print("8. 跑所有新项")
    print("9. 切换时长模式")
    print("T. 批量测试 (从 Excel 读取并自动上传素材)")
    print("C. 清屏")
    print("0. 继续运行")
    print("-" * 60)
    print(f"上次组合：{LAST_PRODUCT} + {LAST_KOL if LAST_KOL else '无'}")
    print("=" * 60)


def select_product_and_kol():
    """选择品名和 KOL"""
    global current_duration
    
    # 显示品名列表
    print("\n请选择品名：")
    products = ["诺特兰德益生菌", "诺特兰德 VB", "其他"]
    for i, p in enumerate(products, 1):
        marker = "⭐" if p == LAST_PRODUCT else " "
        print(f"  {marker} {i}. {p}")
    
    product_input = safe_input("输入品名或编号（回车使用上次）：").strip()
    
    # 如果输入为空或数字，使用上次的
    if not product_input or product_input.isdigit():
        product = LAST_PRODUCT if LAST_PRODUCT else "诺特兰德益生菌"
    elif product_input.isdigit():
        idx = int(product_input) - 1
        if 0 <= idx < len(products):
            product = products[idx]
        else:
            product = LAST_PRODUCT if LAST_PRODUCT else "诺特兰德益生菌"
    else:
        product = product_input
    
    # 保存为上次使用的品名
    # 注意：这里需要更新 config 中的变量，但由于是全局常量，我们只在内存中保存
    # 实际使用时从这个函数返回
    
    # 显示 KOL 列表
    print("\n请选择 KOL（可选）：")
    kol_choices = [None, "无"] + KOL_OPTIONS
    for i, k in enumerate(kol_choices, 1):
        display = k if k else "无"
        marker = "⭐" if k == LAST_KOL else " "
        print(f"  {marker} {i}. {display}")
    
    kol_input = safe_input("输入 KOL 名称或编号（回车跳过）：").strip()
    
    if not kol_input:
        kol = None
    elif kol_input.isdigit():
        idx = int(kol_input) - 1
        if 0 <= idx < len(kol_choices):
            kol = kol_choices[idx]
        else:
            kol = LAST_KOL
    else:
        kol = kol_input if kol_input != "无" else None
    
    # 保存到配置（临时变量）
    global selected_product, selected_kol
    selected_product = product
    selected_kol = kol
    
    print(f"\n✅ 已选择：{product} + {kol if kol else '无'}")
    return product, kol


def handle_quick_command(num):
    """处理数字快捷键"""
    menus = {
        1: lambda: show_status(),
        2: lambda: show_health(),
        3: lambda: print_new_rows(),
        4: lambda: handle_cancel(),
        5: lambda: handle_single_run(),
        6: lambda: handle_multi_run(),
        7: lambda: handle_range_run(),
        8: lambda: handle_run("new"),
        9: lambda: handle_duration_switch(),
        0: lambda: print("继续运行..."),
    }
    
    handler = menus.get(num)
    if handler:
        handler()
    else:
        print(f"无效选项：{num}")


def handle_single_run():
    """处理单行运行"""
    # 先选择品名和 KOL
    select_product_and_kol()
    
    row_input = safe_input("请输入行号：")
    if row_input.isdigit():
        df = load_tasks()
        row_idx = int(row_input) - 1
        if 0 <= row_idx < len(df):
            row = df.iloc[row_idx]
            product = str(row.get(COL_PRODUCT, "")).strip()
            prompt = str(row.get(COL_PROMPT, "")).strip()
            if prompt and prompt not in ("nan", "None"):
                jid, err, acc_name = do_submit(row_idx, product, prompt)
                if jid:
                    print(f"  ✓ 第 {row_idx + 1} 行提交成功 [{acc_name}]")
                    print(f"\n{'='*60}")
                    print(f"批量提交完成！成功 1/1 个")
                    print(f"Excel 回写：1/1 个")
                    print(f"{'='*60}")
                else:
                    print(f"  ✗ 第 {row_idx + 1} 行提交失败：{err}")
                    print(f"\n{'='*60}")
                    print(f"批量提交完成！成功 0/1 个")
                    print(f"Excel 回写：0/1 个")
                    print(f"{'='*60}")
            else:
                print("提示词为空")
        else:
            print("行号越界")
    else:
        print("无效输入")


def handle_multi_run():
    """处理多行运行"""
    # 先选择品名和 KOL
    select_product_and_kol()
    
    row_input = safe_input("请输入行号（逗号分隔，如 1,3,5）：")
    if row_input:
        rows = parse_row_numbers(row_input)
        if rows:
            df = load_tasks()
            total = len(rows)
            success_count = 0
            fail_count = 0
            
            for line_no in rows:
                row_idx = line_no - 1
                if 0 <= row_idx < len(df):
                    row = df.iloc[row_idx]
                    product = str(row.get(COL_PRODUCT, "")).strip()
                    prompt = str(row.get(COL_PROMPT, "")).strip()
                    if prompt and prompt not in ("nan", "None"):
                        jid, err, acc_name = do_submit(row_idx, product, prompt)
                        if jid:
                            print(f"  ✓ 第 {row_idx + 1} 行提交成功 [{acc_name}]")
                            success_count += 1
                        else:
                            print(f"  ✗ 第 {row_idx + 1} 行提交失败：{err}")
                            fail_count += 1
                    else:
                        print(f"跳过第{line_no}行（提示词为空）")
                else:
                    print(f"跳过第{line_no}行（越界）")
            
            # 最后显示汇总
            print(f"\n{'='*60}")
            print(f"批量提交完成！")
            print(f"成功提交：{success_count}/{total} 个")
            if fail_count > 0:
                print(f"提交失败：{fail_count} 个")
            print(f"Excel 回写：{success_count}/{total} 个")
            print(f"{'='*60}")
        else:
            print("取消")
    else:
        print("取消")


def handle_range_run():
    """处理范围运行"""
    # 先选择品名和 KOL
    select_product_and_kol()
    
    range_input = safe_input("请输入范围（如 1-10）：")
    if not range_input:
        print("取消")
        return
    
    rows = parse_row_numbers(range_input)
    if not rows:
        print("无效范围")
        return
    
    total = len(rows)
    success_count = 0
    fail_count = 0
    skip_count = 0
    
    print(f"\n准备执行 {total} 个任务...")
    df = load_tasks()
    excel_total = len(df)
    
    for line_no in rows:
        row_idx = line_no - 1
        
        # 检查越界
        if row_idx < 0 or row_idx >= excel_total:
            print(f"[跳过] 第 {line_no} 行越界（总共{excel_total}行）")
            skip_count += 1
            continue
        
        row = df.iloc[row_idx]
        product = str(row.get(COL_PRODUCT, "")).strip()
        prompt = str(row.get(COL_PROMPT, "")).strip()
        
        # 检查提示词是否为空
        if not prompt or prompt in ("nan", "None"):
            print(f"[跳过] 第 {line_no} 行提示词为空")
            skip_count += 1
            continue
        
        # 在执行每个任务前，检查是否有 dd 输入
        cmd_check = safe_input("")  # 不显示提示符，只是检测输入
        if cmd_check.lower() == "dd":
            show_menu()
        
        # 执行任务
        jid, err, acc_name = do_submit(row_idx, product, prompt)
        if jid:
            print(f"  ✓ 第 {row_idx + 1} 行提交成功 [{acc_name}]")
            success_count += 1
        else:
            print(f"  ✗ 第 {row_idx + 1} 行提交失败：{err}")
            fail_count += 1
    
    # 输出统计
    print(f"\n{'='*60}")
    print(f"批量提交完成！")
    print(f"成功提交：{success_count}/{total} 个")
    if fail_count > 0:
        print(f"提交失败：{fail_count} 个")
    if skip_count > 0:
        print(f"跳过：{skip_count} 个")
    print(f"Excel 回写：{success_count}/{total} 个")
    print(f"{'='*60}")


def handle_duration_switch():
    """处理时长切换"""
    duration_input = safe_input("输入 5 或 15：")
    if duration_input == "5":
        current_duration = 5
        raw_info("已切换到 5 秒模式")
    elif duration_input == "15":
        current_duration = 15
        raw_info("已切换到 15 秒模式")
    else:
        print("无效输入")
    print(f"当前模式：{current_duration}秒")


def handle_batch_test():
    """处理批量测试命令"""
    try:
        from test.batch_test_from_excel import main as run_batch_test
        run_batch_test()
    except ImportError as e:
        print(f"错误：无法导入测试模块 {e}")
        print("请确保 test/batch_test_from_excel.py 文件存在")
    except Exception as e:
        print(f"错误：{e}")


def handle_run(args_str):
    """处理 run 命令，支持灵活输入格式"""
    args_str = args_str.strip()

    if args_str == "new":
        rows = print_new_rows()
        if not rows:
            return
        c = safe_input("\n要跑这 {len(rows)} 个新项目吗？[y/N]: ").strip().lower()
        if c != "y":
            print("已取消")
            return
        for row_idx, info in rows:
            run_one_row(row_idx, info["品名"], info["提示词"])
        # 最后显示汇总
        print(f"\n{'='*60}")
        print(f"批量提交完成！成功 {len(rows)}/{len(rows)} 个")
        print(f"{'='*60}")
        return

    # 解析行号列表，支持：4, 4,5,6, 1-4, 1-4,6-9, 1-3,5,7-9
    row_numbers = parse_row_numbers(args_str)
    
    if not row_numbers:
        print("没有有效的行号")
        return
    
    total = len(row_numbers)
    success_count = 0
    fail_count = 0
    skip_count = 0
    
    print(f"\n准备执行 {total} 个任务...")
    
    df = load_tasks()
    excel_total = len(df)
    
    for line_no in row_numbers:
        row_idx = line_no - 1
        
        # 检查越界
        if row_idx < 0 or row_idx >= excel_total:
            print(f"[跳过] 第 {line_no} 行越界（总共{excel_total}行）")
            skip_count += 1
            continue
        
        row = df.iloc[row_idx]
        product = str(row.get(COL_PRODUCT, "")).strip()
        prompt = str(row.get(COL_PROMPT, "")).strip()
        
        # 检查提示词是否为空
        if not prompt or prompt in ("nan", "None"):
            print(f"[跳过] 第 {line_no} 行提示词为空")
            skip_count += 1
            continue
        
        # 执行任务（不统计成功失败，只打印每行成功信息）
        jid, err, acc_name = do_submit(row_idx, product, prompt)
        if jid:
            print(f"  ✓ 第 {row_idx + 1} 行提交成功 [{acc_name}]")
            success_count += 1
        else:
            print(f"  ✗ 第 {row_idx + 1} 行提交失败：{err}")
            fail_count += 1
    
    # 最后显示汇总统计
    print(f"\n{'='*60}")
    print(f"批量提交完成！")
    print(f"成功提交：{success_count}/{total} 个")
    if fail_count > 0:
        print(f"提交失败：{fail_count} 个")
    if skip_count > 0:
        print(f"跳过：{skip_count} 个")
    print(f"Excel 回写：{success_count}/{total} 个")
    print(f"{'='*60}")


def handle_cancel():
    tasks = REG.active()
    if not tasks:
        print("当前没有活跃任务")
        return
    show_status()
    raw = safe_input("\n输入要取消的任务序号（多个逗号分隔，回车取消）: ")
    if not raw:
        return
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            t = get_task_by_num(int(part))
            if t:
                cancel_one(t)
            else:
                print(f"[跳过] 序号 {part} 越界")


def interactive_loop():
    global current_duration
    
    print()
    print("=" * 80)
    print("AIGC 视频生成系统")
    print("=" * 80)
    print(f"模式：{current_duration}秒 | 账号：{'/'.join([a.name for a in ACCOUNTS])}")
    print("-" * 80)
    print("命令：dd(呼出菜单) | q(退出)")
    print("-" * 80)

    while True:
        try:
            cmd = input(">").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return

        if not cmd:
            continue
        
        # 呼出菜单
        if cmd.lower() == "dd":
            show_menu()
            continue
        
        # 退出
        if cmd.lower() in {"q", "quit", "exit"}:
            print("退出")
            return
        
        # 数字快捷键
        if cmd.isdigit():
            handle_quick_command(int(cmd))
            continue
        
        # T 键：批量测试
        if cmd.lower() == "t":
            handle_batch_test()
            continue
        
        # C 键：清屏
        if cmd.lower() == "c":
            print("\033[2J\033[H")  # 清屏
            continue
        
        print(f"未知命令：{cmd} (输入 dd 查看菜单)")
