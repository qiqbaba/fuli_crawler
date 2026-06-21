import os
import re
import sys
import sqlite3
import argparse
from collections import defaultdict

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 引入项目配置
from config import get_db_path, PDF_BASE_DIR

# Windows下控制台强制使用utf-8编码输出，防止中文乱码
if sys.platform.startswith('win'):
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

def parse_filename(filename):
    """
    解析 PDF 文件名，提取日期前缀和标题。
    """
    if filename.lower().endswith('.pdf'):
        name_part = filename[:-4]
    else:
        name_part = filename

    # 匹配 YYYY-MM-DD
    date_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.*)$")
    match = date_pattern.match(name_part)
    if match:
        return match.group(1), match.group(2)

    # 匹配 Unknown_Date
    if name_part.startswith("Unknown_Date_"):
        return "Unknown_Date", name_part[len("Unknown_Date_"):]

    return None, name_part

def clean_title_suffix(title_part):
    """
    剥离标题中可能含有的数字后缀，如 _1, _2 等
    """
    match = re.search(r"_(?P<num>\d+)$", title_part)
    if match:
        suffix = match.group(0)
        return title_part[:-len(suffix)]
    return title_part

def generate_unique_path(target_dir, base_name):
    """
    在目标目录下生成一个唯一的文件路径（避免重名覆盖）
    """
    name, ext = os.path.splitext(base_name)
    target_path = os.path.join(target_dir, base_name)
    counter = 1
    while os.path.exists(target_path):
        new_name = f"{name}_{counter}{ext}"
        target_path = os.path.join(target_dir, new_name)
        counter += 1
    return target_path

