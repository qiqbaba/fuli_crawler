"""记录过滤、多维查重与番号分离工具合集 (fixes/record_filter.py)

本脚本集成了数据库重复记录的多维检测、CSV 审计导出、批量去重（支持 PDF 级联清理）以及严格日本番号识别、独立库导出与归档清理功能。

包含以下 2 大核心子模块与命令：

1. duplicates: 数据库多维查重、CSV 导出与批量去重
   - 多维查重维度:
     * 单字段维度: URL 地址 (`url`)
     * 单字段维度: 磁力/资源链接 (`resource_link`)
     * 复合联合维度: 标题 + 磁力链接 (`title + resource_link`)
   - 核心功能特性:
     * 终端摘要与详情分组展示：清晰列出每组重复的 ID、标题、链接及分布。
     * CSV 审计导出：将全部重复记录导出为带 UTF-8 BOM 编码的 CSV 文件（默认保存至 D:\\ 或 cache/ 目录）。
     * 灵活去重保留策略：支持按「保留最新一条 (ID 最大)」或「保留最旧一条 (ID 最小)」进行批量去重。
     * 级联物理文件清理：去重删除数据库记录的同时，可选择级联物理删除关联的多余 PDF 文件，并计算释放的磁盘容量。
     * 数据库自压缩：去重完成后自动执行 VACUUM 回收磁盘物理碎片。

2. fanhao: 严格日本番号识别、分布统计、独立库导出与批量删除
   - 核心功能特性:
     * 严格算法识别：利用 utils.fanhao_filter 模块中的正则引擎与过滤黑名单，严格精准识别标题中的日本成人影片番号（如 ABC-123、FC2-PPV-xxxx、SSIS-xxx 等），避免常规数字序号误判。
     * 前缀分布统计：统计并输出匹配记录的 Top 20 番号厂商前缀分布柱状统计。
     * 独立库导出 (--mode export): 将所有匹配到番号的记录无损迁移导出到一个全新的独立 SQLite 数据库中，并生成 fanhao 字段与 URL 唯一索引。
     * 批量清理与级联删文件 (--mode delete): 从当前数据库中批量删除番号记录，并自动级联删除对应的物理 PDF 文件，释放空间。
     * 预览与免确认模式：支持 --dry-run 查看预估影响范围，支持 --yes (-y) 在脚本自动化或批处理中免交互执行。

用法与命令示例:
  python fixes/record_filter.py                              # 进入交互式主菜单
  python fixes/record_filter.py duplicates                   # 进入查重与去重交互子菜单
  python fixes/record_filter.py duplicates --db /path/to.db  # 指定数据库查重
  python fixes/record_filter.py fanhao                       # 扫描并查看当前数据库番号分布
  python fixes/record_filter.py fanhao --mode export         # 将番号记录导出为独立 SQLite 库
  python fixes/record_filter.py fanhao --mode delete --dry-run # 预览待删除的番号记录与 PDF
  python fixes/record_filter.py fanhao --mode delete --yes   # 正式批量删除番号记录并清理对应 PDF
"""

import argparse
import csv
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

# ========== 路径引导与环境初始化 ==========
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fixes.db_utils import (  # noqa: E402
    setup_fixes_module,
    get_connection,
    get_columns,
    get_db_path,
    get_total_count,
    resolve_pdf_path,
    format_size,
    vacuum_db,
    backup_db,
)

setup_fixes_module()

from config import PDF_BASE_DIR  # noqa: E402
from utils.fanhao_filter import extract_fanhao  # noqa: E402

ColumnSpec = Union[str, Tuple[str, str]]
DUPLICATE_FIELDS = [
    ("url", "URL 地址"),
    ("resource_link", "磁力/资源链接"),
    (("title", "resource_link"), "标题+磁力链接"),
]
CSV_OUTPUT_DIR = os.environ.get("CSV_OUTPUT_DIR") or (
    "D:\\" if os.path.exists("D:\\") else os.path.join(PROJECT_ROOT, "cache")
)
os.makedirs(CSV_OUTPUT_DIR, exist_ok=True)


