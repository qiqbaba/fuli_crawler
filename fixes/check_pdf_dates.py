"""
检查 PDF 文件与数据库日期的匹配情况并生成报告

⚠️ 已废弃: 本功能已合并到 fixes/pdf_maintenance.py，请使用以下命令代替:
    python fixes/pdf_maintenance.py check-dates
"""
import os
import re
import sys
import sqlite3
from collections import defaultdict

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 引入项目配置
from config import get_db_path, PDF_BASE_DIR
from utils import setup_console_utf8
from utils.pdf_utils import parse_filename, clean_title_suffix

def main():
    setup_console_utf8()
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
    conn.close()

    print(f"[+] 成功加载 {len(db_rows)} 条数据库记录。")

    # 3. 建立数据库索引
    # 索引 A: 根据 pdf_path 的文件名查找记录 (不区分大小写)
    db_by_pdf_filename = defaultdict(list)
    # 索引 B: 根据 title 查找记录 (精确匹配)
    db_by_title = defaultdict(list)

    for row in db_rows:
        r_id, r_title, r_publish_time, r_pdf_path, r_url = row
        # 统一处理 publish_time
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

        # 索引 A
        if r_pdf_path:
            filename_part = os.path.basename(r_pdf_path.replace('\\', '/')).lower()
            db_by_pdf_filename[filename_part].append(record)
        
        # 索引 B
        if r_title:
            db_by_title[r_title.strip()].append(record)

    # 4. 对每个物理文件进行匹配和比对
    print("[*] 正在比对文件与数据库...")
    
    results = {
        "matched_ok": [],       # 正常匹配一致
        "date_mismatch": [],    # 匹配到了但日期不一致
        "db_not_found": [],     # 数据库中找不到对应记录
        "multiple_conflict": [] # 匹配到多重记录，无法确定
    }

    for filename, full_path in pdf_files:
        # 解析物理文件名
        fn_date, fn_title_part = parse_filename(filename)
        fn_clean_title = clean_title_suffix(fn_title_part)

        # 尝试匹配记录
        matched_records = []

        # 优先使用 pdf_path 文件名匹配
        if filename.lower() in db_by_pdf_filename:
            matched_records = db_by_pdf_filename[filename.lower()]
        
        # 如果没有匹配到，尝试用 title 匹配
        if not matched_records:
            # 尝试用带后缀的 title_part 匹配
            if fn_title_part in db_by_title:
                matched_records = db_by_title[fn_title_part]
            # 尝试用干净的 clean_title 匹配
            elif fn_clean_title in db_by_title:
                matched_records = db_by_title[fn_clean_title]

        # 如果还是没有，尝试去数据库中模糊查找或者作为 Not Found 处理
        if not matched_records:
            results["db_not_found"].append({
                "filename": filename,
                "path": full_path,
                "fn_date": fn_date,
                "clean_title": fn_clean_title
            })
            continue

        # 此时有匹配的记录，检查日期
        # 如果匹配到多条记录，但它们有不同的 publish_time，我们要先看看是否有 pdf_path 明确指定的
        unique_dates = list(set(r["publish_time"] for r in matched_records))

        if len(unique_dates) > 1:
            # 如果有多个不同日期，先找一找是否有一个记录的 pdf_path basename 精确匹配该文件名
            exact_by_path = []
            for r in matched_records:
                if r["pdf_path"]:
                    basename = os.path.basename(r["pdf_path"].replace('\\', '/')).lower()
                    if basename == filename.lower():
                        exact_by_path.append(r)
            
            if len(exact_by_path) == 1:
                # 通过路径精确匹配到了唯一一条
                record = exact_by_path[0]
                db_date = record["publish_time"]
                
                # 比对日期
                if fn_date == db_date:
                    results["matched_ok"].append((filename, full_path, record))
                else:
                    results["date_mismatch"].append({
                        "filename": filename,
                        "path": full_path,
                        "fn_date": fn_date,
                        "db_date": db_date,
                        "record": record
                    })
            else:
                # 冲突，无法确定
                results["multiple_conflict"].append({
                    "filename": filename,
                    "path": full_path,
                    "fn_date": fn_date,
                    "matched_records": matched_records
                })
        else:
            # 所有匹配的记录都具有相同的日期
            record = matched_records[0] # 取第一条作为代表
            db_date = unique_dates[0]

            # 统一将 None 和 Unknown_Date 进行比较
            cmp_fn_date = fn_date if fn_date else "Unknown_Date"
            cmp_db_date = db_date if db_date else "Unknown_Date"

            if cmp_fn_date == cmp_db_date:
                results["matched_ok"].append((filename, full_path, record))
            else:
                results["date_mismatch"].append({
                    "filename": filename,
                    "path": full_path,
                    "fn_date": fn_date,
                    "db_date": db_date,
                    "record": record,
                    "all_matched_records": matched_records # 供参考
                })

    # 5. 生成报告
    from datetime import datetime
    report_lines = []
    report_lines.append("# PDF 文件日期与数据库日期检查报告\n")
    report_lines.append(f"- **检查时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append(f"- **扫描 PDF 物理文件数**: {total_phys_files}")
    report_lines.append(f"- **正常一致文件数**: {len(results['matched_ok'])}")
    report_lines.append(f"- **日期不符文件数**: {len(results['date_mismatch'])}")
    report_lines.append(f"- **数据库中未找到记录的文件数**: {len(results['db_not_found'])}")
    report_lines.append(f"- **多重匹配冲突文件数**: {len(results['multiple_conflict'])}")
    report_lines.append("\n" + "="*40 + "\n")

    if results["date_mismatch"]:
        report_lines.append("## 1. 日期不符文件列表 (文件名中日期 vs 数据库日期)\n")
        report_lines.append("| 序号 | 物理文件名 | 文件中提取日期 | 数据库中日期 | 数据库记录ID | 物理路径 |")
        report_lines.append("| --- | --- | --- | --- | --- | --- |")
        for i, item in enumerate(results["date_mismatch"], 1):
            fn = item["filename"]
            fn_d = item["fn_date"] if item["fn_date"] else "无"
            db_d = item["db_date"]
            rec_id = item["record"]["id"]
            rel_path = os.path.relpath(item["path"], pdf_base)
            report_lines.append(f"| {i} | `{fn}` | `{fn_d}` | `{db_d}` | `{rec_id}` | `pdf/{rel_path}` |")
        report_lines.append("\n")

    if results["db_not_found"]:
        report_lines.append("## 2. 数据库中未找到匹配记录的文件列表\n")
        report_lines.append("| 序号 | 物理文件名 | 提取日期 | 提取标题 | 物理路径 |")
        report_lines.append("| --- | --- | --- | --- | --- |")
        for i, item in enumerate(results["db_not_found"], 1):
            fn = item["filename"]
            fn_d = item["fn_date"] if item["fn_date"] else "无"
            title = item["clean_title"]
            rel_path = os.path.relpath(item["path"], pdf_base)
            report_lines.append(f"| {i} | `{fn}` | `{fn_d}` | `{title}` | `pdf/{rel_path}` |")
        report_lines.append("\n")

    if results["multiple_conflict"]:
        report_lines.append("## 3. 多重匹配冲突文件列表 (匹配到多个不同日期的记录)\n")
        report_lines.append("| 序号 | 物理文件名 | 文件提取日期 | 匹配到的数据库日期 | 匹配到的记录ID列表 | 物理路径 |")
        report_lines.append("| --- | --- | --- | --- | --- | --- |")
        for i, item in enumerate(results["multiple_conflict"], 1):
            fn = item["filename"]
            fn_d = item["fn_date"] if item["fn_date"] else "无"
            dates = [r["publish_time"] for r in item["matched_records"]]
            ids = [r["id"] for r in item["matched_records"]]
            rel_path = os.path.relpath(item["path"], pdf_base)
            report_lines.append(f"| {i} | `{fn}` | `{fn_d}` | `{dates}` | `{ids}` | `pdf/{rel_path}` |")
        report_lines.append("\n")

    # 写到报告文件
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_date_check_report.md")
    with open(report_path, "w", encoding="utf-8") as rf:
        rf.write("\n".join(report_lines))

    # 控制台打印摘要
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

if __name__ == "__main__":
    main()
