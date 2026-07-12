import os
import re
import sys
import sqlite3
import argparse
import random
import time
from playwright.sync_api import sync_playwright

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 引入项目中的配置
from config import get_db_path, PDF_BASE_DIR
from utils import setup_console_utf8
from utils.metadata_parser import sanitize_filename
from utils.pdf_utils import to_relative_path
from utils.browser_manager import create_browser_context

def main():
    setup_console_utf8()
    parser = argparse.ArgumentParser(description="修复本地缺失的 PDF 文件，并将所有 pdf_path 改为相对路径。")
    parser.add_argument(
        "--run",
        action="store_true",
        default=False,
        help="正式执行修复和更新，不加此参数时仅进行预览 (Dry Run)。"
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        default=False,
        help="仅执行路径相对化和纠偏，不重新下载物理缺失的文件。"
    )
    args = parser.parse_args()

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

    # 1. 扫描当前物理存在的文件（大小写不敏感匹配，物理路径转换为绝对路径）
    print("[*] 正在扫描 PDF 物理文件...")
    phys_files = set()
    if os.path.exists(base_dir):
        for root, dirs, files in os.walk(base_dir):
            for f in files:
                if f.lower().endswith(".pdf"):
                    phys_files.add(os.path.abspath(os.path.join(root, f)).lower())
    print(f"[+] 物理目录中现存的 PDF 文件数: {len(phys_files)}")

    # 2. 连接数据库
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 3. 找出所有 pdf_path 不为空的记录
    cursor.execute("SELECT id, title, pdf_path, url, publish_time FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    rows = cursor.fetchall()
    print(f"[*] 数据库中含有 pdf_path 的总记录数: {len(rows)}")

    needs_update_to_relative = [] # 存储 (id, relative_path)
    missing_records = []          # 存储 (id, title, url, publish_time)

    for r_id, title, pdf_path, url, publish_time in rows:
        rel_path = to_relative_path(pdf_path)
        # 本地应该映射到的绝对路径
        norm_abs_path = os.path.abspath(os.path.join(project_dir, rel_path))
        
        if norm_abs_path.lower() in phys_files:
            # 物理文件存在，如果数据库记录的不是这个相对路径格式，就加入更新队列
            if pdf_path != rel_path:
                needs_update_to_relative.append((r_id, rel_path))
        else:
            # 物理文件确实不在
            missing_records.append((r_id, title, url, publish_time))

    print(f"[*] 需要转换为相对路径（且物理文件已存在）的记录数: {len(needs_update_to_relative)}")
    print(f"[*] 真正物理缺失（本地无文件）的记录数: {len(missing_records)}")
    print("=" * 60)

    # --- 执行数据库路径相对化纠偏 ---
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

    # --- 执行重新下载缺失文件 ---
    if missing_records:
        if args.skip_download:
            print("[*] 参数指定跳过重新下载缺失文件。")
        elif not args.run:
            print(f"\n[PLAN] 发现 {len(missing_records)} 个物理缺失的文件（示例前 5 个）：")
            for r_id, title, url, publish_time in missing_records[:5]:
                print(f"  - ID: {r_id} | 标题: {title} | 日期: {publish_time}")
                print(f"    URL: {url}")
            print("[*] 提示: 预览模式下未下载任何文件。若要正式开始修复，请运行: python fixes/rebuild_missing_pdfs.py --run")
        else:
            print(f"\n[*] 准备拉起 Playwright 重新下载 {len(missing_records)} 个缺失的 PDF...")
            success_download = 0
            fail_download = 0
            not_found_download = 0
            
            try:
                with sync_playwright() as p:
                    browser, context = create_browser_context(p)
                    
                    for idx, (r_id, title, url, publish_time) in enumerate(missing_records, 1):
                        year = publish_time.split('-')[0] if (publish_time and '-' in publish_time) else "Unknown_Year"
                        save_dir = os.path.join(base_dir, year)
                        os.makedirs(save_dir, exist_ok=True)
                        
                        safe_title = sanitize_filename(title)
                        pub_date = publish_time if publish_time else "Unknown_Date"
                        base_filename = f"{pub_date}_{safe_title}"
                        pdf_path = os.path.join(save_dir, f"{base_filename}.pdf")
                        
                        # 处理重名
                        counter = 1
                        while os.path.exists(pdf_path):
                            pdf_path = os.path.join(save_dir, f"{base_filename}_{counter}.pdf")
                            counter += 1
                            
                        page = context.new_page()
                        try:
                            print(f"[*] [{idx}/{len(missing_records)}] 正在请求 URL: {url}")
                            response = page.goto(url, timeout=60000, wait_until="domcontentloaded")
                            page.wait_for_load_state("load", timeout=10000)
                            
                            if response and response.status == 404:
                                print(f"  [-] 页面 404 未找到，清除该记录的 pdf_path。")
                                cursor.execute("UPDATE resources SET pdf_path = '' WHERE id = ?", (r_id,))
                                not_found_download += 1
                            else:
                                page.pdf(path=pdf_path, format="A4", print_background=True)
                                
                                # 将下载成功的文件路径转换为相对路径格式写入数据库
                                rel_pdf_path = f"pdf/{year}/{os.path.basename(pdf_path)}"
                                cursor.execute("UPDATE resources SET pdf_path = ? WHERE id = ?", (rel_pdf_path, r_id))
                                success_download += 1
                                print(f"  [+] 下载成功并更新数据库为相对路径: {rel_pdf_path}")
                                
                            conn.commit() # 每次成功即提交，防止中途异常退出丢失进度
                            
                        except Exception as e:
                            print(f"  [-] 下载物理文件失败 ID: {r_id} | 错误: {e}")
                            fail_download += 1
                        finally:
                            page.close()
                            
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

if __name__ == "__main__":
    main()
