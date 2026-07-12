import os
import sys
import sqlite3
import argparse

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 引入项目中的配置
from config import get_db_path, PDF_BASE_DIR
from utils import setup_console_utf8

def main():
    setup_console_utf8()
    parser = argparse.ArgumentParser(description="清理数据库中已物理删除的 Unknown_Year PDF 文件对应的记录。")
    parser.add_argument(
        "--run",
        action="store_true",
        default=False,
        help="正式执行删除，不加此参数时仅进行预览 (Dry Run)。"
    )
    args = parser.parse_args()

    db_path = get_db_path()
    unknown_year_dir = os.path.join(PDF_BASE_DIR, "Unknown_Year")

    print("=" * 60)
    print(f"[*] 运行模式: {'【正式删除模式】' if args.run else '【预览模式 (Dry Run)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] 未知年份 PDF 目录: {unknown_year_dir}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)

    if not os.path.exists(unknown_year_dir):
        print(f"[-] 错误: 未知年份目录不存在: {unknown_year_dir}")
        sys.exit(1)

    # 1. 扫描当前物理存在的文件（大小写不敏感匹配）
    existing_files = {f.lower() for f in os.listdir(unknown_year_dir) if f.endswith(".pdf")}
    print(f"[*] 物理目录中现存 of PDF 文件数: {len(existing_files)}")

    # 2. 连接数据库
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 3. 找出所有 pdf_path 包含 Unknown_Year 的记录
    cursor.execute("SELECT id, title, pdf_path FROM resources WHERE pdf_path LIKE '%Unknown_Year%'")
    rows = cursor.fetchall()
    print(f"[*] 数据库中 pdf_path 包含 'Unknown_Year' 的记录数: {len(rows)}")

    to_delete = []
    for r_id, title, pdf_path in rows:
        if not pdf_path:
            continue
        # 提取文件名
        filename = os.path.basename(pdf_path.replace('\\', '/'))
        if filename.lower() not in existing_files:
            to_delete.append((r_id, title, pdf_path))

    print(f"[*] 发现已被物理删除但数据库中依然存在的记录数: {len(to_delete)}")
    print("=" * 60)

    if not to_delete:
        print("[+] 没有需要清理的记录。")
        conn.close()
        return

    # 打印待删除列表
    for idx, (r_id, title, pdf_path) in enumerate(to_delete, 1):
        print(f"[{idx}] ID: {r_id} | 标题: {title} | 路径: {pdf_path}")

    print("=" * 60)

    if args.run:
        print("[*] 开始从数据库删除这些记录...")
        delete_ids = [item[0] for item in to_delete]
        
        # 分批删除或一次性删除（记录不多时可以直接用 IN 语句）
        id_placeholders = ",".join("?" for _ in delete_ids)
        cursor.execute(f"DELETE FROM resources WHERE id IN ({id_placeholders})", delete_ids)
        conn.commit()
        print(f"[+] 成功删除了 {len(delete_ids)} 条记录！")
    else:
        print("[*] 当前为预览模式，未执行任何删除操作。")
        print("[*] 如果确认要删除以上记录，请运行命令: python clean_deleted_records.py --run")

    conn.close()

if __name__ == "__main__":
    main()
