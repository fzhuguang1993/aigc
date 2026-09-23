"""
console/app.py —— 交互式控制台（黑窗口模式）
数据源已切换为 SQLite（store.task_store），Excel 仅作导入/导出格式。
注意：本文件中所有"任务ID"均为数据库主键 id（替代原 Excel 行号）。
"""
import re
import time

from core.config import (COL_PRODUCT, COL_PROMPT, DEFAULT_DURATION,
                         LAST_PRODUCT, LAST_KOL, KOL_OPTIONS,
                         SUBMIT_PACING, BALANCE_RESCAN_EVERY)
from store import db, task_store
from store.task_store import COL_ID, COL_STATUS, COL_RUNS
from registry.manager import (REG, ACCOUNTS, get_account, get_account_load,
                              BatchBalancer, check_all_accounts, first_check_done)
from workers.submit import do_submit, cancel_one, SubmitOptions
from workers.scan import print_new_rows, SCAN
from core.logger import raw_warning, raw_info
from core.api_client import query_job

# 当前视频时长（秒）——控制台交互层自身的状态，提交时通过 SubmitOptions 显式传入
current_duration = DEFAULT_DURATION

# 当前选择的品名和 KOL
selected_product = LAST_PRODUCT
selected_kol = LAST_KOL


def _current_options():
    """提交时快照控制台当前选择（仅限单线程控制台使用）"""
    return SubmitOptions(duration=current_duration, kol=selected_kol)


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
    print(f"{'序号':<4} {'账号':<6} {'任务':<6} {'状态':<12} {'进度':<8} {'job_id':<15} 提示词")
    print("-" * 80)

    for i, t in enumerate(tasks, 1):
        try:
            acc = get_account(t["account"])
            if acc:
                job_info = query_job(acc.base, t["job_id"])
                cloud_status = job_info.get("status", t["status"])
                cloud_progress = job_info.get("progress", t["progress"])
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
            raw_warning(f"查询任务 {t['job_id']} 失败：{e}")
            status_display = t["status"]
            progress_display = t["progress"]

        prompt_short = t["prompt"][:30].replace("\n", " ")
        print(f"{i:<4} {t['account']:<6} {t['row_idx']:<6} "
              f"{status_display:<12} {str(progress_display):<8} "
              f"{t['job_id']:<15} {prompt_short}")

    print("=" * 80)


def show_health():
    print("\n" + "=" * 80)
    print("【账号健康 + 负载】")
    print("=" * 80)
    if not first_check_done():
        # 默认 healthy=True 只是「还没测」，命令行里当场测一轮再报
        print("（首次查看，正在逐条真实探活…）")
        check_all_accounts()
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
        if text.lower() == "dd":
            show_menu()
            return safe_input()
        return text
    except EOFError:
        return ""


def get_task_by_num(num):
    tasks = REG.active()
    if 1 <= num <= len(tasks):
        return tasks[num - 1]
    return None


def run_one_row(task_id, product, prompt):
    existing = REG.get_by_row(task_id)
    if existing:
        print(f"\n⚠ 任务 {task_id} 正在跑（job_id={existing['job_id'][:8]}，"
              f"进度 {existing['progress']}%）")
        print("   [y] 取消旧任务，重新提交")
        print("   [n] 取消旧任务，不提交")
        print("   [k] 保留旧任务，同时提交新任务")
        print("   [回车] 取消操作")
        c = safe_input("输入：")
        if c == "y":
            cancel_one(existing)
            for _ in range(30):
                if not REG.get_by_row(task_id):
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

    jid, err, acc_name = do_submit(task_id, product, prompt, _current_options())
    if jid:
        print(f"  ✓ 任务 {task_id} 提交成功 [{acc_name}]")
    else:
        print(f"  ✗ 任务 {task_id} 提交失败：{err}")


def parse_row_numbers(args_str):
    """
    解析任务ID字符串，支持多种格式：
    - 4 → [4]；4,5,6 → [4,5,6]；1-4 → [1,2,3,4]；1-4,6-9 → 混合
    """
    row_numbers = set()
    args_str = args_str.replace("，", ",")
    parts = args_str.split(",")

    for part in parts:
        part = part.strip()
        if not part:
            continue
        range_match = re.match(r'(\d+)\s*-\s*(\d+)', part)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start > end:
                print(f"[跳过] 范围 {start}-{end} 无效（起始大于结束）")
                continue
            for i in range(start, end + 1):
                row_numbers.add(i)
        elif part.isdigit():
            row_numbers.add(int(part))
        else:
            print(f"[跳过] 无效格式：{part}")

    return sorted(row_numbers)


