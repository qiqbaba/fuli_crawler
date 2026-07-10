"""
日语标题过滤脚本

从数据库中扫描所有标题，通过语言检测识别日语标题后，提供两种操作：
  1. 导出匹配记录到新数据库（不含非日语记录）
  2. 删除匹配记录（同时删除对应 PDF 文件）

用法:
  python fixes/filter_fanhao_lang.py                        # 交互式模式（逐步询问）
  python fixes/filter_fanhao_lang.py --interactive           # 显式进入交互式模式
  python fixes/filter_fanhao_lang.py --mode export           # 导出日语记录到新库
  python fixes/filter_fanhao_lang.py --mode delete           # 删除日语记录 (含 PDF)
  python fixes/filter_fanhao_lang.py --mode export --dry-run # 预览导出
  python fixes/filter_fanhao_lang.py --mode delete --dry-run # 预览删除
"""

import os
import sys
import shutil
import sqlite3
import argparse
from datetime import datetime

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_db_path, PDF_BASE_DIR

# Windows 控制台 UTF-8 输出
if sys.platform.startswith('win'):
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass


# ============================================================
# 1. 日语检测（复用 utils.lang_filter 的共享实现）
# ============================================================

from utils.lang_filter import is_japanese, _LINGUA_AVAILABLE


# ============================================================
# 2. 数据库操作
# ============================================================

def get_columns(cursor):
    """获取 resources 表所有列名"""
    cursor.execute("PRAGMA table_info(resources)")
    cols = [row[1] for row in cursor.fetchall()]
    return cols


def scan_japanese_records(conn):
    """
    扫描数据库，返回日语标题的记录列表。
    每项为 (matched_text_prefix, row_data_dict)
    matched_text_prefix 为标题前 60 个字符，方便展示。
    """
    cursor = conn.cursor()
    columns = get_columns(cursor)
    cursor.execute(f"SELECT {', '.join(columns)} FROM resources ORDER BY id")
    rows = cursor.fetchall()

    matches = []
    for row in rows:
        row_dict = dict(zip(columns, row))
        title = row_dict.get('title', '')
        if is_japanese(title):
            # 取标题前 60 字符作为标识
            title_prefix = title[:60]
            matches.append((title_prefix, row_dict))

    return matches, columns