# ===================================================================
# 1. duplicates: 重复记录查找、导出与批量清理
# ===================================================================

def get_all_duplicates(conn: sqlite3.Connection, column: ColumnSpec, columns: List[str]) -> List[Dict]:
    """查找指定列（或列组合）上有重复值的所有记录，按重复值分组返回"""
    cursor = conn.cursor()

    if isinstance(column, tuple):
        col1, col2 = column
        r_col_list = ", ".join(f"r.{c}" for c in columns)
        cursor.execute(f"""
            SELECT {r_col_list}
            FROM resources r
            INNER JOIN (
                SELECT {col1}, {col2}
                FROM resources
                WHERE {col1} IS NOT NULL AND {col1} != ''
                  AND {col2} IS NOT NULL AND {col2} != ''
                GROUP BY {col1}, {col2}
                HAVING COUNT(*) > 1
            ) d
                ON r.{col1} = d.{col1}
               AND r.{col2} = d.{col2}
            ORDER BY r.{col1}, r.{col2}, r.id
        """)
        rows = cursor.fetchall()

        result = []
        for row in rows:
            record = dict(zip(columns, row))
            record["_dup_column"] = f"{col1}+{col2}"
            record["_group_key"] = f"{record.get(col1, '')}|||{record.get(col2, '')}"
            result.append(record)
        return result

    else:
        cursor.execute(f"""
            SELECT {column}
            FROM resources
            WHERE {column} IS NOT NULL AND {column} != ''
            GROUP BY {column}
            HAVING COUNT(*) > 1
        """)
        dup_values = [row[0] for row in cursor.fetchall()]
        if not dup_values:
            return []

        placeholders = ",".join("?" for _ in dup_values)
        col_list = ", ".join(columns)
        cursor.execute(f"""
            SELECT {col_list}
            FROM resources
            WHERE {column} IN ({placeholders})
            ORDER BY {column}, id
        """, dup_values)
        rows = cursor.fetchall()

        result = []
        for row in rows:
            record = dict(zip(columns, row))
            record["_dup_column"] = column
            record["_group_key"] = record.get(column)
            result.append(record)
        return result


def export_duplicates_to_csv(records: List[Dict], filepath: str) -> str:
    """将重复记录导出到 CSV 文件"""
    if not records:
        return ""
    fieldnames = [k for k in records[0].keys() if not k.startswith("_")]
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            clean = {k: v for k, v in rec.items() if not k.startswith("_")}
            writer.writerow(clean)
    return filepath


def print_dup_summary(records: List[Dict], column: ColumnSpec, label: str):
    """打印重复记录摘要"""
    if not records:
        print(f"  [✓] 未发现 {label} 重复记录。")
        return

    groups: Dict[str, List[Dict]] = {}
    for rec in records:
        groups.setdefault(rec["_group_key"], []).append(rec)

    is_composite = isinstance(column, tuple)
    print(f"\n  [发现] {label} 重复，共 {len(records)} 条记录，{len(groups)} 组重复：")
    print(f"  {'=' * 70}")
    for idx, (key, group) in enumerate(groups.items(), 1):
        if is_composite:
            parts = key.split("|||", 1)
            t_part = parts[0][:60] if len(parts) > 0 else ""
            l_part = (parts[1][:60] + "...") if len(parts) > 1 and len(parts[1]) > 60 else (parts[1] if len(parts) > 1 else "")
            print(f"  [{idx}] 标题: {t_part}")
            print(f"      链接: {l_part}")
        else:
            print(f"  [{idx}] 重复值: {key[:80]}{'...' if len(key) > 80 else ''}")
        print(f"      重复次数: {len(group)} 条")
        for rec in group:
            title = (rec.get("title") or "")[:50]
            rid = rec.get("id", "?")
            print(f"      - ID={rid}, title={title}")
        print()