def _submit_by_ids(id_list):
    """按任务ID列表提交，返回 (成功数, 失败数, 跳过数)。
    多条时走注水式批量分配（绕开在途闸门、按全局快照均衡）；单条保持闸门旧行为。"""
    success = fail = skip = 0
    balancer = (BatchBalancer(len(id_list), rescan_every=BALANCE_RESCAN_EVERY)
                if len(id_list) > 1 else None)
    submitted = 0
    for tid in id_list:
        task = task_store.get_task(tid)
        if not task:
            print(f"[跳过] 任务 {tid} 不存在")
            skip += 1
            continue
        prompt = str(task.get(COL_PROMPT) or task["prompt"] or "").strip()
        if not prompt or prompt in ("nan", "None"):
            print(f"[跳过] 任务 {tid} 提示词为空")
            skip += 1
            continue
        if balancer and submitted:
            time.sleep(SUBMIT_PACING)   # 批量推送小间隔，仅防连发
        submitted += 1
        product = str(task.get(COL_PRODUCT) or task["product"] or "").strip()
        jid, err, acc_name = do_submit(tid, product, prompt, _current_options(),
                                       balancer=balancer)
        if jid:
            print(f"  ✓ 任务 {tid} 提交成功 [{acc_name}]")
            success += 1
        else:
            print(f"  ✗ 任务 {tid} 提交失败：{err}")
            fail += 1
    return success, fail, skip


def _summary(success, fail, skip):
    total = success + fail + skip
    print(f"\n{'=' * 60}")
    print(f"批量提交完成！总计 {total} 个")
    print(f"成功提交：{success} 个")
    if fail:
        print(f"提交失败：{fail} 个")
    if skip:
        print(f"跳过：{skip} 个")
    print(f"记录已写入数据库，可在 GUI「执行记录」页查看")
    print(f"{'=' * 60}")


def handle_single_run():
    """跑单个任务"""
    select_product_and_kol()
    s = safe_input("请输入任务ID：")
    if s.isdigit():
        _summary(*_submit_by_ids([int(s)]))
    else:
        print("无效输入")


def handle_multi_run():
    """跑多个任务（逗号分隔）"""
    global current_duration
    select_product_and_kol()
    s = safe_input("请输入任务ID（逗号分隔，如 1,3,5）：")
    ids = parse_row_numbers(s) if s else []
    if ids:
        _summary(*_submit_by_ids(ids))
    else:
        print("取消")


def handle_range_run():
    """跑范围任务（如 1-10）"""
    select_product_and_kol()
    s = safe_input("请输入任务ID范围（如 1-10）：")
    ids = parse_row_numbers(s) if s else []
    if not ids:
        print("无效范围")
        return
    print(f"\n准备执行 {len(ids)} 个任务...")
    _summary(*_submit_by_ids(ids))


def handle_duration_switch():
    """处理时长切换"""
    global current_duration
    duration_input = safe_input("输入 5 或 15：")
    if duration_input in ("5", "15"):
        current_duration = int(duration_input)
        raw_info(f"已切换到 {current_duration} 秒模式")
    else:
        print("无效输入")
    print(f"当前模式：{current_duration}秒")


def handle_import_excel():
    """从 Excel 导入任务到数据库"""
    from core.config import EXCEL_PATH
    path = safe_input(f"Excel 路径（回车使用默认 {EXCEL_PATH}）：").strip() or EXCEL_PATH
    try:
        n, dup, _hit_ids = task_store.import_from_excel(path)
        msg = f"✓ 已导入 {n} 条任务到数据库"
        if dup:
            msg += f"；跳过重复 {dup} 条（提示词已存在）"
        print(msg)
    except Exception as e:
        print(f"导入失败：{e}")


def handle_export():
    """导出任务表"""
    fmt = safe_input("导出格式 excel / csv / json（回车默认 excel）：").strip().lower() or "excel"
    if fmt not in ("excel", "csv", "json"):
        print("无效格式")
        return
    try:
        path = task_store.export_tasks(fmt)
        print(f"✓ 已导出：{path}")
    except Exception as e:
        print(f"导出失败：{e}")


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


