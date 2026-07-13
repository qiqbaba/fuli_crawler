import os
import re
import sys
import sqlite3
import argparse
import random
import time
from collections import defaultdict
from datetime import datetime
from playwright.sync_api import sync_playwright

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_db_path, PDF_BASE_DIR
from utils import setup_console_utf8
from utils.metadata_parser import sanitize_filename
from utils.pdf_utils import parse_filename, clean_title_suffix, to_relative_path, generate_unique_path


def _create_browser_context(p, user_agent=None, viewport=None):
    """创建 Playwright 浏览器上下文（内联版，替代已废弃的 create_browser_context）"""
    from config import USER_AGENTS, get_crawler_proxy, is_proxy_manager_enabled
    from utils.stealth import get_browser_launch_args, apply_stealth
    launch_args = get_browser_launch_args(browser_type='chromium', headless=True)
    playwright_proxy = None
    crawler_proxy = get_crawler_proxy()
    if crawler_proxy:
        playwright_proxy = {"server": crawler_proxy}
    elif is_proxy_manager_enabled():
        try:
            from utils.proxy_manager import get_proxy_string
            proxy_url = get_proxy_string()
            if proxy_url:
                playwright_proxy = {"server": proxy_url}
        except Exception as ex:
            print(f"[!] 获取自动代理失败: {ex}")
    if playwright_proxy:
        print(f"[*] Playwright 启动代理: {playwright_proxy['server']}")
    else:
        print("[*] Playwright 未启用代理")
    browser = p.chromium.launch(headless=True, args=launch_args, proxy=playwright_proxy)
    ctx_args = {"locale": "zh-CN", "user_agent": user_agent or random.choice(USER_AGENTS)}
    ctx_args["viewport"] = viewport or {"width": 1920, "height": 1080}
    context = browser.new_context(**ctx_args)
    
    # 使用统一 stealth 模块注入伪装脚本
    apply_stealth(context)
    
    return browser, context

# =============================================================================
# PDF 维护工具合集
# 合并自: check_pdf_dates.py, fix_pdf_files.py, rebuild_missing_pdfs.py
#
# 功能:
#   1. check-dates  - 检查 PDF 文件与数据库日期的匹配情况并生成报告
#   2. fix-paths    - 将 Unknown_Year 中的 PDF 按数据库日期移到正确年份文件夹
#   3. redownload   - 重新下载体积小于 20KB 的 PDF
#   4. rebuild      - 重建缺失的 PDF 文件并路径相对化
# =============================================================================

date_regex = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ===================================================================
# 功能 1: check-dates - 检查 PDF 文件与数据库日期的匹配情况
# ===================================================================
def run_check_dates(args):
    db_path = get_db_path()
    pdf_base = PDF_BASE_DIR

    print("=" * 60)
    print(f"[*] 开始检查 PDF 文件与数据库日期的匹配情况...")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    if not os.path.exists(pdf_base):
        print(f"[-] 错误: PDF 根目录不存在: {pdf_base}")
        sys.exit(1)

    # 1. 扫描所有 PDF 物理文件
    print("[*] 正在扫描 PDF 目录...")
    pdf_files = []
    for root, dirs, files in os.walk(pdf_base):
        for f in files:
            if f.lower().endswith(".pdf"):
                pdf_files.append((f, os.path.join(root, f)))
    total_phys_files = len(pdf_files)
    print(f"[+] 扫描到 {total_phys_files} 个 PDF 物理文件。")

    # 2. 连接数据库
    print("[*] 正在加载数据库中的资源记录...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, publish_time, pdf_path, url FROM resources")
    db_rows = cursor.fetchall()
    conn.close()
    print(f"[+] 成功加载 {len(db_rows)} 条数据库记录。")

    # 3. 建立索引
    db_by_pdf_filename = defaultdict(list)
    db_by_title = defaultdict(list)
    for row in db_rows:
        r_id, r_title, r_publish_time, r_pdf_path, r_url = row
        pub_time = r_publish_time.strip() if r_publish_time else "Unknown_Date"
        if not pub_time:
            pub_time = "Unknown_Date"
        record = {"id": r_id, "title": r_title, "publish_time": pub_time,
                  "pdf_path": r_pdf_path, "url": r_url}
        if r_pdf_path:
            filename_part = os.path.basename(r_pdf_path.replace('\\', '/')).lower()
            db_by_pdf_filename[filename_part].append(record)
        if r_title:
            db_by_title[r_title.strip()].append(record)

    # 4. 比对
    print("[*] 正在比对文件与数据库...")
    results = {"matched_ok": [], "date_mismatch": [], "db_not_found": [], "multiple_conflict": []}

    for filename, full_path in pdf_files:
        fn_date, fn_title_part = parse_filename(filename)
        fn_clean_title = clean_title_suffix(fn_title_part)
        matched_records = []

        if filename.lower() in db_by_pdf_filename:
            matched_records = db_by_pdf_filename[filename.lower()]
        if not matched_records:
            if fn_title_part in db_by_title:
                matched_records = db_by_title[fn_title_part]
            elif fn_clean_title in db_by_title:
                matched_records = db_by_title[fn_clean_title]
        if not matched_records:
            results["db_not_found"].append({
                "filename": filename, "path": full_path,
                "fn_date": fn_date, "clean_title": fn_clean_title})
            continue

        unique_dates = list(set(r["publish_time"] for r in matched_records))
        if len(unique_dates) > 1:
            exact_by_path = []
            for r in matched_records:
                if r["pdf_path"]:
                    basename = os.path.basename(r["pdf_path"].replace('\\', '/')).lower()
                    if basename == filename.lower():
                        exact_by_path.append(r)
            if len(exact_by_path) == 1:
                record = exact_by_path[0]
                if fn_date == record["publish_time"]:
                    results["matched_ok"].append((filename, full_path, record))
                else:
                    results["date_mismatch"].append({
                        "filename": filename, "path": full_path,
                        "fn_date": fn_date, "db_date": record["publish_time"],
                        "record": record})
            else:
                results["multiple_conflict"].append({
                    "filename": filename, "path": full_path,
                    "fn_date": fn_date, "matched_records": matched_records})
        else:
            record = matched_records[0]
            db_date = unique_dates[0]
            cmp_fn_date = fn_date if fn_date else "Unknown_Date"
            cmp_db_date = db_date if db_date else "Unknown_Date"
            if cmp_fn_date == cmp_db_date:
                results["matched_ok"].append((filename, full_path, record))
            else:
                results["date_mismatch"].append({
                    "filename": filename, "path": full_path,
                    "fn_date": fn_date, "db_date": db_date,
                    "record": record, "all_matched_records": matched_records})

    # 5. 报告
    report_lines = [
        "# PDF 文件日期与数据库日期检查报告\n",
        f"- **检查时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **扫描 PDF 物理文件数**: {total_phys_files}",
        f"- **正常一致文件数**: {len(results['matched_ok'])}",
        f"- **日期不符文件数**: {len(results['date_mismatch'])}",
        f"- **数据库中未找到记录的文件数**: {len(results['db_not_found'])}",
        f"- **多重匹配冲突文件数**: {len(results['multiple_conflict'])}",
        "\n" + "=" * 40 + "\n"]
    if results["date_mismatch"]:
        report_lines.append("## 1. 日期不符文件列表\n")
        report_lines.append("| 序号 | 物理文件名 | 文件中提取日期 | 数据库中日期 | 数据库记录ID | 物理路径 |")
        report_lines.append("| --- | --- | --- | --- | --- | --- |")
        for i, item in enumerate(results["date_mismatch"], 1):
            rel_path = os.path.relpath(item["path"], pdf_base)
            report_lines.append(
                f"| {i} | {item['filename']} | {item['fn_date'] or '无'} | "
                f"{item['db_date']} | {item['record']['id']} | pdf/{rel_path} |")
        report_lines.append("\n")
    if results["db_not_found"]:
        report_lines.append("## 2. 数据库中未找到匹配记录的文件列表\n")
        report_lines.append("| 序号 | 物理文件名 | 提取日期 | 提取标题 | 物理路径 |")
        report_lines.append("| --- | --- | --- | --- | --- |")
        for i, item in enumerate(results["db_not_found"], 1):
            rel_path = os.path.relpath(item["path"], pdf_base)
            report_lines.append(
                f"| {i} | {item['filename']} | {item['fn_date'] or '无'} | "
                f"{item['clean_title']} | pdf/{rel_path} |")
        report_lines.append("\n")
    if results["multiple_conflict"]:
        report_lines.append("## 3. 多重匹配冲突文件列表\n")
        report_lines.append("| 序号 | 物理文件名 | 文件提取日期 | 匹配到的数据库日期 | 匹配到的记录ID列表 | 物理路径 |")
        report_lines.append("| --- | --- | --- | --- | --- | --- |")
        for i, item in enumerate(results["multiple_conflict"], 1):
            dates = [r["publish_time"] for r in item["matched_records"]]
            ids = [r["id"] for r in item["matched_records"]]
            rel_path = os.path.relpath(item["path"], pdf_base)
            report_lines.append(
                f"| {i} | {item['filename']} | {item['fn_date'] or '无'} | "
                f"{dates} | {ids} | pdf/{rel_path} |")
        report_lines.append("\n")

    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_date_check_report.md")
    with open(report_path, "w", encoding="utf-8") as rf:
        rf.write("\n".join(report_lines))

    print("\n" + "=" * 60)
    print("                      检查结果摘要")
    print("=" * 60)
    print(f" 物理文件总数:                      {total_phys_files}")
    print(f" 正常一致文件数:                    {len(results['matched_ok'])}")
    print(f" 日期不符文件数 (需修复):            {len(results['date_mismatch'])}")
    print(f" 数据库中未找到 (孤立文件):          {len(results['db_not_found'])}")
    print(f" 多重匹配冲突 (需人工介入):          {len(results['multiple_conflict'])}")
    print("=" * 60)
    print(f"[+] 详细报告已生成至: {report_path}")
    print("=" * 60)