def print_dup_detail(records: List[Dict], column: ColumnSpec):
    """打印重复记录详细信息"""
    if not records:
        return
    groups: Dict[str, List[Dict]] = {}
    for rec in records:
        groups.setdefault(rec["_group_key"], []).append(rec)

    all_cols = [k for k in records[0].keys() if not k.startswith("_")]
    is_composite = isinstance(column, tuple)

    for idx, (key, group) in enumerate(groups.items(), 1):
        print(f"\n{'=' * 80}")
        if is_composite:
            parts = key.split("|||", 1)
            print(f"  重复组 [{idx}] - 标题: {parts[0] if len(parts) > 0 else ''}")
            if len(parts) > 1:
                print(f"                   链接: {parts[1]}")
        else:
            print(f"  重复组 [{idx}] - 重复值: {key}")
        print(f"{'=' * 80}")
        for rec in group:
            print(f"  --- 记录 ID={rec.get('id', '?')} ---")
            for col in all_cols:
                val = rec.get(col, "")
                if val is None:
                    val = ""
                if len(str(val)) > 120:
                    val = str(val)[:120] + "..."
                print(f"    {col}: {val}")
            print()


def collect_pdf_stats(records: List[Dict], keep_oldest: bool = False) -> Tuple[int, int]:
    """统计删除重复记录时会移除的 PDF 文件数量与字节数"""
    groups: Dict[str, List[Dict]] = {}
    for rec in records:
        groups.setdefault(rec["_group_key"], []).append(rec)

    pdf_files: List[str] = []
    seen: set = set()
    for group in groups.values():
        group_sorted = sorted(group, key=lambda r: r.get("id", 0) or 0)
        with_pdf = [r for r in group_sorted if (r.get("pdf_path") or "").strip()]
        candidates = with_pdf if with_pdf else group_sorted
        kept_id = candidates[0].get("id") if keep_oldest else candidates[-1].get("id")
        for rec in group_sorted:
            if (rec.get("id", 0) or 0) == kept_id:
                continue
            p = rec.get("pdf_path") or ""
            if not p:
                continue
            abs_p = resolve_pdf_path(p, PROJECT_ROOT)
            key_p = abs_p.lower().replace("\\", "/")
            if key_p in seen:
                continue
            seen.add(key_p)
            if os.path.exists(abs_p):
                pdf_files.append(abs_p)

    total_bytes = sum(os.path.getsize(f) for f in pdf_files)
    return len(pdf_files), total_bytes


