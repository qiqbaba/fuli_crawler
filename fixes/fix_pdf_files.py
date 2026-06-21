import os
import re
import sys
import sqlite3
import argparse

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 引入项目中的配置
from config import get_db_path, PDF_BASE_DIR

# Windows下控制台强制使用utf-8编码输出，防止中文乱码
if sys.platform.startswith('win'):
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

def main():
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
    # 格式: (src_path, dst_path, list_of_ids, new_filename, matched_time)
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
    
    if args.run:
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
        print("[*] 当前为预览模式，未执行任何物理文件移动或数据库修改。")
        print("[*] 如果确认计划无误，请运行命令: python fix_pdf_files.py --run")

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

if __name__ == "__main__":
    main()