def select_product_and_kol():
    """选择品名和 KOL（结果写入 workers.submit 运行时参数）"""
    global selected_product, selected_kol

    print("\n请选择品名：")
    products = ["诺特兰德益生菌", "诺特兰德 VB", "其他"]
    for i, p in enumerate(products, 1):
        marker = "⭐" if p == LAST_PRODUCT else " "
        print(f"  {marker} {i}. {p}")

    product_input = safe_input("输入品名或编号（回车使用上次）：").strip()
    if not product_input:
        product = LAST_PRODUCT or "诺特兰德益生菌"
    elif product_input.isdigit() and 1 <= int(product_input) <= len(products):
        product = products[int(product_input) - 1]
    else:
        product = product_input

    print("\n请选择 KOL（可选）：")
    kol_choices = [None] + KOL_OPTIONS
    for i, k in enumerate(kol_choices, 1):
        display = k if k else "无"
        marker = "⭐" if k == LAST_KOL else " "
        print(f"  {marker} {i}. {display}")

    kol_input = safe_input("输入 KOL 名称或编号（回车跳过）：").strip()
    if not kol_input or kol_input == "无":
        kol = None
    elif kol_input.isdigit():
        idx = int(kol_input) - 1
        kol = kol_choices[idx] if 0 <= idx < len(kol_choices) else None
    else:
        kol = kol_input

    selected_product = product
    selected_kol = kol
    print(f"\n✅ 已选择：{product} + {kol if kol else '无'}")
    return product, kol


def show_menu():
    """显示功能菜单"""
    print("\033[2J\033[H")  # 清屏
    print("=" * 60)
    print("【功能菜单】")
    print("=" * 60)
    print("1. 查看活跃任务")
    print("2. 查看账号健康")
    print("3. 扫描新任务")
    print("4. 取消任务")
    print("5. 跑单个任务 (输入任务ID)")
    print("6. 跑多个任务 (任务ID逗号分隔)")
    print("7. 跑范围 (如 1-10)")
    print("8. 跑所有新任务")
    print("9. 切换时长模式")
    print("I. 从 Excel 导入任务")
    print("E. 导出任务表 (excel/csv/json)")
    print("T. 批量测试 (从 Excel 读取并自动上传素材)")
    print("C. 清屏")
    print("0. 继续运行")
    print("-" * 60)
    print(f"上次组合：{LAST_PRODUCT} + {LAST_KOL if LAST_KOL else '无'}")
    print(f"当前时长：{current_duration}秒")
    print("=" * 60)


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


def handle_run(args_str):
    """处理 run 命令：run new / run 1-10 / run 1,3,5（数字为任务ID）"""
    args_str = args_str.strip()

    if args_str == "new":
        rows = print_new_rows()
        if not rows:
            return
        c = safe_input(f"\n要跑这 {len(rows)} 个新任务吗？[y/N]: ").strip().lower()
        if c != "y":
            print("已取消")
            return
        ids = [tid for tid, _ in rows]
        _summary(*_submit_by_ids(ids))
        return

    ids = parse_row_numbers(args_str)
    if not ids:
        print("没有有效的任务ID")
        return
    print(f"\n准备执行 {len(ids)} 个任务...")
    _summary(*_submit_by_ids(ids))


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
    db.init()

    print()
    print("=" * 80)
    print("AIGC 视频生成系统（命令行模式）")
    print("=" * 80)
    print(f"模式：{current_duration}秒 | 账号：{'/'.join([a.name for a in ACCOUNTS])}")
    print("-" * 80)
    print("命令：dd(呼出菜单) | run 1-10(按任务ID执行) | run new(所有新任务) | q(退出)")
    print("-" * 80)

    while True:
        try:
            cmd = input(">").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return

        if not cmd:
            continue

        if cmd.lower() == "dd":
            show_menu()
            continue

        if cmd.lower() in {"q", "quit", "exit"}:
            print("退出")
            return

        if cmd.lower() == "run new" or cmd.lower().startswith("run "):
            handle_run(cmd[3:])
            continue

        if cmd.isdigit():
            handle_quick_command(int(cmd))
            continue

        if cmd.lower() == "t":
            handle_batch_test()
            continue

        if cmd.lower() == "i":
            handle_import_excel()
            continue

        if cmd.lower() == "e":
            handle_export()
            continue

        if cmd.lower() == "c":
            print("\033[2J\033[H")
            continue

        print(f"未知命令：{cmd} (输入 dd 查看菜单)")