def delete_duplicates_batch(
    conn: sqlite3.Connection,
    records: List[Dict],
    column: ColumnSpec,
    label: str,
    keep_newest: bool = True,
    delete_pdf: bool = False,
) -> Tuple[int, int, int, int]:
    """批量删除重复记录并可选清理对应 PDF"""
    if not records:
        return 0, 0, 0, 0

    groups: Dict[str, List[Dict]] = {}
    for rec in records:
        groups.setdefault(rec["_group_key"], []).append(rec)

    total_deleted = 0
    total_pdf_deleted = 0
    total_pdf_failed = 0
    total_pdf_bytes = 0
    cursor = conn.cursor()

    n_groups = len(groups)
    progress_interval = max(5, (n_groups + 99) // 100)

    for gidx, (key, group) in enumerate(groups.items(), 1):
        if gidx % progress_interval == 0 or gidx == n_groups:
            print(f"  处理中... {gidx}/{n_groups} 组 ({gidx * 100 // n_groups}%)")

        group_sorted = sorted(group, key=lambda r: r.get("id", 0) or 0)
        with_pdf = [r for r in group_sorted if (r.get("pdf_path") or "").strip()]
        candidates = with_pdf if with_pdf else group_sorted
        ids_sorted = sorted(r.get("id", 0) or 0 for r in candidates)
        keep_id = ids_sorted[-1] if keep_newest else ids_sorted[0]

        keep_id_int = int(keep_id)
        delete_ids = [str(r.get("id")) for r in group_sorted if (r.get("id", 0) or 0) != keep_id_int]
        if not delete_ids:
            continue

        pdf_files_to_delete: List[str] = []
        seen: set = set()
        for rec in group_sorted:
            if (rec.get("id", 0) or 0) == keep_id_int:
                continue
            p = rec.get("pdf_path") or ""
            if not p:
                continue
            abs_p = resolve_pdf_path(p, PROJECT_ROOT)
            key_p = abs_p.lower().replace("\\", "/")
            if key_p not in seen and os.path.exists(abs_p):
                seen.add(key_p)
                pdf_files_to_delete.append(abs_p)

        deleted_pdfs = 0
        failed_pdfs = 0
        deleted_pdf_bytes = 0
        if delete_pdf:
            for pdf_path in pdf_files_to_delete:
                try:
                    size = os.path.getsize(pdf_path)
                    os.remove(pdf_path)
                    deleted_pdfs += 1
                    deleted_pdf_bytes += size
                except Exception as e:
                    failed_pdfs += 1
                    print(f"    [删除PDF失败] {pdf_path}: {e}")

        placeholders = ",".join("?" for _ in delete_ids)
        cursor.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", delete_ids)
        conn.commit()
        deleted = cursor.rowcount
        total_deleted += deleted
        total_pdf_deleted += deleted_pdfs
        total_pdf_failed += failed_pdfs
        total_pdf_bytes += deleted_pdf_bytes

    return total_deleted, total_pdf_deleted, total_pdf_failed, total_pdf_bytes


def run_duplicates_menu(args=None):
    """查重与清理主交互菜单"""
    db_path = get_db_path(getattr(args, "db", None) if args else None)
    if not os.path.exists(db_path):
        print(f"[!] 数据库文件不存在: {db_path}")
        return

    print(f"\n{'=' * 60}")
    print("        数据库重复记录查找与清理工具")
    print(f"{'=' * 60}")
    print(f"  数据库: {db_path}\n")

    conn = get_connection(db_path)
    columns = get_columns(conn.cursor())
    all_dup_data: Dict[ColumnSpec, List[Dict]] = {}

    for col_key, col_label in DUPLICATE_FIELDS:
        all_dup_data[col_key] = get_all_duplicates(conn, col_key, columns)

    while True:
        print(f"\n{'─' * 60}")
        print("  查重与清理子菜单")
        print(f"{'─' * 60}")
        print("  当前重复状态:")
        for col_key, col_label in DUPLICATE_FIELDS:
            cnt = len(all_dup_data[col_key])
            status = f"{cnt} 条重复" if cnt else "无重复"
            print(f"    - {col_label}: {status}")

        print("\n    1 - 检查/刷新指定类型的重复数据")
        print("    2 - 查看重复详情")
        print("    3 - 导出全部重复到 CSV")
        print("    4 - 批量删除重复记录（支持级联删除 PDF）")
        print("    5 - 重新扫描数据库")
        print("    0 - 返回上一级 / 退出")

        choice = input("\n  请选择 [0-5]: ").strip()

        if choice == "0":
            break

        elif choice == "1":
            print("\n  选择要检查的重复类型：")
            for i, (col_key, col_label) in enumerate(DUPLICATE_FIELDS, 1):
                cnt = len(all_dup_data[col_key])
                print(f"    {i} - {col_label} (当前 {cnt} 条)")
            print("    a - 全部类型")
            sub = input(f"  请选择 [1-{len(DUPLICATE_FIELDS)}/a]: ").strip().lower()

            targets = []
            if sub == "a":
                targets = DUPLICATE_FIELDS
            else:
                try:
                    idx = int(sub) - 1
                    if 0 <= idx < len(DUPLICATE_FIELDS):
                        targets = [DUPLICATE_FIELDS[idx]]
                    else:
                        print("  无效选择。")
                        continue
                except ValueError:
                    print("  无效输入。")
                    continue

            for col_key, col_label in targets:
                print(f"\n  >>> 正在检查 {col_label} 重复...")
                all_dup_data[col_key] = get_all_duplicates(conn, col_key, columns)
                print_dup_summary(all_dup_data[col_key], col_key, col_label)

        elif choice == "2":
            print("\n  选择要查看的重复类型：")
            for i, (col_key, col_label) in enumerate(DUPLICATE_FIELDS, 1):
                cnt = len(all_dup_data[col_key])
                print(f"    {i} - {col_label} ({cnt} 条)")
            sub = input(f"  请选择 [1-{len(DUPLICATE_FIELDS)}]: ").strip()
            try:
                idx = int(sub) - 1
                if 0 <= idx < len(DUPLICATE_FIELDS):
                    col_key, col_label = DUPLICATE_FIELDS[idx]
                    print_dup_detail(all_dup_data[col_key], col_key)
                else:
                    print("  无效选择。")
            except ValueError:
                print("  无效输入。")

        elif choice == "3":
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            exported = []
            for col_key, col_label in DUPLICATE_FIELDS:
                if not all_dup_data[col_key]:
                    continue
                file_key = "_".join(col_key) if isinstance(col_key, tuple) else col_key
                filename = f"duplicates_{file_key}_{timestamp}.csv"
                filepath = os.path.join(CSV_OUTPUT_DIR, filename)
                actual = export_duplicates_to_csv(all_dup_data[col_key], filepath)
                if actual:
                    exported.append((col_label, actual, len(all_dup_data[col_key])))

            if exported:
                print("\n  导出完成：")
                for label, path, cnt in exported:
                    print(f"    [✓] {label}: {cnt} 条 -> {path}")
            else:
                print("\n  没有重复记录可导出。")

        elif choice == "4":
            print("\n  选择要删除的重复类型：")
            for i, (col_key, col_label) in enumerate(DUPLICATE_FIELDS, 1):
                cnt = len(all_dup_data[col_key])
                print(f"    {i} - {col_label} ({cnt} 条)")
            print("    a - 全部类型")
            sub = input(f"  请选择 [1-{len(DUPLICATE_FIELDS)}/a]: ").strip().lower()

            targets = []
            if sub == "a":
                targets = DUPLICATE_FIELDS
            else:
                try:
                    idx = int(sub) - 1
                    if 0 <= idx < len(DUPLICATE_FIELDS):
                        targets = [DUPLICATE_FIELDS[idx]]
                    else:
                        print("  无效选择。")
                        continue
                except ValueError:
                    print("  无效输入。")
                    continue

            grand_records = 0
            grand_pdfs_k = 0
            grand_bytes_k = 0
            grand_pdfs_o = 0
            grand_bytes_o = 0
            overview_lines = []
            for col_key, col_label in targets:
                recs = all_dup_data[col_key]
                if not recs:
                    continue
                n_groups = len({r["_group_key"] for r in recs})
                pdfs_k, bytes_k = collect_pdf_stats(recs, keep_oldest=False)
                pdfs_o, bytes_o = collect_pdf_stats(recs, keep_oldest=True)
                overview_lines.append(
                    f"  - {col_label}: {n_groups} 组重复, {len(recs)} 条记录, "
                    f"预计删除 {len(recs) - n_groups} 条\n"
                    f"      保留最新: 删 PDF {pdfs_k} 个, 释放 {format_size(bytes_k)}\n"
                    f"      保留最旧: 删 PDF {pdfs_o} 个, 释放 {format_size(bytes_o)}"
                )
                grand_records += len(recs) - n_groups
                grand_pdfs_k += pdfs_k
                grand_bytes_k += bytes_k
                grand_pdfs_o += pdfs_o
                grand_bytes_o += bytes_o

            if not grand_records:
                print("\n  所选类型没有可删除的重复记录。")
                continue

            print(f"\n{'=' * 60}")
            print("  [概况确认] 删除前预览")
            print(f"{'=' * 60}")
            for line in overview_lines:
                print(line)
            print(f"  {'-' * 56}")
            print(f"  合计: 预计删除重复记录 {grand_records} 条")
            print(f"    保留最新策略: 同时删除 PDF {grand_pdfs_k} 个, 释放 {format_size(grand_bytes_k)}")
            print(f"    保留最旧策略: 同时删除 PDF {grand_pdfs_o} 个, 释放 {format_size(grand_bytes_o)}")
            print(f"{'=' * 60}")
            confirm = input("  确认开始删除？[y/N]: ").strip().lower()
            if confirm != "y":
                print("  已取消删除。")
                continue

            print("\n  请选择保留策略：")
            print("    k - 保留最新一条 (ID 最大)，删除其余")
            print("    o - 保留最旧一条 (ID 最小)，删除其余")
            keep_input = input("  请选择 [k/o] (默认 k): ").strip().lower()
            keep_newest = keep_input != "o"

            pdf_input = input("  是否同时删除关联的 PDF 文件？[y/N] (默认 N): ").strip().lower()
            delete_pdf = pdf_input == "y"

            total_deleted = 0
            total_pdf_deleted = 0
            total_pdf_failed = 0
            total_pdf_bytes = 0

            for col_key, col_label in targets:
                if not all_dup_data[col_key]:
                    continue
                del_recs, del_pdfs, fail_pdfs, del_bytes = delete_duplicates_batch(
                    conn, all_dup_data[col_key], col_key, col_label,
                    keep_newest=keep_newest,
                    delete_pdf=delete_pdf,
                )
                msg_type = f"  [✓] {col_label}: 删除 {del_recs} 条记录"
                if del_pdfs:
                    msg_type += f", PDF {del_pdfs} 个, 共 {format_size(del_bytes)}"
                    if fail_pdfs:
                        msg_type += f"（失败 {fail_pdfs} 个）"
                print(msg_type)
                total_deleted += del_recs
                total_pdf_deleted += del_pdfs
                total_pdf_failed += fail_pdfs
                total_pdf_bytes += del_bytes
                all_dup_data[col_key] = get_all_duplicates(conn, col_key, columns)

            if total_deleted:
                vacuum_db(conn)
                print(f"\n[+] 成功删除 {total_deleted} 条重复记录！")
                if total_pdf_deleted:
                    print(f"    共删除 PDF {total_pdf_deleted} 个，释放磁盘空间 {format_size(total_pdf_bytes)}")
            else:
                print("\n  未删除任何记录。")

        elif choice == "5":
            print("\n  >>> 正在重新扫描数据库...")
            for col_key, col_label in DUPLICATE_FIELDS:
                all_dup_data[col_key] = get_all_duplicates(conn, col_key, columns)
                print_dup_summary(all_dup_data[col_key], col_key, col_label)

    conn.close()


# ===================================================================
# 2. fanhao: 严格日本番号过滤、导出与清理
# ===================================================================

def scan_fanhao_records(conn: sqlite3.Connection):
    """扫描数据库，返回包含番号的记录列表"""
    cursor = conn.cursor()
    columns = get_columns(cursor)
    cursor.execute(f"SELECT {', '.join(columns)} FROM resources ORDER BY id")
    rows = cursor.fetchall()

    matches = []
    for row in rows:
        row_dict = dict(zip(columns, row))
        title = row_dict.get("title", "")
        found, fanhao = extract_fanhao(title or "")
        if found:
            matches.append((fanhao, row_dict))

    return matches, columns


def export_fanhao_to_new_db(matches, columns, output_path: str) -> None:
    """将匹配的番号记录导出到新的 SQLite 数据库"""
    print(f"\n[*] 正在导出 {len(matches)} 条记录到: {output_path}")

    if os.path.exists(output_path):
        backup_db(output_path, prefix_tag="fanhao_export")

    conn = sqlite3.connect(output_path)
    cursor = conn.cursor()

    col_defs = ", ".join(f"{col} TEXT" if col != "id" else "id INTEGER PRIMARY KEY" for col in columns)
    cursor.execute(f"CREATE TABLE IF NOT EXISTS resources ({col_defs})")

    try:
        cursor.execute("ALTER TABLE resources ADD COLUMN fanhao TEXT")
    except sqlite3.OperationalError:
        pass

    col_list = ", ".join(columns + ["fanhao"])
    placeholders = ", ".join(["?" for _ in range(len(columns) + 1)])
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_url ON resources(url)")

    for fanhao, row_dict in matches:
        values = [row_dict.get(col) for col in columns]
        values.append(fanhao)
        cursor.execute(f"INSERT OR IGNORE INTO resources ({col_list}) VALUES ({placeholders})", values)

    conn.commit()
    print(f"[+] 导出完成！新数据库共 {len(matches)} 条记录")
    conn.close()


def delete_fanhao_records(conn: sqlite3.Connection, matches, dry_run: bool = False, force: bool = False) -> None:
    """删除匹配的番号记录并级联删除 PDF"""
    ids_to_delete = [row_dict["id"] for _, row_dict in matches]
    pdf_files_to_delete = []

    for _, row_dict in matches:
        pdf_path = row_dict.get("pdf_path", "")
        if pdf_path:
            abs_p = resolve_pdf_path(pdf_path, PROJECT_ROOT)
            if os.path.exists(abs_p):
                pdf_files_to_delete.append(abs_p)

    print(f"\n{'=' * 60}")
    print(f"[操作] 准备删除 {len(ids_to_delete)} 条番号记录")
    if pdf_files_to_delete:
        print(f"[操作] 同时删除 {len(pdf_files_to_delete)} 个关联 PDF 文件")

    if dry_run:
        print("\n[预览模式] 以下记录将被删除 (样例):")
        for i, (fanhao, row_dict) in enumerate(matches[:20], 1):
            print(f"  {i:4d}. [{fanhao}] {(row_dict.get('title') or '')[:60]}")
        if len(matches) > 20:
            print(f"  ... 以及其他 {len(matches) - 20} 条记录")
        print(f"\n[预览] 共 {len(ids_to_delete)} 条记录, {len(pdf_files_to_delete)} 个 PDF 文件将被删除")
        return

    if not force:
        confirm = input(f"\n[?] 确定要删除这 {len(ids_to_delete)} 条记录及关联 PDF？(yes/NO): ").strip().lower()
        if confirm != "yes":
            print("[-] 已取消操作。")
            return

    deleted_pdfs = 0
    failed_pdfs = 0
    for pdf_p in pdf_files_to_delete:
        try:
            os.remove(pdf_p)
            deleted_pdfs += 1
        except Exception as e:
            failed_pdfs += 1
            print(f"  [-删除PDF失败] {pdf_p}: {e}")

    cursor = conn.cursor()
    batch_size = 500
    deleted_count = 0
    for i in range(0, len(ids_to_delete), batch_size):
        batch = ids_to_delete[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        cursor.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", batch)
        deleted_count += cursor.rowcount

    conn.commit()
    vacuum_db(conn)

    print("\n[+] 操作完成！")
    print(f"  删除数据库记录: {deleted_count} 条")
    print(f"  删除 PDF 文件: {deleted_pdfs} 个{' (失败: ' + str(failed_pdfs) + ')' if failed_pdfs else ''}")
    print(f"  数据库中剩余记录: {get_total_count(conn)} 条")


def run_fanhao_cli(args):
    """番号过滤 CLI 处理入口"""
    db_path = get_db_path(getattr(args, "db", None))
    if not os.path.exists(db_path):
        print(f"[-] 数据库文件不存在: {db_path}")
        return

    conn = get_connection(db_path)
    total = get_total_count(conn)
    print(f"[*] 数据库: {db_path} (共 {total} 条记录)")
    print("[*] 正在扫描标题中的番号 (严格判定)...")

    matches, columns = scan_fanhao_records(conn)
    print(f"[*] 找到含番号记录: {len(matches)} 条 ({len(matches)/max(total, 1)*100:.1f}%)")

    if not matches:
        print("[*] 没有找到任何含番号记录。")
        conn.close()
        return

    mode = getattr(args, "mode", None)
    if not mode:
        # 打印番号分布
        prefix_stats = {}
        for fanhao, _ in matches:
            prefix = re.sub(r"[\d\-_ ].*$", "", fanhao).upper() or fanhao.upper()
            prefix_stats[prefix] = prefix_stats.get(prefix, 0) + 1
        print("\n[*] 番号前缀分布 (Top 20):")
        for prefix, count in sorted(prefix_stats.items(), key=lambda x: -x[1])[:20]:
            print(f"    {prefix:<12s} : {count:5d} 条")

        print("\n请指定 --mode export (导出) 或 --mode delete (删除)。")
        conn.close()
        return

    if mode == "export":
        output_path = getattr(args, "output", None) or os.path.join(
            os.path.dirname(db_path),
            f"{os.path.splitext(os.path.basename(db_path))[0]}_fanhao_only_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        )
        if getattr(args, "dry_run", False):
            print(f"\n[预览模式] 共 {len(matches)} 条记录将会导出到: {output_path}")
        else:
            conn.close()
            export_fanhao_to_new_db(matches, columns, output_path)
    else:
        delete_fanhao_records(conn, matches, dry_run=getattr(args, "dry_run", False), force=getattr(args, "yes", False))
        conn.close()


# ===================================================================
# 3. 主入口与全局交互菜单
# ===================================================================

def interactive_menu():
    """记录过滤与去重全局主菜单"""
    print(f"\n{'=' * 60}")
    print("           记录过滤、去重与导出工具合集")
    print(f"{'=' * 60}")
    print("  请选择要进入的功能模块：")
    print()
    print("    1. duplicates - 数据库查重、导出 CSV、批量去重（支持级联删除 PDF）")
    print("    2. fanhao     - 严格番号识别、分布统计、导出独立库或批量清理")
    print()
    print("    0. 退出")
    print("=" * 60)

    try:
        choice = input("请输入序号 [0-2] (直接回车退出): ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n[*] 操作已取消。")
        return

    if choice == "1":
        run_duplicates_menu()
    elif choice == "2":
        db_path = get_db_path()
        conn = get_connection(db_path)
        total = get_total_count(conn)
        print(f"\n[*] 正在扫描数据库番号: {db_path} (共 {total} 条)...")
        matches, columns = scan_fanhao_records(conn)
        print(f"[*] 找到含番号记录: {len(matches)} 条")
        if matches:
            prefix_stats = {}
            for fanhao, _ in matches:
                prefix = re.sub(r"[\d\-_ ].*$", "", fanhao).upper() or fanhao.upper()
                prefix_stats[prefix] = prefix_stats.get(prefix, 0) + 1
            print("\n[*] 番号前缀分布 (Top 20):")
            for prefix, count in sorted(prefix_stats.items(), key=lambda x: -x[1])[:20]:
                print(f"    {prefix:<12s} : {count:5d} 条")

            print("\n  操作选项:")
            print("    1. 导出匹配记录到新 SQLite 数据库")
            print("    2. 批量删除匹配记录 (含 PDF)")
            sub_c = input("  请选择 [1/2] (回车取消): ").strip()
            if sub_c == "1":
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                out = os.path.join(os.path.dirname(db_path), f"fanhao_only_{ts}.db")
                export_fanhao_to_new_db(matches, columns, out)
            elif sub_c == "2":
                delete_fanhao_records(conn, matches, dry_run=False, force=False)
        conn.close()
    else:
        print("[*] 已退出。")


def main():
    parser = argparse.ArgumentParser(
        description="记录过滤与去重清理工具合集 (查重与清理 / 番号识别与导出清理)"
    )
    subparsers = parser.add_subparsers(dest="command", help="可用的子命令")

    # duplicates
    p_dup = subparsers.add_parser("duplicates", help="数据库查重、导出 CSV、批量去重并级联清理 PDF")
    p_dup.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_dup.set_defaults(func=run_duplicates_menu)

    # fanhao
    p_fan = subparsers.add_parser("fanhao", help="严格日本番号识别、分布统计、导出独立库或批量清理")
    p_fan.add_argument("--mode", choices=["export", "delete"], help="操作模式: export(导出) 或 delete(删除)")
    p_fan.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_fan.add_argument("--output", type=str, default=None, help="导出模式下的输出数据库路径")
    p_fan.add_argument("--dry-run", action="store_true", default=False, help="预览模式，不实际修改数据")
    p_fan.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_fan.set_defaults(func=run_fanhao_cli)

    args = parser.parse_args()

    if args.command is None:
        interactive_menu()
    else:
        args.func(args)


if __name__ == "__main__":
    main()
