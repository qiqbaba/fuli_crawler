"""
修复未知日期 PDF 文件名和路径（将文件移入正确年份文件夹）

⚠️ 已废弃: 本功能已合并到 fixes/pdf_maintenance.py，请使用以下命令代替:
    python fixes/pdf_maintenance.py fix-paths
"""
import os
import re
import sys
import sqlite3
import argparse

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 引入项目中的配置
from config import get_db_path, PDF_BASE_DIR
from utils import setup_console_utf8
from utils.metadata_parser import sanitize_filename
from utils.pdf_utils import to_relative_path


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

    # 连接本地数据库
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 获取所有 PDF 文件
    files = [f for f in os.listdir(unknown_year_dir) if f.endswith(".pdf")]
    total_files = len(files)
    print(f"[*] 扫描到 Unknown_Year 下的 PDF 文件数: {total_files}")

    stats = {
        "success": 0,
        "conflict": 0,
        "not_found": 0,
        "still_unknown_date": 0,
        "invalid_date_format": 0,
        "error": 0
    }

    # 用于保存计划要移动的文件信息
    move_plans = []
    
    # 匹配 YYYY-MM-DD 格式的日期
    date_regex = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    print("[*] 正在分析文件匹配关系...")
    for idx, filename in enumerate(files, 1):
        if not filename.startswith("Unknown_Date_"):
            if args.verbose:
                print(f"[-] 跳过非 Unknown_Date 开头的文件: {filename}")
            continue

        # 提取标题
        title_part = filename[len("Unknown_Date_"):-4]
        if not title_part:
            if args.verbose:
                print(f"[-] 跳过空标题文件: {filename}")
            stats["error"] += 1
            continue

        clean_title = title_part
        suffix = ""
        matched_rows = []

        # 阶段 1: 尝试完整匹配 Title
        cursor.execute("SELECT id, publish_time, url, pdf_path FROM resources WHERE title = ?", (clean_title,))
        matched_rows = cursor.fetchall()

        # 阶段 2: 如果找不到，且以 _\d+ 结尾，尝试剥离后缀匹配
        if not matched_rows:
            match = re.search(r"_(?P<num>\d+)$", title_part)
            if match:
                suffix = match.group(0) # 比如 "_1"
                clean_title = title_part[:-len(suffix)]
                cursor.execute("SELECT id, publish_time, url, pdf_path FROM resources WHERE title = ?", (clean_title,))
                matched_rows = cursor.fetchall()

        if not matched_rows:
            # 尝试使用 LIKE 模糊匹配（辅助查找可能由于空格或微调导致名字不一致的）
            cursor.execute("SELECT id, publish_time, url, pdf_path, title FROM resources WHERE title LIKE ?", (f"%{clean_title}%",))
            like_rows = cursor.fetchall()
            if len(like_rows) == 1:
                matched_rows = [(like_rows[0][0], like_rows[0][1], like_rows[0][2], like_rows[0][3])]
            elif len(like_rows) > 1:
                # 筛选掉 publish_time 是 Unknown_Date 的再看看
                valid_like_rows = [r for r in like_rows if r[1] != 'Unknown_Date' and date_regex.match(r[1])]
                if len(valid_like_rows) == 1:
                    matched_rows = [(valid_like_rows[0][0], valid_like_rows[0][1], valid_like_rows[0][2], valid_like_rows[0][3])]

        # 处理匹配结果
        if not matched_rows:
            stats["not_found"] += 1
            if args.verbose:
                print(f"[NOT FOUND] 数据库中未找到匹配的资源: {filename}")
            continue

        # 检查多重匹配
        unique_dates = list(set(r[1] for r in matched_rows))
        
        if len(unique_dates) > 1:
            # 多重匹配且日期不同，尝试查找 pdf_path 为当前文件的记录
            matched_by_path = []
            for r in matched_rows:
                db_pdf_path = r[3]
                if db_pdf_path:
                    db_filename = os.path.basename(db_pdf_path.replace('\\', '/'))
                    if db_filename.lower() == filename.lower():
                        matched_by_path.append(r)
            
            if len(matched_by_path) == 1:
                # 找到了唯一的匹配记录，用它替换 matched_rows
                matched_rows = matched_by_path
                unique_dates = [matched_rows[0][1]]
                if args.verbose:
                    print(f"[RESOLVED] 文件 {filename} 对应多个日期，已通过 pdf_path 匹配到唯一记录: ID={matched_rows[0][0]}, Date={unique_dates[0]}")
            else:
                # 仍无法唯一定位，视为冲突
                stats["conflict"] += 1
                if args.verbose or True: # 冲突应当打印
                    print(f"[CONFLICT] 文件 {filename} 对应多个不同的数据库日期: {unique_dates}，且无法通过 pdf_path 唯一匹配。")
                continue

        # 此时所有匹配行都具有相同的日期
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

        # 匹配成功！
        stats["success"] += 1
        year = matched_date.split('-')[0]
        new_filename = f"{matched_date}_{clean_title}{suffix}.pdf"
        
        src_path = os.path.join(unknown_year_dir, filename)
        dst_dir = os.path.join(pdf_base, year)
        dst_path = os.path.join(dst_dir, new_filename)

        move_plans.append((src_path, dst_path, matched_ids, new_filename, matched_date))
        
        if args.verbose:
            print(f"[PLAN] {filename} -> {year}/{new_filename} (ID: {matched_ids})")

    # 执行移动和数据库更新
    print("\n" + "=" * 60)
    print(f"[*] 分析完成！准备处理 {len(move_plans)} 个文件...")
    
    do_run = args.run
    if not do_run:
        print("[*] 当前为预览模式，未执行任何物理文件移动或数据库修改。")
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
                # 检查目标文件夹是否存在
                dst_dir = os.path.dirname(dst_path)
                if not os.path.exists(dst_dir):
                    os.makedirs(dst_dir, exist_ok=True)
                
                # 移动文件
                os.rename(src_path, dst_path)
                
                # 更新数据库
                abs_dst_path = os.path.abspath(dst_path)
                id_placeholders = ",".join("?" for _ in matched_ids)
                cursor.execute(
                    f"UPDATE resources SET pdf_path = ? WHERE id IN ({id_placeholders})",
                    [abs_dst_path] + matched_ids
                )
                success_moved += 1
            except Exception as e:
                print(f"[-] 移动文件失败 {os.path.basename(src_path)}: {e}")
                stats["error"] += 1

        conn.commit()
        print(f"[+] 物理修复完成！成功移动并更新了 {success_moved} 个文件。")
    else:
        print("[*] 未执行任何物理文件移动或数据库修改。")

    conn.close()

    # 输出统计报告
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

    # 1. 扫描体积小于 20KB 的 PDF 文件
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

    # 2. 连接数据库，一次性查出所有有 pdf_path 的记录
    print("[*] 正在加载数据库中的 PDF 路径进行比对匹配...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, url, pdf_path, publish_time FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    db_rows = cursor.fetchall()
    
    # 建立文件名映射以优化查询速度
    db_map = {}
    for row in db_rows:
        db_pdf_path = row[3]
        if db_pdf_path:
            # 统一路径分隔符并取文件名（小写）
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

    # 3. 预览或询问是否正式下载
    do_run = args.run
    if not do_run:
        print("\n" + "=" * 60)
        print("                  需要重新下载的 PDF 预览")
        print("=" * 60)
        # 预览限制前 20 条，防止控制台刷屏
        preview_limit = 20
        for idx, (fp, sz, r_id, title, url, pub_time) in enumerate(to_download[:preview_limit], 1):
            print(f"[{idx}] 路径: {fp}")
            print(f"    大小: {sz:.2f} KB | ID: {r_id} | 标题: {title} | 日期: {pub_time}")
            print(f"    URL:  {url}")
            print("-" * 60)
        if len(to_download) > preview_limit:
            print(f"... 还有 {len(to_download) - preview_limit} 个文件未列出")
        print(f"\n[*] 预览结束。当前为预览模式，未重新下载。")
        
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

    # 正式下载流程
    print(f"\n[*] 准备拉起 Playwright 重新下载 {len(to_download)} 个 PDF 文件...")
    from playwright.sync_api import sync_playwright
    import random
    import time

    success_count = 0
    fail_count = 0

    try:
        with sync_playwright() as p:
            browser, context = _create_browser_context(p, viewport={'width': 1280, 'height': 900})

            for idx, (file_path, size_kb, r_id, title, url, publish_time) in enumerate(to_download, 1):
                print(f"\n[*] [{idx}/{len(to_download)}] 正在请求: {url} (当前大小: {size_kb:.2f} KB)")
                page = context.new_page()
                try:
                    # 访问页面
                    response = page.goto(url, timeout=45000, wait_until="domcontentloaded")
                    time.sleep(3.0)
                    
                    if response and response.status == 404:
                        print(f"  [-] 页面返回 404，不重新下载。")
                        fail_count += 1
                        continue
                    
                    # 屏蔽广告
                    try:
                        page.evaluate("""
                            () => {
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
                            }
                        """)
                    except Exception as eval_e:
                        if args.verbose:
                            print(f"  [!] 广告过滤脚本执行失败: {eval_e}")

                    # 生成并保存PDF，覆盖原路径
                    page.pdf(
                        path=file_path,
                        format="A4",
                        print_background=True,
                        margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"}
                    )

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

def main():
    setup_console_utf8()
    parser = argparse.ArgumentParser(description="修正 PDF 文件名称并移到正确的年份文件夹，并更新本地 SQLite 数据库中的路径。")
    parser.add_argument(
        "--run",
        action="store_true",
        default=False,
        help="正式运行修复，不加此参数时仅进行预览 (Dry Run)。"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="详细输出每个文件的分析计划。"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("[*] 请选择要运行的功能：")
    print("    1. 修正 PDF 文件名称并移到正确的年份文件夹 (原功能)")
    print("    2. 检测 PDF 保存目录中文件体积小于 20KB 的 PDF 并重新保存 (新功能)")
    print("=" * 60)
    
    try:
        choice = input("请输入序号 [1/2] (直接回车默认 1): ").strip()
        if not choice:
            choice = "1"
    except (KeyboardInterrupt, EOFError):
        print("\n[-] 运行已取消")
        sys.exit(0)

    if choice == "1":
        run_fix_names_and_paths(args)
    elif choice == "2":
        run_redownload_small_pdfs(args)
    else:
        print("[-] 错误: 输入的序号无效。")
        sys.exit(1)

if __name__ == "__main__":
    main()