def export_to_new_db(matches, columns, output_path):
    """将匹配的记录导出到新的 SQLite 数据库"""
    print(f"\n[*] 正在导出 {len(matches)} 条记录到: {output_path}")

    if os.path.exists(output_path):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{output_path}.bak_{timestamp}"
        shutil.copy2(output_path, backup)
        print(f"[*] 已备份原有数据库: {backup}")

    conn = sqlite3.connect(output_path)
    cursor = conn.cursor()

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS resources (
            {', '.join(f'{col} TEXT' if col != 'id' else 'id INTEGER PRIMARY KEY' for col in columns)}
        )
    ''')

    col_list = ', '.join(columns)
    placeholders = ', '.join(['?' for _ in columns])

    cursor.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_url ON resources(url)")

    for _, row_dict in matches:
        values = [row_dict.get(col) for col in columns]
        cursor.execute(
            f"INSERT OR IGNORE INTO resources ({col_list}) VALUES ({placeholders})",
            values
        )

    conn.commit()
    print(f"[+] 导出完成！新数据库共 {len(matches)} 条记录")
    conn.close()


def delete_records(conn, matches, dry_run=False):
    """删除匹配的记录，并可选删除对应的 PDF 文件"""
    ids_to_delete = [row_dict['id'] for _, row_dict in matches]
    pdf_files_to_delete = []

    for _, row_dict in matches:
        pdf_path = row_dict.get('pdf_path', '')
        if pdf_path:
            # 支持相对路径和绝对路径
            abs_path = pdf_path
            if not os.path.isabs(pdf_path):
                project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                abs_path = os.path.join(project_dir, pdf_path)
            if os.path.exists(abs_path):
                pdf_files_to_delete.append(abs_path)

    print(f"\n{'='*60}")
    print(f"[操作] 准备删除 {len(ids_to_delete)} 条数据库记录")
    if pdf_files_to_delete:
        print(f"[操作] 同时删除 {len(pdf_files_to_delete)} 个 PDF 文件")

    if dry_run:
        print("\n[预览模式] 以下记录将被删除:")
        for i, (_, row_dict) in enumerate(matches, 1):
            title = row_dict.get('title', '')[:60]
            pdf = row_dict.get('pdf_path', '')
            print(f"  {i:4d}. {title}")
            if pdf:
                print(f"       PDF: {pdf}")
        print(f"\n[预览] 共 {len(ids_to_delete)} 条记录, {len(pdf_files_to_delete)} 个 PDF 文件将被删除")
        return

    confirm = input(f"\n[?] 确定要删除这 {len(ids_to_delete)} 条记录 {'和 '+str(len(pdf_files_to_delete))+' 个 PDF 文件' if pdf_files_to_delete else ''}？(yes/NO): ").strip().lower()
    if confirm != 'yes':
        print("[-] 已取消操作")
        return

    cursor = conn.cursor()

    deleted_pdfs = 0
    failed_pdfs = 0
    for pdf_path in pdf_files_to_delete:
        try:
            os.remove(pdf_path)
            deleted_pdfs += 1
            print(f"  [删除PDF] {pdf_path}")
        except Exception as e:
            failed_pdfs += 1
            print(f"  [-删除PDF失败] {pdf_path}: {e}")

    deleted_count = 0
    batch_size = 500
    for i in range(0, len(ids_to_delete), batch_size):
        batch = ids_to_delete[i:i+batch_size]
        placeholders = ','.join(['?' for _ in batch])
        cursor.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", batch)
        deleted_count += cursor.rowcount

    conn.commit()

    # 回收磁盘空间
    print("[*] 正在压缩数据库以回收磁盘空间...")
    cursor.execute("VACUUM")
    print("[+] 数据库压缩完成")

    print(f"\n[+] 操作完成！")
    print(f"  删除数据库记录: {deleted_count} 条")
    print(f"  删除 PDF 文件: {deleted_pdfs} 个{' (失败: {})'.format(failed_pdfs) if failed_pdfs else ''}")
    print(f"  数据库中剩余记录: {get_total_count(conn)} 条")


def get_total_count(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM resources")
    return cursor.fetchone()[0]


# ============================================================
# 3. 交互式控制台询问
# ============================================================

def ask_yes_no(prompt, default=False):
    """交互式 yes/no 询问"""
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} ({hint}): ").strip().lower()
        if not answer:
            return default
        if answer in ('y', 'yes'):
            return True
        if answer in ('n', 'no'):
            return False
        print("  请输入 y 或 n。")


def ask_choice(prompt, choices, default=None):
    """交互式选项选择"""
    print(f"\n{prompt}")
    for i, (key, desc) in enumerate(choices, 1):
        marker = " [默认]" if key == default else ""
        print(f"  {i}. {key} - {desc}{marker}")
    while True:
        answer = input(f"  请输入选项 ({'/'.join(k for k, _ in choices)}): ").strip().lower()
        if not answer and default:
            return default
        if answer in (k for k, _ in choices):
            return answer
        if answer.isdigit():
            idx = int(answer) - 1
            if 0 <= idx < len(choices):
                return choices[idx][0]
        print(f"  无效选项，请重新输入。")


def ask_path(prompt, default=None, must_exist=False):
    """交互式路径输入"""
    while True:
        answer = input(f"{prompt}{' (直接回车使用默认值)' if default else ''}: ").strip()
        if not answer and default:
            return default
        if not answer:
            print("  路径不能为空。")
            continue
        if must_exist and not os.path.exists(answer):
            print(f"  路径不存在: {answer}")
            continue
        return answer


def show_results_preview(matches):
    """显示匹配结果的预览列表"""
    print(f"\n{'='*60}")
    print(f"[*] 匹配到 {len(matches)} 条日语标题记录:")
    print(f"{'='*60}")

    show_detail = ask_yes_no("\n[*] 是否查看详细匹配列表?", False)
    if show_detail:
        print(f"\n[*] 匹配详情:")
        for i, (title_prefix, row_dict) in enumerate(matches, 1):
            title = title_prefix[:70]
            url = row_dict.get('url', '')
            print(f"  {i:4d}. {title}")
            if url:
                print(f"       URL: {url}")
            if i >= 100:
                print(f"  ... 及另外 {len(matches) - 100} 条")
                break


def interactive_mode():
    """交互式控制台模式：逐步询问用户"""
    print(f"\n{'='*60}")
    print("  日语标题过滤工具 - 交互式模式")
    print(f"{'='*60}\n")

    if not _LINGUA_AVAILABLE:
        print("[!] 警告: lingua-language-detector 未安装，将仅基于假名(平假名/片假名)判断日语。")
        print("    若要更精确的检测，请执行: pip install lingua-language-detector\n")

    # 1. 询问数据库路径
    default_db = get_db_path()
    db_path = ask_path("请输入数据库路径", default=default_db, must_exist=True)
    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    total = get_total_count(conn)
    print(f"\n[*] 数据库总记录数: {total}")

    print("[*] 正在扫描所有标题中的日语文本...")
    matches, columns = scan_japanese_records(conn)
    print(f"[*] 找到日语标题记录: {len(matches)} 条 ({len(matches)/total*100:.1f}%)")

    if not matches:
        print("[*] 没有找到任何日语标题的记录，退出。")
        conn.close()
        return

    conn.close()

    show_results_preview(matches)

    # 2. 询问操作模式
    print(f"\n{'='*60}")
    mode = ask_choice(
        "请选择操作模式:",
        [("export", "导出匹配记录到新数据库"), ("delete", "删除匹配记录 (含PDF)")],
        default="export"
    )

    # 3. 询问是否预览
    dry_run = ask_yes_no("是否仅预览 (不执行实际操作)?", default=False)

    # 4. 如果导出模式，询问输出路径
    output_path = None
    if mode == "export":
        base_dir = os.path.dirname(db_path)
        base_name = os.path.splitext(os.path.basename(db_path))[0]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_output = os.path.join(base_dir, f"{base_name}_japanese_only_{timestamp}.db")
        output_path = ask_path("请输入导出数据库路径", default=default_output)

    # 5. 询问是否显示详细日志
    verbose = ask_yes_no("是否显示每条记录的匹配详情?", False)

    # 6. 确认执行
    print(f"\n{'='*60}")
    print(f"[*] 操作确认:")
    print(f"  数据库:     {db_path}")
    print(f"  操作模式:   {'导出' if mode == 'export' else '删除'}")
    print(f"  预览模式:   {'是 (不实际执行)' if dry_run else '否'}")
    print(f"  匹配记录:   {len(matches)} 条")
    if mode == "export" and output_path:
        print(f"  输出路径:   {output_path}")
    if verbose:
        print(f"  详细日志:   是")
    print(f"{'='*60}")

    if not ask_yes_no("\n确认执行以上操作?", default=False):
        print("[-] 已取消操作")
        return

    # 7. 执行
    if mode == "export":
        if dry_run:
            print("\n[预览模式] 导出预览:")
            for i, (_, row_dict) in enumerate(matches, 1):
                title = row_dict.get('title', '')[:60]
                print(f"  {i:4d}. {title}")
            print(f"\n[预览] 共 {len(matches)} 条记录【将会】导出到: {output_path}")
        else:
            export_to_new_db(matches, columns, output_path)
    else:
        conn = sqlite3.connect(db_path)
        delete_records(conn, matches, dry_run=dry_run)
        conn.close()

    print(f"\n{'='*60}")
    print("[+] 操作成功完成！")
    print(f"{'='*60}")


# ============================================================
# 4. 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="日语标题过滤工具 - 识别并导出/删除数据库中标题为日语的记录"
    )
    parser.add_argument(
        '--interactive', action='store_true',
        help='进入交互式模式'
    )
    parser.add_argument(
        '--mode', choices=['export', 'delete'],
        help='操作模式: export(导出) 或 delete(删除)'
    )
    parser.add_argument(
        '--db', type=str, default=None,
        help='SQLite 数据库路径 (默认自动检测)'
    )
    parser.add_argument(
        '--output', type=str, default=None,
        help='导出模式下的输出数据库路径'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='预览模式: 仅显示将要执行的操作，不实际修改数据'
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='显示每条记录的详细匹配信息'
    )

    args = parser.parse_args()

    # 如果未提供任何参数，或指定了 --interactive，进入交互式模式
    if args.interactive or not (args.mode or args.db or args.output or args.dry_run):
        interactive_mode()
        return

    # 非交互式模式
    if not args.mode:
        print("[-] 请指定 --mode (export/delete) 或使用 --interactive 进入交互模式")
        parser.print_help()
        sys.exit(1)

    if not _LINGUA_AVAILABLE:
        print("[!] 警告: lingua-language-detector 未安装，将仅基于假名判断日语。")
        print("    建议安装: pip install lingua-language-detector\n")

    db_path = args.db or get_db_path()
    if not os.path.exists(db_path):
        print(f"[-] 数据库文件不存在: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    total = get_total_count(conn)
    print(f"[*] 数据库: {db_path} (共 {total} 条记录)")

    print("[*] 正在扫描日语标题...")
    matches, columns = scan_japanese_records(conn)
    print(f"[*] 找到日语标题记录: {len(matches)} 条 ({len(matches)/total*100:.1f}%)")

    if not matches:
        print("[*] 没有找到任何日语标题的记录，退出。")
        conn.close()
        return

    # 非交互模式下，直接执行
    if args.mode == "export":
        output_path = args.output or os.path.join(
            os.path.dirname(db_path),
            f"{os.path.splitext(os.path.basename(db_path))[0]}_japanese_only_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        )
        if args.dry_run:
            print(f"\n[预览模式] 共 {len(matches)} 条记录【将会】导出到: {output_path}")
            for i, (_, row_dict) in enumerate(matches, 1):
                title = row_dict.get('title', '')[:60]
                print(f"  {i:4d}. {title}")
        else:
            conn.close()
            export_to_new_db(matches, columns, output_path)
    else:
        conn.close()
        conn = sqlite3.connect(db_path)
        delete_records(conn, matches, dry_run=args.dry_run)
        conn.close()


if __name__ == '__main__':
    main()