# ===================================================================
# 功能 2: fix-paths - 将 Unknown_Year 中的 PDF 按数据库日期移到正确年份文件夹
#          (Phase 1) + 全量扫描修复文件名日期不匹配 (Phase 2)
# ===================================================================
def run_fix_names_and_paths(args):
    db_path = get_db_path()
    pdf_base = PDF_BASE_DIR
    unknown_year_dir = os.path.join(pdf_base, "Unknown_Year")

    print("=" * 60)
    print(f"[*] 运行模式: {'【正式修复模式】' if args.run else '【预览模式 (Dry Run)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print(f"[*] 未知年份 PDF 目录: {unknown_year_dir}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    if not os.path.exists(unknown_year_dir):
        print(f"[-] 错误: 未知年份目录不存在: {unknown_year_dir}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    files = [f for f in os.listdir(unknown_year_dir) if f.endswith(".pdf")]
    total_files = len(files)
    print(f"[*] 扫描到 Unknown_Year 下的 PDF 文件数: {total_files}")

    stats = {"success": 0, "conflict": 0, "not_found": 0,
             "still_unknown_date": 0, "invalid_date_format": 0, "error": 0}
    move_plans = []

    print("[*] 正在分析文件匹配关系...")
    for idx, filename in enumerate(files, 1):
        if not filename.startswith("Unknown_Date_"):
            if args.verbose:
                print(f"[-] 跳过非 Unknown_Date 开头的文件: {filename}")
            continue

        title_part = filename[len("Unknown_Date_"):-4]
        if not title_part:
            if args.verbose:
                print(f"[-] 跳过空标题文件: {filename}")
            stats["error"] += 1
            continue

        clean_title = title_part
        suffix = ""
        cursor.execute("SELECT id, publish_time, url, pdf_path FROM resources WHERE title = ?", (clean_title,))
        matched_rows = cursor.fetchall()

        if not matched_rows:
            match = re.search(r"_(?P<num>\d+)$", title_part)
            if match:
                suffix = match.group(0)
                clean_title = title_part[: -len(suffix)]
                cursor.execute("SELECT id, publish_time, url, pdf_path FROM resources WHERE title = ?", (clean_title,))
                matched_rows = cursor.fetchall()

        if not matched_rows:
            cursor.execute("SELECT id, publish_time, url, pdf_path, title FROM resources WHERE title LIKE ?", (f"%{clean_title}%",))
            like_rows = cursor.fetchall()
            if len(like_rows) == 1:
                matched_rows = [(like_rows[0][0], like_rows[0][1], like_rows[0][2], like_rows[0][3])]
            elif len(like_rows) > 1:
                valid_like_rows = [r for r in like_rows if r[1] != 'Unknown_Date' and date_regex.match(r[1])]
                if len(valid_like_rows) == 1:
                    matched_rows = [(valid_like_rows[0][0], valid_like_rows[0][1], valid_like_rows[0][2], valid_like_rows[0][3])]

        if not matched_rows:
            stats["not_found"] += 1
            if args.verbose:
                print(f"[NOT FOUND] 数据库中未找到匹配的资源: {filename}")
            continue

        unique_dates = list(set(r[1] for r in matched_rows))
        if len(unique_dates) > 1:
            matched_by_path = []
            for r in matched_rows:
                db_pdf_path = r[3]
                if db_pdf_path:
                    db_filename = os.path.basename(db_pdf_path.replace('\\', '/'))
                    if db_filename.lower() == filename.lower():
                        matched_by_path.append(r)
            if len(matched_by_path) == 1:
                matched_rows = matched_by_path
                unique_dates = [matched_rows[0][1]]
                if args.verbose:
                    print(f"[RESOLVED] 文件 {filename} 对应多个日期，已通过 pdf_path 匹配到唯一记录: ID={matched_rows[0][0]}, Date={unique_dates[0]}")
            else:
                stats["conflict"] += 1
                if args.verbose or True:
                    print(f"[CONFLICT] 文件 {filename} 对应多个不同的数据库日期: {unique_dates}")
                continue

        matched_date = unique_dates[0]
        matched_ids = [r[0] for r in matched_rows]

        if matched_date == "Unknown_Date" or not matched_date:
            stats["still_unknown_date"] += 1
            if args.verbose:
                print(f"[STILL UNKNOWN] 数据库中对应日期仍为 Unknown_Date: {filename}")
            continue
        if not date_regex.match(matched_date):
            stats["invalid_date_format"] += 1
            if args.verbose:
                print(f"[INVALID FORMAT] 数据库中日期格式非 YYYY-MM-DD ({matched_date}): {filename}")
            continue

        stats["success"] += 1
        year = matched_date.split('-')[0]
        new_filename = f"{matched_date}_{clean_title}{suffix}.pdf"
        src_path = os.path.join(unknown_year_dir, filename)
        dst_dir = os.path.join(pdf_base, year)
        dst_path = os.path.join(dst_dir, new_filename)
        move_plans.append((src_path, dst_path, matched_ids, new_filename, matched_date))
        if args.verbose:
            print(f"[PLAN] {filename} -> {year}/{new_filename} (ID: {matched_ids})")

    print("\n" + "=" * 60)
    print(f"[*] 分析完成！准备处理 {len(move_plans)} 个文件...")

    do_run = args.run
    if not do_run:
        print("[*] 当前为预览模式，未执行任何操作。")
        try:
            confirm = input("[*] 检测完毕，是否直接开始移动文件并更新数据库？[y/N]: ").strip().lower()
            if confirm in ('y', 'yes'):
                do_run = True
        except (KeyboardInterrupt, EOFError):
            print("\n[-] 运行已取消")
            conn.close()
            return

    if do_run:
        print("[*] 开始执行物理移动和数据库更新...")
        success_moved = 0
        for src_path, dst_path, matched_ids, new_filename, matched_date in move_plans:
            try:
                dst_dir = os.path.dirname(dst_path)
                if not os.path.exists(dst_dir):
                    os.makedirs(dst_dir, exist_ok=True)
                os.rename(src_path, dst_path)
                abs_dst_path = os.path.abspath(dst_path)
                id_placeholders = ",".join("?" for _ in matched_ids)
                cursor.execute(
                    f"UPDATE resources SET pdf_path = ? WHERE id IN ({id_placeholders})",
                    [abs_dst_path] + matched_ids)
                success_moved += 1
            except Exception as e:
                print(f"[-] 移动文件失败 {os.path.basename(src_path)}: {e}")
                stats["error"] += 1
        conn.commit()
        print(f"[+] 物理修复完成！成功移动并更新了 {success_moved} 个文件。")
    else:
        print("[*] 未执行任何操作。")

    # ===================================================================
    # Phase 2: 全量扫描所有年份文件夹，修复文件名日期与数据库不一致
    # (从 fix_date_mismatches.py 合并而来)
    # ===================================================================
    print("\n" + "=" * 60)
    print("  Phase 2: 全量检查 - 修复文件名日期与数据库不匹配")
    print("=" * 60)

    print("[*] 正在全量扫描 PDF 目录 (递归所有年份文件夹)...")
    all_pdf_files = []
    for root, dirs, files in os.walk(pdf_base):
        # 跳过 Unknown_Year (已在 Phase 1 处理)
        norm_root = os.path.normpath(root)
        if "Unknown_Year" in norm_root:
            continue
        for f in files:
            if f.lower().endswith(".pdf"):
                all_pdf_files.append((f, os.path.join(root, f)))
    total_phase2 = len(all_pdf_files)
    print(f"[+] 扫描到 {total_phase2} 个 PDF 文件 (不含 Unknown_Year)。")

    # 建立数据库索引
    print("[*] 正在构建数据库索引...")
    cursor.execute("SELECT id, title, publish_time, pdf_path, url FROM resources")
    db_rows = cursor.fetchall()

    db_by_pdf_filename = defaultdict(list)
    db_by_title = defaultdict(list)
    for row in db_rows:
        r_id, r_title, r_publish_time, r_pdf_path, r_url = row
        pub_time = r_publish_time.strip() if r_publish_time else "Unknown_Date"
        if not pub_time:
            pub_time = "Unknown_Date"
        record = {"id": r_id, "title": r_title, "publish_time": pub_time,
                  "pdf_path": r_pdf_path, "url": r_url}
        if r_pdf_path:
            filename_part = os.path.basename(r_pdf_path.replace('\\', '/')).lower()
            db_by_pdf_filename[filename_part].append(record)
        if r_title:
            db_by_title[r_title.strip()].append(record)

    # 分析不匹配
    print("[*] 正在比对文件名日期与数据库日期...")
    mismatches = []
    for filename, full_path in all_pdf_files:
        fn_date, fn_title_part = parse_filename(filename)
        fn_clean_title = clean_title_suffix(fn_title_part)

        matched_records = []
        if filename.lower() in db_by_pdf_filename:
            matched_records = db_by_pdf_filename[filename.lower()]
        if not matched_records:
            if fn_title_part in db_by_title:
                matched_records = db_by_title[fn_title_part]
            elif fn_clean_title in db_by_title:
                matched_records = db_by_title[fn_clean_title]
        if not matched_records:
            continue

        unique_dates = list(set(r["publish_time"] for r in matched_records))
        if len(unique_dates) > 1:
            exact_by_path = []
            for r in matched_records:
                if r["pdf_path"]:
                    basename = os.path.basename(r["pdf_path"].replace('\\', '/')).lower()
                    if basename == filename.lower():
                        exact_by_path.append(r)
            if len(exact_by_path) == 1:
                record = exact_by_path[0]
                if fn_date != record["publish_time"]:
                    mismatches.append((filename, full_path, record["publish_time"], [record]))
        else:
            db_date = unique_dates[0]
            cmp_fn_date = fn_date if fn_date else "Unknown_Date"
            cmp_db_date = db_date if db_date else "Unknown_Date"
            if cmp_fn_date != cmp_db_date:
                mismatches.append((filename, full_path, db_date, matched_records))

    print(f"[+] 发现 {len(mismatches)} 个文件名日期与数据库不符的文件。")

    phase2_success = 0
    if mismatches:
        print(f"\n{'='*60}")
        print(f"  Phase 2: 准备处理 {len(mismatches)} 个文件...")
        for filename, src_path, db_date, matched_records in mismatches:
            if db_date and date_regex.match(db_date):
                target_year = db_date.split('-')[0]
            else:
                target_year = "Unknown_Year"
                db_date = "Unknown_Date"
            _, fn_title_part = parse_filename(filename)
            new_filename = f"{db_date}_{fn_title_part}.pdf"
            target_dir = os.path.join(pdf_base, target_year)

            if do_run:
                if not os.path.exists(target_dir):
                    os.makedirs(target_dir, exist_ok=True)
                dst_path = generate_unique_path(target_dir, new_filename)
            else:
                dst_path = os.path.join(target_dir, new_filename)
                if os.path.exists(dst_path):
                    name, ext = os.path.splitext(new_filename)
                    dst_path = os.path.join(target_dir, f"{name}_1{ext}")

            matched_ids = [r["id"] for r in matched_records]
            rel_src = os.path.relpath(src_path, pdf_base)
            rel_dst = os.path.relpath(dst_path, pdf_base)
            print(f"  [PLAN] ID: {matched_ids} | pdf/{rel_src} -> pdf/{rel_dst}")

            if do_run:
                try:
                    os.rename(src_path, dst_path)
                    abs_dst_path = os.path.abspath(dst_path)
                    id_placeholders = ",".join("?" for _ in matched_ids)
                    cursor.execute(
                        f"UPDATE resources SET pdf_path = ? WHERE id IN ({id_placeholders})",
                        [abs_dst_path] + matched_ids
                    )
                    phase2_success += 1
                except Exception as e:
                    print(f"    [-] 修复失败: {e}")

        if do_run:
            conn.commit()
            print(f"\n[+] Phase 2 完成！成功修复 {phase2_success}/{len(mismatches)} 个文件。")
        else:
            print(f"\n[*] Phase 2 预览完成，未执行实际操作。")
    else:
        print("[+] 所有文件名日期与数据库一致，无需修复。")

    conn.close()
    print("\n" + "=" * 60)
    print("                      统计报告")
    print("=" * 60)
    print(f" 扫描文件总数:                      {total_files}")
    print(f" 匹配成功数 (可修复):                 {stats['success']}")
    print(f" 数据库中仍未修复 (Still Unknown):   {stats['still_unknown_date']}")
    print(f" 数据库中未找到 (Not Found):         {stats['not_found']}")
    print(f" 多重日期冲突 (Conflict):             {stats['conflict']}")
    print(f" 无效日期格式:                      {stats['invalid_date_format']}")
    print(f" 其他处理错误 (Error):               {stats['error']}")
    print("=" * 60)


# ===================================================================
# 功能 3: redownload - 重新下载体积小于 20KB 的 PDF
# ===================================================================
def run_redownload_small_pdfs(args):
    db_path = get_db_path()
    pdf_base = os.path.abspath(PDF_BASE_DIR)

    print("=" * 60)
    print(f"[*] 运行模式: {'【正式修复模式 (重下/覆盖)】' if args.run else '【预览模式 (Dry Run)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    if not os.path.exists(pdf_base):
        print(f"[-] 错误: PDF 根目录不存在: {pdf_base}")
        sys.exit(1)

    print("[*] 正在扫描 PDF 物理文件以寻找体积小于 20KB 的文件...")
    small_files = []
    for root, dirs, files in os.walk(pdf_base):
        for f in files:
            if f.lower().endswith(".pdf"):
                full_path = os.path.join(root, f)
                try:
                    size_bytes = os.path.getsize(full_path)
                    size_kb = size_bytes / 1024.0
                    if size_kb < 20.0:
                        small_files.append((full_path, size_kb))
                except OSError as e:
                    print(f"[-] 无法读取文件大小 {f}: {e}")

    total_small = len(small_files)
    print(f"[+] 扫描完成，共找到 {total_small} 个体积小于 20KB 的 PDF 文件。")
    if total_small == 0:
        print("[*] 未发现需要重新保存的 PDF 文件。")
        return

    print("[*] 正在加载数据库中的 PDF 路径进行比对匹配...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, url, pdf_path, publish_time FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    db_rows = cursor.fetchall()

    db_map = {}
    for row in db_rows:
        db_pdf_path = row[3]
        if db_pdf_path:
            base_f = os.path.basename(db_pdf_path.replace('\\', '/')).lower()
            db_map.setdefault(base_f, []).append(row)

    to_download = []
    not_found_in_db = []

    for file_path, size_kb in small_files:
        filename = os.path.basename(file_path).lower()
        rows = db_map.get(filename, [])
        target_rel = to_relative_path(file_path).lower()
        matched_row = None
        for row in rows:
            db_rel = to_relative_path(row[3]).lower()
            if db_rel == target_rel:
                matched_row = row
                break
        if not matched_row and rows:
            for row in rows:
                if os.path.basename(row[3]).lower() == filename:
                    matched_row = row
                    break
        if matched_row:
            r_id, title, url, pdf_path_db, publish_time = matched_row
            to_download.append((file_path, size_kb, r_id, title, url, publish_time))
        else:
            not_found_in_db.append((file_path, size_kb))

    print(f"[*] 成功匹配数据库记录: {len(to_download)} 个")
    if not_found_in_db:
        print(f"[!] 未能匹配数据库记录: {len(not_found_in_db)} 个 (无法获取 URL 重新下载)")
        if args.verbose:
            for fp, sz in not_found_in_db:
                print(f"  - {fp} ({sz:.2f} KB)")

    if not to_download:
        print("[*] 无匹配的数据库记录可用于重新下载。")
        conn.close()
        return

    do_run = args.run
    if not do_run:
        print("\n" + "=" * 60)
        print("                  需要重新下载的 PDF 预览")
        print("=" * 60)
        preview_limit = 20
        for idx, (fp, sz, r_id, title, url, pub_time) in enumerate(to_download[:preview_limit], 1):
            print(f"[{idx}] 路径: {fp}")
            print(f"    大小: {sz:.2f} KB | ID: {r_id} | 标题: {title} | 日期: {pub_time}")
            print(f"    URL:  {url}")
            print("-" * 60)
        if len(to_download) > preview_limit:
            print(f"... 还有 {len(to_download) - preview_limit} 个文件未列出")
        print(f"\n[*] 预览结束。")
        try:
            confirm = input("[*] 检测完毕，是否直接开始重新下载并覆盖修复？[y/N]: ").strip().lower()
            if confirm in ('y', 'yes'):
                do_run = True
        except (KeyboardInterrupt, EOFError):
            print("\n[-] 运行已取消")
            conn.close()
            return

    if not do_run:
        print("[*] 未重新下载任何文件。")
        conn.close()
        return

    print(f"\n[*] 准备拉起 Playwright 重新下载 {len(to_download)} 个 PDF 文件...")
    success_count = 0
    fail_count = 0

    try:
        with sync_playwright() as p:
            browser, context = _create_browser_context(p, viewport={'width': 1280, 'height': 900})
            for idx, (file_path, size_kb, r_id, title, url, publish_time) in enumerate(to_download, 1):
                print(f"\n[*] [{idx}/{len(to_download)}] 正在请求: {url} (当前大小: {size_kb:.2f} KB)")
                page = context.new_page()
                try:
                    response = page.goto(url, timeout=45000, wait_until="domcontentloaded")
                    time.sleep(3.0)
                    if response and response.status == 404:
                        print(f"  [-] 页面返回 404，不重新下载。")
                        fail_count += 1
                        continue
                    try:
                        page.evaluate("""() => {
                            const breadcrumbs = document.querySelector('.breadcrumbs');
                            if (breadcrumbs) {
                                let prev = breadcrumbs.previousElementSibling;
                                while (prev) {
                                    if (prev.classList.contains('gs-isgood') &&
                                        !prev.textContent.includes('永久地址') &&
                                        !prev.textContent.includes('永久')) {
                                        prev.remove();
                                    }
                                    prev = prev.previousElementSibling;
                                }
                            }
                            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"]');
                            adDivs.forEach(div => div.remove());
                            const bottomFloat = document.getElementById('bottom_float');
                            if (bottomFloat) bottomFloat.remove();
                        }""")
                    except Exception:
                        pass
                    page.pdf(path=file_path, format="A4", print_background=True,
                             margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"})
                    if os.path.exists(file_path):
                        new_size_kb = os.path.getsize(file_path) / 1024.0
                        if new_size_kb >= 20.0:
                            success_count += 1
                            print(f"  [+] 成功重新保存并覆盖! 新文件大小: {new_size_kb:.2f} KB")
                        else:
                            fail_count += 1
                            print(f"  [-] 警告: 重新保存后体积依然小于 20KB ({new_size_kb:.2f} KB)")
                    else:
                        fail_count += 1
                        print("  [-] 错误: PDF 生成文件未在本地检测到")
                except Exception as download_err:
                    print(f"  [-] 下载失败: {download_err}")
                    fail_count += 1
                finally:
                    page.close()
                time.sleep(random.uniform(2.0, 4.0))
            browser.close()
    except Exception as run_e:
        print(f"[-] Playwright 运行异常: {run_e}")

    conn.close()
    print("\n" + "=" * 60)
    print("                      下载统计报告")
    print("=" * 60)
    print(f" 计划重新下载数:             {len(to_download)}")
    print(f" 成功重新下载/覆盖数:         {success_count}")
    print(f" 失败或依然不合格数:         {fail_count}")
    print("=" * 60)


# ===================================================================
# 功能 4: rebuild - 重建缺失的 PDF 文件并路径相对化
# ===================================================================
def run_rebuild(args):
    db_path = get_db_path()
    base_dir = os.path.abspath(PDF_BASE_DIR)
    project_dir = os.path.dirname(base_dir)

    print("=" * 60)
    print(f"[*] 运行模式: {'【正式修复模式】' if args.run else '【预览模式 (Dry Run)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] 项目根目录: {project_dir}")
    print(f"[*] PDF 根目录: {base_dir}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    if not os.path.exists(base_dir):
        print(f"[*] 创建 PDF 根目录: {base_dir}")
        if args.run:
            os.makedirs(base_dir, exist_ok=True)

    print("[*] 正在扫描 PDF 物理文件...")
    phys_files = set()
    if os.path.exists(base_dir):
        for root, dirs, files in os.walk(base_dir):
            for f in files:
                if f.lower().endswith(".pdf"):
                    phys_files.add(os.path.abspath(os.path.join(root, f)).lower())
    print(f"[+] 物理目录中现存的 PDF 文件数: {len(phys_files)}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT id, title, pdf_path, url, publish_time, source FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    rows = cursor.fetchall()
    print(f"[*] 数据库中含有 pdf_path 的总记录数: {len(rows)}")

    needs_update_to_relative = []
    missing_records = []

    for r_id, title, pdf_path, url, publish_time, source in rows:
        rel_path = to_relative_path(pdf_path)
        norm_abs_path = os.path.abspath(os.path.join(project_dir, rel_path))
        if norm_abs_path.lower() in phys_files:
            if pdf_path != rel_path:
                needs_update_to_relative.append((r_id, rel_path))
        else:
            missing_records.append((r_id, title, url, publish_time, source))

    print(f"[*] 需要转换为相对路径（且物理文件已存在）的记录数: {len(needs_update_to_relative)}")
    print(f"[*] 真正物理缺失（本地无文件）的记录数: {len(missing_records)}")
    print("=" * 60)

    # --- 路径相对化 ---
    if needs_update_to_relative:
        if args.run:
            print("[*] 正在执行相对路径纠偏更新数据库...")
            success_update = 0
            for r_id, rel_path in needs_update_to_relative:
                try:
                    cursor.execute("UPDATE resources SET pdf_path = ? WHERE id = ?", (rel_path, r_id))
                    success_update += 1
                except Exception as e:
                    print(f"[-] 纠偏 ID {r_id} 失败: {e}")
            conn.commit()
            print(f"[+] 路径相对化成功更新了 {success_update} 条记录。")
        else:
            print(f"[PLAN] 将更新 {len(needs_update_to_relative)} 条记录为相对路径（示例前 5 条）：")
            for r_id, rel_path in needs_update_to_relative[:5]:
                print(f"  - ID: {r_id} -> {rel_path}")
            print("[*] 提示: 预览模式下未修改数据库。")

    # --- 重新下载缺失文件 ---
    if missing_records:
        if args.skip_download:
            print("[*] 参数指定跳过重新下载缺失文件。")
        elif not args.run:
            print(f"\n[PLAN] 发现 {len(missing_records)} 个物理缺失的文件（示例前 5 个）：")
            for r_id, title, url, publish_time in missing_records[:5]:
                print(f"  - ID: {r_id} | 标题: {title} | 日期: {publish_time}")
                print(f"    URL: {url}")
            print("[*] 提示: 预览模式下未下载任何文件。")
            print("[*] 若要正式开始修复，请运行: python fixes/pdf_maintenance.py rebuild --run")
        else:
            print(f"\n[*] 准备拉起 Playwright 重新下载 {len(missing_records)} 个缺失 of PDF...")
            success_download = 0
            fail_download = 0
            not_found_download = 0

            try:
                from utils.pdf_generator import PDFGenerator, PDFRenderConfig
                
                CONFIG_MAP = {
                    "seju": PDFRenderConfig(
                        margin={"top": "20mm", "bottom": "20mm", "left": "20mm", "right": "20mm"}
                    ),
                    "gcbt": PDFRenderConfig(
                        need_img_proxy=True,
                        pre_access_url="https://gcbt.net/",
                        referer="https://gcbt.net/",
                        need_lazy_scroll=True,
                        emulate_media="screen",
                        ad_selectors=[
                            '.layui-layer', '.layui-layer-shade',
                            '.modal', '.modal-backdrop',
                            '.swal-overlay', '.swal-modal', '.swal2-container',
                            '[id*="layui-layer"]'
                        ],
                        ad_block_js="""() => {
                            if (document.body) document.body.style.overflow = 'auto';
                            if (document.documentElement) document.documentElement.style.overflow = 'auto';
                        }"""
                    ),
                    "madou": PDFRenderConfig(
                        ad_selectors=[
                            'div[style*="height:60px"]',
                            'div[style*="height:55px"]',
                            'div[style*="height:70px"]',
                            '#bottom_float'
                        ]
                    ),
                    "datang": PDFRenderConfig(
                        ad_block_js="""() => {
                            const breadcrumbs = document.querySelector('.breadcrumbs');
                            if (breadcrumbs) {
                                let prev = breadcrumbs.previousElementSibling;
                                while (prev) {
                                    if (prev.classList.contains('gs-isgood') && 
                                        !prev.textContent.includes('永久地址') && 
                                        !prev.textContent.includes('永久')) {
                                        prev.remove();
                                    }
                                    prev = prev.previousElementSibling;
                                }
                            }
                            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"]');
                            adDivs.forEach(div => div.remove());
                            const bottomFloat = document.getElementById('bottom_float');
                            if (bottomFloat) {
                                bottomFloat.remove();
                            }
                        }"""
                    ),
                    "jingpin_toupai": PDFRenderConfig(
                        emulate_media="screen",
                        ad_selectors=[
                            'div[style*="height:60px"]', 
                            'div[style*="height:55px"]', 
                            'div[style*="height:70px"]',
                            '#bottom_float',
                            '.layui-layer',
                            '.layui-layer-shade',
                            '[id*="layui-layer"]',
                            '.modal',
                            '.modal-backdrop'
                        ],
                        ad_block_js="""() => {
                            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
                            if (document.body) document.body.style.overflow = 'auto';
                            if (document.documentElement) document.documentElement.style.overflow = 'auto';
                        }"""
                    )
                }

                generator = PDFGenerator(r2_uploader=None)

                with sync_playwright() as p:
                    browser, context = _create_browser_context(p)
                    for idx, (r_id, title, url, publish_time, source) in enumerate(missing_records, 1):
                        try:
                            print(f"[*] [{idx}/{len(missing_records)}] 正在请求 URL: {url} (来源: {source})")
                            
                            config = CONFIG_MAP.get(source, PDFRenderConfig())

                            # 预检查 404 状态
                            temp_page = context.new_page()
                            is_404 = False
                            try:
                                response = temp_page.goto(url, timeout=30000, wait_until="domcontentloaded")
                                if response and response.status == 404:
                                    is_404 = True
                            except Exception:
                                pass
                            finally:
                                temp_page.close()

                            if is_404:
                                print(f"  [-] 页面 404 未找到，清除该记录的 pdf_path。")
                                cursor.execute("UPDATE resources SET pdf_path = '' WHERE id = ?", (r_id,))
                                not_found_download += 1
                            else:
                                rel_pdf_path = generator.generate_pdf(
                                    page_or_context=context,
                                    target_url_or_page=url,
                                    publish_date=publish_time,
                                    title=title,
                                    source_name=source,
                                    config=config
                                )
                                if rel_pdf_path:
                                    cursor.execute("UPDATE resources SET pdf_path = ? WHERE id = ?", (rel_pdf_path, r_id))
                                    success_download += 1
                                    print(f"  [+] 下载成功并更新数据库为相对路径: {rel_pdf_path}")
                                else:
                                    print(f"  [-] 下载物理文件失败 ID: {r_id}")
                                    fail_download += 1
                            conn.commit()
                        except Exception as e:
                            print(f"  [-] 下载物理文件失败 ID: {r_id} | 错误: {e}")
                            fail_download += 1
                        time.sleep(random.uniform(1.5, 3.5))
                    browser.close()
            except Exception as e:
                print(f"[-] Playwright 运行异常: {e}")

            print("\n" + "=" * 60)
            print("                      下载统计报告")
            print("=" * 60)
            print(f" 计划重新下载数:             {len(missing_records)}")
            print(f" 成功下载并更新数:           {success_download}")
            print(f" 页面不存在 (404已清理) 数:   {not_found_download}")
            print(f" 下载失败 (保留原样) 数:     {fail_download}")
            print("=" * 60)

    conn.close()
    print("[+] 运行结束。")


# ===================================================================
# 功能 5: orphan - 检查多余PDF或恢复多余PDF
# ===================================================================
def _move_orphans_to_root(db_path, pdf_base, args):
    """扫描所有年份子文件夹，将数据库中无对应记录的PDF移到/pdf根目录"""
    print("=" * 60)
    print("[*] 检查多余PDF - 将年份文件夹中数据库中无记录的PDF移到/pdf根目录")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        return
    if not os.path.exists(pdf_base):
        print(f"[-] 错误: PDF 根目录不存在: {pdf_base}")
        return

    # 1. 扫描所有PDF物理文件（排除根目录本身和Unknown_Year）
    print("[*] 正在扫描PDF目录...")
    all_pdf_files = {}  # rel_path -> (full_path, filename, year_folder)
    year_folders = []
    for item in os.listdir(pdf_base):
        item_path = os.path.join(pdf_base, item)
        if os.path.isdir(item_path) and item != "Unknown_Year":
            year_folders.append(item)
            for root, dirs, files in os.walk(item_path):
                for f in files:
                    if f.lower().endswith(".pdf"):
                        full_path = os.path.join(root, f)
                        rel = os.path.relpath(full_path, pdf_base)
                        all_pdf_files[rel] = (full_path, f, item)

    print(f"[+] 扫描到 {len(year_folders)} 个年份文件夹, 共 {len(all_pdf_files)} 个PDF文件。")

    # 2. 加载数据库记录
    print("[*] 正在加载数据库中的资源记录...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, publish_time, pdf_path, url FROM resources")
    db_rows = cursor.fetchall()
    print(f"[+] 成功加载 {len(db_rows)} 条数据库记录。")

    # 建立索引: 按pdf_path（相对路径和绝对路径）和文件名
    db_by_relpath = {}
    db_by_abspath = {}
    db_by_filename = defaultdict(list)
    db_by_title = defaultdict(list)
    for row in db_rows:
        r_id, r_title, r_publish_time, r_pdf_path, r_url = row
        if r_pdf_path:
            rel = r_pdf_path.replace('\\', '/')
            db_by_relpath[rel.lower()] = row
            abs_p = os.path.abspath(os.path.join(pdf_base, '..', rel)).lower()
            db_by_abspath[abs_p] = row
            fn = os.path.basename(rel).lower()
            db_by_filename[fn].append(row)
        if r_title:
            db_by_title[r_title.strip()].append(row)

    # 3. 比对，找出孤儿文件
    print("[*] 正在比对文件与数据库...")
    matched = []
    associated = []      # (filename, full_path, year_folder, record_id, title, pub_time)
    warn_conflict = []   # (filename, full_path, year_folder, record)
    truly_orphan = []    # (filename, full_path, year_folder)
    for rel_path, (full_path, filename, year_folder) in all_pdf_files.items():
        norm_rel = rel_path.replace('\\', '/').lower()
        norm_abs = full_path.lower()
        found = False

        # 按相对路径匹配
        if norm_rel in db_by_relpath:
            found = True
        # 按绝对路径匹配
        if not found and norm_abs in db_by_abspath:
            found = True
        # 按文件名匹配
        if not found:
            fn_lower = filename.lower()
            if fn_lower in db_by_filename:
                # 检查是否有多个匹配，且其中某个的路径与当前文件一致
                for r in db_by_filename[fn_lower]:
                    db_p = r[3].replace('\\', '/').lower() if r[3] else ""
                    if db_p and os.path.basename(db_p) == fn_lower:
                        found = True
                        break
                if not found:
                    found = True  # 只要有文件名匹配就算

        if found:
            matched.append((filename, full_path, year_folder))
            continue

        # 未通过路径/文件名匹配，尝试通过文件名中的资源名在数据库中搜索标题
        fn_date, fn_title = parse_filename(filename)
        clean_title = clean_title_suffix(fn_title) if fn_title else ""

        matched_records = []
        if clean_title and clean_title in db_by_title:
            matched_records = db_by_title[clean_title]
        if not matched_records and fn_title and fn_title in db_by_title:
            matched_records = db_by_title[fn_title]

        if matched_records:
            # 取第一个匹配的记录
            record = matched_records[0]
            r_id, r_title, r_publish_time, r_pdf_path, r_url = record

            if not r_pdf_path:  # pdf_path 为空，可以关联
                associated.append((filename, full_path, year_folder, r_id, r_title, r_publish_time))
            else:  # 已有 pdf_path，给出警告
                warn_conflict.append((filename, full_path, year_folder, record))
                print(f"[!] 警告: PDF '{filename}' 对应数据库记录(ID={r_id}, 标题='{r_title}') "
                      f"已存在 pdf_path='{r_pdf_path}'，可能为重复PDF")
        else:
            truly_orphan.append((filename, full_path, year_folder))

    # 4. 执行自动关联（将关联到的 pdf_path 写入数据库）
    if associated:
        print(f"\n[*] 发现 {len(associated)} 个PDF文件可通过标题匹配到数据库记录（pdf_path 为空），正在自动关联...")
        for fn, full_path, yf, r_id, r_title, r_publish_time in associated:
            try:
                rel_pdf_path = to_relative_path(full_path)
                cursor.execute("UPDATE resources SET pdf_path = ? WHERE id = ?", (rel_pdf_path, r_id))
                print(f"  [+] 已关联: {yf}/{fn} -> 记录ID={r_id}, 标题='{r_title}', pdf_path='{rel_pdf_path}'")
            except Exception as e:
                print(f"  [-] 关联失败 {fn}: {e}")
        conn.commit()
        print(f"[+] 自动关联完成。\n")

    # 5. 报告
    print("\n" + "=" * 60)
    print(f"  扫描文件总数: {len(all_pdf_files)}")
    print(f"  数据库有记录(保留): {len(matched)}")
    print(f"  通过标题自动关联: {len(associated)}")
    print(f"  标题匹配但已有pdf_path(需人工确认): {len(warn_conflict)}")
    print(f"  完全无匹配(多余): {len(truly_orphan)}")
    print("=" * 60)

    all_orphans = []
    for item in warn_conflict:
        all_orphans.append({"type": "warn", "filename": item[0], "path": item[1], "folder": item[2], "record": item[3]})
    for item in truly_orphan:
        all_orphans.append({"type": "orphan", "filename": item[0], "path": item[1], "folder": item[2]})

    if not all_orphans:
        print("[*] 未发现多余PDF文件。")
        conn.close()
        return

    # 列出标题匹配但已有 pdf_path 的警告文件
    if warn_conflict:
        print("\n[!] 以下PDF文件通过标题匹配到数据库记录，但记录已有pdf_path，需人工确认:")
        for fn, fp, yf, record in warn_conflict:
            r_id, r_title, r_publish_time, r_pdf_path, r_url = record
            print(f"  [ID={r_id}] {yf}/{fn}")
            print(f"       数据库已有 pdf_path: {r_pdf_path}")
        print()

    # 列出完全无匹配的多余文件
    if truly_orphan:
        print("[*] 以下为数据库中完全无对应记录的PDF文件（多余PDF）:")
        for i, (fn, fp, yf) in enumerate(truly_orphan, 1):
            print(f"  [{i}] {yf}/{fn}")
        print()

    # 6. 询问是否移动多余PDF
    try:
        confirm = input(f"\n[*] 是否将这 {len(all_orphans)} 个多余/冲突PDF文件移动到 {pdf_base} 根目录？[y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n[-] 操作已取消")
        conn.close()
        return

    if confirm not in ('y', 'yes'):
        print("[*] 未执行移动操作。")
        conn.close()
        return

    # 7. 执行移动
    print("[*] 正在移动多余PDF文件...")
    moved_count = 0
    for item in all_orphans:
        fn = item["filename"]
        full_path = item["path"]
        yf = item["folder"]
        try:
            dst_path = os.path.join(pdf_base, fn)
            # 避免重名
            if os.path.exists(dst_path):
                name, ext = os.path.splitext(fn)
                counter = 1
                while os.path.exists(dst_path):
                    dst_path = os.path.join(pdf_base, f"{name}_{counter}{ext}")
                    counter += 1
            os.rename(full_path, dst_path)
            print(f"  [+] 已移动: {yf}/{fn} -> {os.path.basename(dst_path)}")
            moved_count += 1
        except Exception as e:
            print(f"  [-] 移动失败 {fn}: {e}")

    print(f"\n[+] 成功移动 {moved_count}/{len(all_orphans)} 个文件到 {pdf_base} 根目录。")
    conn.close()


def _restore_orphans_from_root(db_path, pdf_base, args):
    """将/pdf根目录下的PDF文件移回对应年份文件夹"""
    print("=" * 60)
    print("[*] 恢复多余PDF - 将/pdf根目录下的PDF移回对应年份文件夹")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        return
    if not os.path.exists(pdf_base):
        print(f"[-] 错误: PDF 根目录不存在: {pdf_base}")
        return

    # 1. 扫描/pdf根目录下的PDF文件
    print("[*] 正在扫描/pdf根目录下的PDF文件...")
    root_pdfs = []
    for f in os.listdir(pdf_base):
        if f.lower().endswith(".pdf"):
            full_path = os.path.join(pdf_base, f)
            if os.path.isfile(full_path):
                root_pdfs.append((f, full_path))

    if not root_pdfs:
        print("[*] /pdf根目录下没有PDF文件。")
        return

    print(f"[+] 扫描到 {len(root_pdfs)} 个PDF文件。")

    # 2. 加载数据库记录
    print("[*] 正在加载数据库中的资源记录...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, publish_time, pdf_path FROM resources")
    db_rows = cursor.fetchall()
    print(f"[+] 成功加载 {len(db_rows)} 条数据库记录。")

    # 建立索引
    db_by_title = defaultdict(list)
    db_by_filename = defaultdict(list)
    for row in db_rows:
        r_id, r_title, r_publish_time, r_pdf_path = row
        if r_pdf_path:
            fn = os.path.basename(r_pdf_path.replace('\\', '/')).lower()
            db_by_filename[fn].append(row)
        if r_title:
            db_by_title[r_title.strip()].append(row)

    # 3. 对每个根目录PDF，判断应放入哪个年份文件夹
    print("[*] 正在分析PDF文件归属...")
    restore_plans = []  # (src_path, dst_path, filename, year)
    no_match = []
    skipped = []

    for filename, full_path in root_pdfs:
        # 尝试从文件名解析日期
        fn_date, fn_title = parse_filename(filename)
        clean_title = clean_title_suffix(fn_title) if fn_title else None

        target_year = None
        target_date = None

        if fn_date and date_regex.match(fn_date):
            # 文件名已有日期，直接使用
            target_date = fn_date
            target_year = fn_date.split('-')[0]
        else:
            # 尝试从数据库查找
            matched_records = []

            # 按文件名匹配
            if filename.lower() in db_by_filename:
                matched_records = db_by_filename[filename.lower()]

            # 按标题匹配
            if not matched_records and fn_title:
                if fn_title in db_by_title:
                    matched_records = db_by_title[fn_title]
                elif clean_title and clean_title in db_by_title:
                    matched_records = db_by_title[clean_title]

            if matched_records:
                # 取第一个有有效日期的记录
                for r in matched_records:
                    _, _, pub_time, _ = r
                    if pub_time and date_regex.match(pub_time):
                        target_date = pub_time
                        target_year = pub_time.split('-')[0]
                        break
                if not target_year and matched_records:
                    _, _, pub_time, _ = matched_records[0]
                    if pub_time and date_regex.match(pub_time):
                        target_date = pub_time
                        target_year = pub_time.split('-')[0]

        if target_year and target_year.isdigit():
            year_dir = os.path.join(pdf_base, target_year)
            new_filename = filename
            if fn_date is None and target_date:
                # 文件名没有日期前缀，加上
                new_filename = f"{target_date}_{fn_title or filename[:-4]}.pdf"
            dst_path = os.path.join(year_dir, new_filename)
            # 避免重名
            if os.path.exists(dst_path):
                name, ext = os.path.splitext(new_filename)
                counter = 1
                while os.path.exists(dst_path):
                    dst_path = os.path.join(year_dir, f"{name}_{counter}{ext}")
                    counter += 1
            restore_plans.append((full_path, dst_path, filename, target_year))
        elif fn_date == "Unknown_Date":
            skipped.append((filename, "文件中日期为 Unknown_Date，无法确定年份"))
            no_match.append((filename, "文件中日期为 Unknown_Date"))
        else:
            no_match.append((filename, "无法从文件名或数据库确定日期"))

    # 4. 报告
    print("\n" + "=" * 60)
    print(f"  /pdf根目录文件总数: {len(root_pdfs)}")
    print(f"  可恢复(有对应年份): {len(restore_plans)}")
    print(f"  无法确定归属: {len(no_match)}")
    print("=" * 60)

    if restore_plans:
        print("\n[*] 以下文件可恢复到对应年份文件夹:")
        for src, dst, fn, year in restore_plans:
            print(f"  {fn} -> {year}/{os.path.basename(dst)}")

    if no_match:
        print("\n[!] 以下文件无法确定归属:")
        for fn, reason in no_match:
            print(f"  {fn} ({reason})")

    if not restore_plans:
        print("[*] 没有可恢复的文件。")
        conn.close()
        return

    # 5. 询问是否移动
    try:
        confirm = input(f"\n[*] 是否将这 {len(restore_plans)} 个文件移回对应年份文件夹？[y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n[-] 操作已取消")
        conn.close()
        return

    if confirm not in ('y', 'yes'):
        print("[*] 未执行移动操作。")
        conn.close()
        return

    # 6. 执行移动
    print("[*] 正在移动文件...")
    moved_count = 0
    for src_path, dst_path, filename, year in restore_plans:
        try:
            dst_dir = os.path.dirname(dst_path)
            if not os.path.exists(dst_dir):
                os.makedirs(dst_dir, exist_ok=True)
            os.rename(src_path, dst_path)
            print(f"  [+] 已移动: {filename} -> {year}/{os.path.basename(dst_path)}")
            moved_count += 1
        except Exception as e:
            print(f"  [-] 移动失败 {filename}: {e}")

    print(f"\n[+] 成功移动 {moved_count}/{len(restore_plans)} 个文件。")
    conn.close()


def run_orphan(args):
    """多余PDF管理 - 检查多余PDF或恢复多余PDF"""
    db_path = get_db_path()
    pdf_base = os.path.abspath(PDF_BASE_DIR)

    print("=" * 60)
    print("                 多余PDF管理工具")
    print("=" * 60)
    print("  1. 检查多余PDF - 将年份文件夹中数据库中无记录的PDF移到/pdf根目录")
    print("  2. 恢复多余PDF - 将/pdf根目录下的PDF移回对应年份文件夹")
    print("=" * 60)

    try:
        choice = input("请输入序号 [1/2] (直接回车默认 1): ").strip()
        if not choice:
            choice = "1"
    except (KeyboardInterrupt, EOFError):
        print("\n[-] 运行已取消")
        return

    if choice == "1":
        _move_orphans_to_root(db_path, pdf_base, args)
    elif choice == "2":
        _restore_orphans_from_root(db_path, pdf_base, args)
    else:
        print("[-] 无效的序号。")


# ===================================================================
# 主入口 - 支持子命令: check-dates, fix-paths, redownload, rebuild, orphan
# ===================================================================
def main():
    setup_console_utf8()
    parser = argparse.ArgumentParser(
        description="PDF 维护工具合集 - 检查日期、修正路径、重新下载小文件、重建缺失文件、管理多余PDF")
    subparsers = parser.add_subparsers(dest="command", help="可用的子命令")

    # check-dates
    p_check = subparsers.add_parser("check-dates", help="检查 PDF 文件与数据库日期的匹配情况并生成报告")
    p_check.set_defaults(func=run_check_dates)

    # fix-paths
    p_fix = subparsers.add_parser("fix-paths", help="将 Unknown_Year 中的 PDF 按数据库日期移到正确年份文件夹 + 全量检查修复文件名日期不匹配")
    p_fix.add_argument("--run", action="store_true", default=False,
                       help="正式运行修复，不加此参数时仅进行预览 (Dry Run)")
    p_fix.add_argument("--verbose", "-v", action="store_true", default=False,
                       help="详细输出每个文件的分析计划")
    p_fix.set_defaults(func=run_fix_names_and_paths)

    # redownload
    p_redl = subparsers.add_parser("redownload", help="重新下载体积小于 20KB 的 PDF 文件")
    p_redl.add_argument("--run", action="store_true", default=False,
                        help="正式运行修复，不加此参数时仅进行预览 (Dry Run)")
    p_redl.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="详细输出")
    p_redl.set_defaults(func=run_redownload_small_pdfs)

    # rebuild
    p_rebuild = subparsers.add_parser("rebuild", help="重建缺失的 PDF 文件并路径相对化")
    p_rebuild.add_argument("--run", action="store_true", default=False,
                           help="正式执行修复和更新，不加此参数时仅进行预览 (Dry Run)")
    p_rebuild.add_argument("--skip-download", action="store_true", default=False,
                           help="仅执行路径相对化和纠偏，不重新下载物理缺失的文件")
    p_rebuild.set_defaults(func=run_rebuild)

    # orphan
    p_orphan = subparsers.add_parser("orphan", help="检查多余PDF或将多余PDF移回原处")
    p_orphan.set_defaults(func=run_orphan)

    args = parser.parse_args()

    if args.command is None:
        # 无子命令时，补充所有子命令参数的默认值
        for attr in ('run', 'verbose', 'skip_download'):
            if not hasattr(args, attr):
                setattr(args, attr, False)

        # 显示交互菜单
        print("=" * 60)
        print("                  PDF 维护工具合集")
        print("=" * 60)
        print("  请选择要运行的功能：")
        print()
        print("    1. check-dates  - 检查 PDF 文件与数据库日期的匹配情况并生成报告")
        print("    2. fix-paths    - 将 Unknown_Year 中的 PDF 移到正确年份文件夹 + 全量修复文件名日期不匹配")
        print("    3. redownload   - 重新下载体积小于 20KB 的 PDF 文件")
        print("    4. rebuild      - 重建缺失的 PDF 文件并路径相对化")
        print("    5. orphan       - 检查多余PDF或将多余PDF移回原处")
        print()
        print("    0. 退出")
        print("=" * 60)

        try:
            choice = input("请输入序号 [0-5] (直接回车默认 1): ").strip()
            if not choice:
                choice = "1"
        except (KeyboardInterrupt, EOFError):
            print("\n[-] 运行已取消")
            sys.exit(0)

        if choice == "1":
            run_check_dates(args)
        elif choice == "2":
            run_fix_names_and_paths(args)
        elif choice == "3":
            run_redownload_small_pdfs(args)
        elif choice == "4":
            run_rebuild(args)
        elif choice == "5":
            run_orphan(args)
        elif choice == "0":
            print("[*] 已退出。")
            sys.exit(0)
        else:
            print("[-] 无效的序号。")
            sys.exit(1)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