def main():
    parser = argparse.ArgumentParser(description="自动修复 PDF 文件名中的日期与数据库不符的情况。")
    parser.add_argument(
        "--run",
        action="store_true",
        default=False,
        help="正式运行修复，不加此参数时仅进行预览 (Dry Run)。"
    )
    args = parser.parse_args()

    db_path = get_db_path()
    pdf_base = PDF_BASE_DIR

    print("=" * 60)
    print(f"[*] 运行模式: {'【正式修复模式】' if args.run else '【预览模式 (Dry Run)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)

    if not os.path.exists(pdf_base):
        print(f"[-] 错误: PDF 根目录不存在: {pdf_base}")
        sys.exit(1)

    # 1. 扫描所有的 PDF 物理文件
    print("[*] 正在扫描 PDF 目录...")
    pdf_files = []
    for root, dirs, files in os.walk(pdf_base):
        for f in files:
            if f.lower().endswith(".pdf"):
                full_path = os.path.join(root, f)
                pdf_files.append((f, full_path))
    
    total_phys_files = len(pdf_files)
    print(f"[+] 扫描到 {total_phys_files} 个 PDF 物理文件。")

    # 2. 连接数据库并加载全部 resources 记录
    print("[*] 正在加载数据库中的资源记录...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, publish_time, pdf_path, url FROM resources")
    db_rows = cursor.fetchall()

    print(f"[+] 成功加载 {len(db_rows)} 条数据库记录。")

    # 3. 建立数据库索引
    db_by_pdf_filename = defaultdict(list)
    db_by_title = defaultdict(list)

    for row in db_rows:
        r_id, r_title, r_publish_time, r_pdf_path, r_url = row
        pub_time = r_publish_time.strip() if r_publish_time else "Unknown_Date"
        if not pub_time:
            pub_time = "Unknown_Date"

        record = {
            "id": r_id,
            "title": r_title,
            "publish_time": pub_time,
            "pdf_path": r_pdf_path,
            "url": r_url
        }

        if r_pdf_path:
            filename_part = os.path.basename(r_pdf_path.replace('\\', '/')).lower()
            db_by_pdf_filename[filename_part].append(record)
        
        if r_title:
            db_by_title[r_title.strip()].append(record)

    # 4. 对比文件与数据库，筛选需要修复的文件
    print("[*] 正在分析需要修复的文件...")
    
    mismatches = []
    
    for filename, full_path in pdf_files:
        fn_date, fn_title_part = parse_filename(filename)
        fn_clean_title = clean_title_suffix(fn_title_part)

        matched_records = []

        # 优先使用 pdf_path 文件名匹配
        if filename.lower() in db_by_pdf_filename:
            matched_records = db_by_pdf_filename[filename.lower()]
        
        # 如果没有匹配到，尝试用 title 匹配
        if not matched_records:
            if fn_title_part in db_by_title:
                matched_records = db_by_title[fn_title_part]
            elif fn_clean_title in db_by_title:
                matched_records = db_by_title[fn_clean_title]

        if not matched_records:
            continue

        unique_dates = list(set(r["publish_time"] for r in matched_records))

        if len(unique_dates) > 1:
            # 尝试通过 path 唯一锁定
            exact_by_path = []
            for r in matched_records:
                if r["pdf_path"]:
                    basename = os.path.basename(r["pdf_path"].replace('\\', '/')).lower()
                    if basename == filename.lower():
                        exact_by_path.append(r)
            if len(exact_by_path) == 1:
                record = exact_by_path[0]
                db_date = record["publish_time"]
                if fn_date != db_date:
                    mismatches.append((filename, full_path, db_date, [record]))
        else:
            # 只有一种日期
            db_date = unique_dates[0]
            cmp_fn_date = fn_date if fn_date else "Unknown_Date"
            cmp_db_date = db_date if db_date else "Unknown_Date"
            
            if cmp_fn_date != cmp_db_date:
                mismatches.append((filename, full_path, db_date, matched_records))

    total_mismatches = len(mismatches)
    print(f"[+] 分析完成！共发现 {total_mismatches} 个文件名日期与数据库不符的物理文件。")

    if total_mismatches == 0:
        print("[*] 没有需要修复的文件。")
        conn.close()
        return

    # 5. 生成修复计划并执行
    print("\n" + "=" * 60)
    print(f"[*] 准备处理 {total_mismatches} 个文件...")
    
    success_count = 0
    
    for filename, src_path, db_date, matched_records in mismatches:
        # 提取正确的年份
        # 如果数据库里的日期依然不是 YYYY-MM-DD，而是 Unknown_Date，则年份为 Unknown_Year
        if db_date and re.match(r"^\d{4}-\d{2}-\d{2}$", db_date):
            target_year = db_date.split('-')[0]
        else:
            target_year = "Unknown_Year"
            db_date = "Unknown_Date"

        # 生成新文件名
        fn_date, fn_title_part = parse_filename(filename)
        # 将原文件名的日期前缀替换为数据库中的正确日期
        new_filename = f"{db_date}_{fn_title_part}.pdf"

        target_dir = os.path.join(pdf_base, target_year)
        
        # 预览或实际查找不重名的路径
        if args.run:
            if not os.path.exists(target_dir):
                os.makedirs(target_dir, exist_ok=True)
            dst_path = generate_unique_path(target_dir, new_filename)
        else:
            # 预览模式下做模拟防重名
            dst_path = os.path.join(target_dir, new_filename)
            if os.path.exists(dst_path):
                # 模拟加后缀
                name, ext = os.path.splitext(new_filename)
                dst_path = os.path.join(target_dir, f"{name}_1{ext}")

        rel_src = os.path.relpath(src_path, pdf_base)
        rel_dst = os.path.relpath(dst_path, pdf_base)
        matched_ids = [r["id"] for r in matched_records]
        
        print(f"[PLAN] ID: {matched_ids} | 原路径: pdf/{rel_src} -> 新路径: pdf/{rel_dst}")

        if args.run:
            try:
                # 物理重命名/移动
                os.rename(src_path, dst_path)
                
                # 更新数据库中的路径（绝对路径）
                abs_dst_path = os.path.abspath(dst_path)
                id_placeholders = ",".join("?" for _ in matched_ids)
                cursor.execute(
                    f"UPDATE resources SET pdf_path = ? WHERE id IN ({id_placeholders})",
                    [abs_dst_path] + matched_ids
                )
                success_count += 1
            except Exception as e:
                print(f"  [-] 物理修复文件失败: {e}")

    if args.run:
        conn.commit()
        print(f"\n[+] 修复执行完成！成功移动并更新了 {success_count}/{total_mismatches} 个文件。")
    else:
        print("\n[*] 当前为预览模式，没有执行任何物理重命名或数据库更新。")
        print("[*] 若要开始实际修复，请运行: python fix_date_mismatches.py --run")

    conn.close()

if __name__ == "__main__":
    main()
