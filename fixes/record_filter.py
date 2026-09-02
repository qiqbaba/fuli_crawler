"""记录过滤、多维查重与番号分离工具合集 (fixes/record_filter.py)

本脚本集成了数据库重复记录的多维检测、默认独立 SQLite DB 导出（支持 CSV）、批量去重（强制级联清理关联 PDF）以及严格日本番号识别、独立库导出与归档清理功能。

包含以下 2 大核心子模块与命令：

1. duplicates: 数据库多维查重、默认 DB 导出与批量去重
   - 多维查重维度:
     * 单字段维度: URL 地址 (`url`)
     * 单字段维度: 磁力/资源链接 (`resource_link`)
     * 复合联合维度: 标题 + 磁力链接 (`title + resource_link`)
   - 核心功能特性:
     * 终端摘要与详情分组展示：清晰列出每组重复的 ID、标题、链接及分布。
     * 独立 DB 审计导出（默认）与 CSV 导出：将全部重复记录导出到独立 SQLite .db 库（优先 D:\\ 盘，否则 temp_profiles/）。
     * 灵活去重保留策略：支持按「保留最新一条 (ID 最大)」或「保留最旧一条 (ID 最小)」进行批量去重。
     * 强制级联物理 PDF 清理：去重删除数据库记录的同时，强制级联物理删除关联的多余 PDF 文件并统计释放空间。
     * 数据库自压缩：去重完成后自动执行 VACUUM 回收磁盘物理碎片。

2. fanhao: 严格日本番号识别、分布统计、独立库导出与批量清理
   - 核心功能特性:
     * 严格算法识别：利用 utils.fanhao_filter 模块中的正则引擎与过滤黑名单，严格精准识别标题中的日本番号。
     * 前缀分布统计：统计并输出匹配记录的 Top 20 番号厂商前缀分布。
     * 独立库导出 (--mode export): 将匹配到番号的记录无损导出为一个全新的独立 SQLite 数据库文件。
     * 批量清理与级联删文件 (--mode delete): 批量删除番号记录，并强制级联删除对应的物理 PDF 文件。
     * 预览与免确认模式：默认 Dry-Run 预览，使用 --run 正式执行，支持 -y / --yes 免交互执行。

用法与命令示例:
  python fixes/record_filter.py                                       # 进入交互式主菜单 (常驻循环)
  python fixes/record_filter.py duplicates                            # 进入查重与去重交互子菜单
  python fixes/record_filter.py duplicates --field all --export-db    # 扫描全维度重复并导出为独立 DB 库
  python fixes/record_filter.py duplicates --field url --run --keep newest # 批量去重并级联删除 PDF
  python fixes/record_filter.py fanhao                                # 扫描并查看当前数据库番号分布 (预览)
  python fixes/record_filter.py fanhao --mode export --run            # 将番号记录导出为独立 SQLite 库 (默认 .db)
  python fixes/record_filter.py fanhao --mode delete --run --yes      # 正式批量删除番号记录并清理对应 PDF
"""

import argparse
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple, Union

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
    get_timestamp,
    export_records_to_db,
    export_to_csv,
    delete_records_cascade_pdf,
    print_banner,
    print_section,
    print_step,
    print_success,
    print_warning,
    print_error,
    confirm_action,
    pause_for_user,
)

setup_fixes_module()

from utils.fanhao_filter import extract_fanhao  # noqa: E402

ColumnSpec = Union[str, Tuple[str, str]]
DUPLICATE_FIELDS: List[Tuple[ColumnSpec, str, str]] = [
    ("url", "URL 地址", "url"),
    ("resource_link", "磁力/资源链接", "resource_link"),
    (("title", "resource_link"), "标题+磁力链接", "title_link"),
]


# ===================================================================
# 1. duplicates: 重复记录查找、导出与批量清理 (强制级联删除 PDF)
# ===================================================================

def get_all_duplicates(conn: sqlite3.Connection, column: ColumnSpec, columns: List[str]) -> List[Dict[str, Any]]:
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
            record["_group_key"] = str(record.get(column, ""))
            result.append(record)
        return result


def print_dup_summary(records: List[Dict[str, Any]], column: ColumnSpec, label: str) -> None:
    """打印重复记录摘要"""
    if not records:
        print_success(f"未发现 {label} 重复记录。")
        return

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        groups.setdefault(rec["_group_key"], []).append(rec)

    is_composite = isinstance(column, tuple)
    print_warning(f"发现 {label} 重复，共 {len(records)} 条记录，{len(groups)} 组重复：")
    print("  " + "─" * 58)
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


def print_dup_detail(records: List[Dict[str, Any]], column: ColumnSpec) -> None:
    """打印重复记录详细信息"""
    if not records:
        print_step("暂无重复记录明细。")
        return
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        groups.setdefault(rec["_group_key"], []).append(rec)

    all_cols = [k for k in records[0].keys() if not k.startswith("_")]
    is_composite = isinstance(column, tuple)

    for idx, (key, group) in enumerate(groups.items(), 1):
        print("\n" + "=" * 60)
        if is_composite:
            parts = key.split("|||", 1)
            print(f"  重复组 [{idx}] - 标题: {parts[0] if len(parts) > 0 else ''}")
            if len(parts) > 1:
                print(f"                   链接: {parts[1]}")
        else:
            print(f"  重复组 [{idx}] - 重复值: {key}")
        print("=" * 60)
        for rec in group:
            print(f"  --- 记录 ID={rec.get('id', '?')} ---")
            for col in all_cols:
                val = rec.get(col, "")
                if val is None:
                    val = ""
                if len(str(val)) > 100:
                    val = str(val)[:100] + "..."
                print(f"    {col}: {val}")
            print()


def plan_duplicate_deletions(
    records: List[Dict[str, Any]],
    keep_newest: bool = True,
    only_no_pdf: bool = False,
) -> Tuple[List[int], List[str]]:
    """根据保留策略与模式，计算需要删除的记录 ID 列表以及关联需要删除的物理 PDF 路径列表

    Args:
        records: 重复记录列表
        keep_newest: True 保留最新 (ID 最大)，False 保留最旧 (ID 最小)
        only_no_pdf: 若为 True，仅删除没有关联 PDF 的重复记录，绝对不删除任何已包含 PDF 的记录与文件

    Returns:
        (ids_to_delete, pdf_paths_to_delete)
    """
    if not records:
        return [], []

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        groups.setdefault(rec["_group_key"], []).append(rec)

    ids_to_delete: List[int] = []
    pdf_paths: List[str] = []
    seen_pdfs: set = set()

    for group in groups.values():
        group_sorted = sorted(group, key=lambda r: int(r.get("id", 0) or 0))
        with_pdf = [r for r in group_sorted if (r.get("pdf_path") or "").strip()]
        without_pdf = [r for r in group_sorted if not (r.get("pdf_path") or "").strip()]

        if only_no_pdf:
            # 仅删除无 PDF 的重复链接模式
            if with_pdf:
                # 组内存在有 PDF 的记录：所有无 PDF 的多余链接副本加入待删除列表，含 PDF 记录全量保留
                for rec in without_pdf:
                    ids_to_delete.append(int(rec.get("id", 0) or 0))
            else:
                # 组内所有记录均无 PDF：按照保留策略保留 1 条（最新或最旧），其余无 PDF 副本加入待删除列表
                ids_sorted = sorted(int(r.get("id", 0) or 0) for r in without_pdf)
                keep_id = ids_sorted[-1] if keep_newest else ids_sorted[0]
                for rec in without_pdf:
                    rid = int(rec.get("id", 0) or 0)
                    if rid != keep_id:
                        ids_to_delete.append(rid)
            # 仅删无 PDF 模式下，绝不删除任何 PDF 文件
        else:
            # 默认完整去重模式：每组只保留唯一一条记录（优先在含 PDF 的候选集中按最新/最旧保留），其余所有副本级联删除
            candidates = with_pdf if with_pdf else group_sorted
            ids_sorted = sorted(int(r.get("id", 0) or 0) for r in candidates)
            keep_id = ids_sorted[-1] if keep_newest else ids_sorted[0]

            for rec in group_sorted:
                rid = int(rec.get("id", 0) or 0)
                if rid != keep_id:
                    ids_to_delete.append(rid)
                    p = (rec.get("pdf_path") or "").strip()
                    if p:
                        abs_p = resolve_pdf_path(p, PROJECT_ROOT)
                        key_p = abs_p.lower().replace("\\", "/")
                        if key_p not in seen_pdfs and os.path.exists(abs_p):
                            seen_pdfs.add(key_p)
                            pdf_paths.append(abs_p)

    return ids_to_delete, pdf_paths


def collect_pdf_stats(records: List[Dict[str, Any]], keep_oldest: bool = False, only_no_pdf: bool = False) -> Tuple[int, int]:
    """统计删除重复记录时会被级联清理的 PDF 文件数量与字节数"""
    if only_no_pdf or not records:
        return 0, 0
    _, pdf_files = plan_duplicate_deletions(records, keep_newest=not keep_oldest, only_no_pdf=False)
    total_bytes = sum(os.path.getsize(f) for f in pdf_files if os.path.exists(f))
    return len(pdf_files), total_bytes


def delete_duplicates_batch(
    conn: sqlite3.Connection,
    records: List[Dict[str, Any]],
    keep_newest: bool = True,
    only_no_pdf: bool = False,
) -> Tuple[int, int, int, int]:
    """批量删除重复记录并【强制级联删除对应物理 PDF】（若开启 only_no_pdf 则仅删除未关联 PDF 的重复记录）

    Returns:
        (deleted_records, deleted_pdfs, failed_pdfs, deleted_pdf_bytes)
    """
    if not records:
        return 0, 0, 0, 0

    ids_to_delete, _ = plan_duplicate_deletions(records, keep_newest=keep_newest, only_no_pdf=only_no_pdf)
    if not ids_to_delete:
        return 0, 0, 0, 0

    # 级联物理删除 PDF 与数据库记录（在 only_no_pdf 模式下待删记录无 PDF，仅清理 DB 记录）
    return delete_records_cascade_pdf(conn, ids_to_delete, project_root=PROJECT_ROOT)


def export_duplicates(records: List[Dict[str, Any]], tag: str, as_csv: bool = False) -> str:
    """导出重复记录（默认导出为独立 .db 文件，可选 CSV）"""
    if not records:
        return ""
    ts = get_timestamp()
    if as_csv:
        filename = f"record_duplicates_{tag}_{ts}.csv"
        return export_to_csv(records, filename)
    else:
        filename = f"record_duplicates_{tag}_{ts}.db"
        return export_records_to_db(records, filename, table_name="duplicate_resources")


def run_duplicates_menu(args=None) -> None:
    """查重与去重主交互子菜单 (循环常驻)"""
    db_path = get_db_path(getattr(args, "db", None) if args else None)
    if not os.path.exists(db_path):
        print_error(f"数据库文件不存在: {db_path}")
        pause_for_user()
        return

    conn = get_connection(db_path)
    columns = get_columns(conn.cursor())
    all_dup_data: Dict[str, List[Dict[str, Any]]] = {}

    print_step("正在初次扫描数据库重复数据...")
    for col_key, col_label, key_tag in DUPLICATE_FIELDS:
        all_dup_data[key_tag] = get_all_duplicates(conn, col_key, columns)

    while True:
        print_banner("数据库重复记录查找、导出与去重工具")
        print(f"  当前数据库: {db_path}")
        print("  当前重复状态:")
        for col_key, col_label, key_tag in DUPLICATE_FIELDS:
            cnt = len(all_dup_data[key_tag])
            status = f"{cnt} 条重复" if cnt else "无重复"
            print(f"    - {col_label:<14s}: {status}")

        print("\n  操作选项：")
        print("    1. 检查/刷新指定类型的重复数据")
        print("    2. 查看重复记录明细")
        print("    3. 导出全部重复记录到独立数据库 (默认 .db)")
        print("    4. 导出全部重复记录为 CSV 审计表")
        print("    5. 批量去重（强制级联清理关联 PDF 与压缩数据库）")
        print("    6. 全量重新扫描数据库")
        print()
        print("    0. 返回上一级菜单")
        print("=" * 60)

        try:
            choice = input("  请选择 [0-6]: ").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if choice in ("0", "q", "quit", "exit"):
            break

        elif choice == "1":
            print("\n  选择要检查的重复类型：")
            for i, (col_key, col_label, key_tag) in enumerate(DUPLICATE_FIELDS, 1):
                cnt = len(all_dup_data[key_tag])
                print(f"    {i} - {col_label} (当前 {cnt} 条)")
            print("    a - 全部类型")
            sub = input(f"  请选择 [1-{len(DUPLICATE_FIELDS)}/a]: ").strip().lower()

            targets = DUPLICATE_FIELDS if sub == "a" else []
            if not targets:
                try:
                    idx = int(sub) - 1
                    if 0 <= idx < len(DUPLICATE_FIELDS):
                        targets = [DUPLICATE_FIELDS[idx]]
                except ValueError:
                    pass

            if not targets:
                print_error("无效的选择。")
                pause_for_user()
                continue

            for col_key, col_label, key_tag in targets:
                print_step(f"正在检查 {col_label} 重复...")
                all_dup_data[key_tag] = get_all_duplicates(conn, col_key, columns)
                print_dup_summary(all_dup_data[key_tag], col_key, col_label)

            pause_for_user()

        elif choice == "2":
            print("\n  选择要查看详情的重复类型：")
            for i, (col_key, col_label, key_tag) in enumerate(DUPLICATE_FIELDS, 1):
                cnt = len(all_dup_data[key_tag])
                print(f"    {i} - {col_label} ({cnt} 条)")
            sub = input(f"  请选择 [1-{len(DUPLICATE_FIELDS)}]: ").strip()
            try:
                idx = int(sub) - 1
                if 0 <= idx < len(DUPLICATE_FIELDS):
                    col_key, col_label, key_tag = DUPLICATE_FIELDS[idx]
                    print_dup_detail(all_dup_data[key_tag], col_key)
                else:
                    print_error("无效的选择。")
            except ValueError:
                print_error("无效的输入。")
            pause_for_user()

        elif choice in ("3", "4"):
            as_csv = (choice == "4")
            exported_any = False
            for col_key, col_label, key_tag in DUPLICATE_FIELDS:
                recs = all_dup_data[key_tag]
                if not recs:
                    continue
                out_path = export_duplicates(recs, tag=key_tag, as_csv=as_csv)
                if out_path:
                    exported_any = True

            if not exported_any:
                print_step("当前没有发现重复记录，无需导出。")
            pause_for_user()

        elif choice == "5":
            print("\n  选择要执行去重的类型：")
            for i, (col_key, col_label, key_tag) in enumerate(DUPLICATE_FIELDS, 1):
                cnt = len(all_dup_data[key_tag])
                print(f"    {i} - {col_label} ({cnt} 条)")
            print("    a - 全部类型")
            sub = input(f"  请选择 [1-{len(DUPLICATE_FIELDS)}/a]: ").strip().lower()

            targets = DUPLICATE_FIELDS if sub == "a" else []
            if not targets:
                try:
                    idx = int(sub) - 1
                    if 0 <= idx < len(DUPLICATE_FIELDS):
                        targets = [DUPLICATE_FIELDS[idx]]
                except ValueError:
                    pass

            if not targets:
                print_error("无效选择。")
                pause_for_user()
                continue

            print("\n  请选择去重模式：")
            print("    1 - 默认完整去重 (优先保留含PDF记录 > 保留唯一1条，强制级联删除其余副本的 PDF)")
            print("    2 - 仅删除无 PDF 的重复链接 (安全保护模式：仅清理无PDF链接副本，保护已有PDF记录与文件)")
            mode_input = input("  请选择 [1/2] (默认 1): ").strip()
            only_no_pdf = (mode_input == "2")

            grand_records = 0
            grand_pdfs_k, grand_bytes_k = 0, 0
            grand_pdfs_o, grand_bytes_o = 0, 0
            overview_lines = []

            for col_key, col_label, key_tag in targets:
                recs = all_dup_data[key_tag]
                if not recs:
                    continue
                ids_del, _ = plan_duplicate_deletions(recs, keep_newest=True, only_no_pdf=only_no_pdf)
                n_groups = len({r["_group_key"] for r in recs})
                pdfs_k, bytes_k = collect_pdf_stats(recs, keep_oldest=False, only_no_pdf=only_no_pdf)
                pdfs_o, bytes_o = collect_pdf_stats(recs, keep_oldest=True, only_no_pdf=only_no_pdf)
                if only_no_pdf:
                    overview_lines.append(
                        f"  - {col_label}: {n_groups} 组重复, {len(recs)} 条记录, 预计仅删除无 PDF 副本 {len(ids_del)} 条 (保护已有 PDF)"
                    )
                else:
                    overview_lines.append(
                        f"  - {col_label}: {n_groups} 组重复, {len(recs)} 条记录, 预计删除 {len(recs) - n_groups} 条\n"
                        f"      保留最新: 级联删 PDF {pdfs_k} 个, 释放空间 {format_size(bytes_k)}\n"
                        f"      保留最旧: 级联删 PDF {pdfs_o} 个, 释放空间 {format_size(bytes_o)}"
                    )
                grand_records += len(ids_del)
                grand_pdfs_k += pdfs_k
                grand_bytes_k += bytes_k
                grand_pdfs_o += pdfs_o
                grand_bytes_o += bytes_o

            if not grand_records:
                print_step("所选类型没有可清理的重复记录。")
                pause_for_user()
                continue

            banner_title = "【去重前预估概况 (仅清理无 PDF 链接)】" if only_no_pdf else "【去重前预估概况 (强制级联清理关联 PDF)】"
            print_section(banner_title)
            for line in overview_lines:
                print(line)
            print("  " + "─" * 56)
            print(f"  合计预计删除数据库记录: {grand_records} 条")
            if not only_no_pdf:
                print(f"  保留最新策略: 级联删除 PDF {grand_pdfs_k} 个, 释放空间 {format_size(grand_bytes_k)}")
                print(f"  保留最旧策略: 级联删除 PDF {grand_pdfs_o} 个, 释放空间 {format_size(grand_bytes_o)}")

            if not confirm_action("\n  确认开始执行去重？", default=False):
                print_step("已取消去重操作。")
                pause_for_user()
                continue

            print("\n  请选择保留策略：")
            print("    k - 优先保留有 PDF 记录 > 最新入库 (ID 最大)，删除其余副本 (默认)")
            print("    o - 优先保留有 PDF 记录 > 最旧入库 (ID 最小)，删除其余副本")
            keep_input = input("  请选择 [k/o] (默认 k): ").strip().lower()
            keep_newest = (keep_input != "o")

            # 备份数据库
            backup_db(db_path, prefix_tag="duplicates_clean")

            total_del_rec = 0
            total_del_pdf = 0
            total_fail_pdf = 0
            total_del_bytes = 0

            for col_key, col_label, key_tag in targets:
                recs = all_dup_data[key_tag]
                if not recs:
                    continue
                d_rec, d_pdf, f_pdf, d_bytes = delete_duplicates_batch(
                    conn, recs, keep_newest=keep_newest, only_no_pdf=only_no_pdf
                )
                print_success(f"{col_label}: 成功删除 {d_rec} 条数据库记录，级联删除 PDF {d_pdf} 个 (释放 {format_size(d_bytes)})")
                total_del_rec += d_rec
                total_del_pdf += d_pdf
                total_fail_pdf += f_pdf
                total_del_bytes += d_bytes
                # 刷新该项
                all_dup_data[key_tag] = get_all_duplicates(conn, col_key, columns)

            if total_del_rec > 0:
                vacuum_db(conn)
                print_banner("去重完成汇总")
                print(f"  删除重复记录数:        {total_del_rec} 条")
                if only_no_pdf:
                    print(f"  模式说明:              仅删除无 PDF 重复链接 (已有 PDF 记录已受保护)")
                else:
                    print(f"  级联删除 PDF 文件数:   {total_del_pdf} 个 (失败: {total_fail_pdf} 个)")
                    print(f"  释放物理磁盘空间:      {format_size(total_del_bytes)}")
                print("=" * 60)
            else:
                print_step("未删除任何记录。")

            pause_for_user()

        elif choice == "6":
            print_step("正在重新全量扫描数据库...")
            for col_key, col_label, key_tag in DUPLICATE_FIELDS:
                all_dup_data[key_tag] = get_all_duplicates(conn, col_key, columns)
                print_dup_summary(all_dup_data[key_tag], col_key, col_label)
            pause_for_user()

    conn.close()


def run_duplicates_cli(args) -> None:
    """查重与去重非交互 CLI 处理入口"""
    db_path = get_db_path(getattr(args, "db", None))
    if not os.path.exists(db_path):
        print_error(f"数据库文件不存在: {db_path}")
        return

    is_run = getattr(args, "run", False) or getattr(args, "yes", False)
    target_field = getattr(args, "field", "all")
    export_db_flag = getattr(args, "export_db", False)
    export_csv_flag = getattr(args, "export_csv", False)
    keep_strategy = getattr(args, "keep", "newest")
    only_no_pdf = getattr(args, "only_no_pdf", False)

    conn = get_connection(db_path)
    columns = get_columns(conn.cursor())

    selected_fields = []
    if target_field == "all":
        selected_fields = DUPLICATE_FIELDS
    else:
        for f in DUPLICATE_FIELDS:
            if f[2] == target_field:
                selected_fields.append(f)

    if not selected_fields:
        print_error(f"未知的查重字段类型: {target_field}")
        conn.close()
        return

    print_banner("数据库多维查重与去重系统 (CLI 模式)")
    print(f"[*] 运行模式: {'【正式执行 (RUN)】' if is_run else '【预览模式 (DRY RUN)】'}")
    print(f"[*] 数据库: {db_path}")
    print(f"[*] 查重字段: {target_field}")
    print(f"[*] 保留策略: {keep_strategy}")
    print(f"[*] 仅删无PDF: {'【开启 (仅清理无PDF链接，保护已有PDF)】' if only_no_pdf else '【关闭 (级联清理所有冗余副本及PDF)】'}")
    print("=" * 60)

    total_pred_del = 0
    for col_key, col_label, key_tag in selected_fields:
        recs = get_all_duplicates(conn, col_key, columns)
        print_dup_summary(recs, col_key, col_label)
        if recs:
            ids_del, _ = plan_duplicate_deletions(recs, keep_newest=(keep_strategy == "newest"), only_no_pdf=only_no_pdf)
            total_pred_del += len(ids_del)
        if export_db_flag:
            export_duplicates(recs, tag=key_tag, as_csv=False)
        if export_csv_flag:
            export_duplicates(recs, tag=key_tag, as_csv=True)

    if not is_run:
        print_step("当前为预览模式，未对数据库或 PDF 进行任何修改。")
        if only_no_pdf:
            print_step(f"已开启【仅删除无 PDF 重复链接】模式：预计将清理 {total_pred_del} 条无 PDF 冗余记录，保护已有 PDF 记录不受影响。")
        else:
            print_step(f"默认去重模式：预计将删除 {total_pred_del} 条冗余记录并同步级联清理多余 PDF。")
        print_step("若确认执行去重操作，请附加 --run 或 -y 参数。")
        conn.close()
        return

    # 正式执行去重
    backup_db(db_path, prefix_tag="cli_dedup")
    total_del_rec, total_del_pdf, total_fail, total_bytes = 0, 0, 0, 0

    for col_key, col_label, key_tag in selected_fields:
        recs = get_all_duplicates(conn, col_key, columns)
        if not recs:
            continue
        d_rec, d_pdf, f_pdf, d_bytes = delete_duplicates_batch(
            conn, recs, keep_newest=(keep_strategy == "newest"), only_no_pdf=only_no_pdf
        )
        total_del_rec += d_rec
        total_del_pdf += d_pdf
        total_fail += f_pdf
        total_bytes += d_bytes

    vacuum_db(conn)
    conn.close()

    print_banner("CLI 去重执行结果")
    print(f"  删除记录总数:          {total_del_rec} 条")
    if only_no_pdf:
        print(f"  模式说明:              仅删除无 PDF 重复链接 (已有 PDF 记录 100% 安全保护)")
    else:
        print(f"  级联删除 PDF 文件数:   {total_del_pdf} 个 (失败: {total_fail})")
        print(f"  释放物理磁盘空间:      {format_size(total_bytes)}")
    print("=" * 60)


# ===================================================================
# 2. fanhao: 严格日本番号过滤、导出与清理 (默认 DB 导出，级联删 PDF)
# ===================================================================

def scan_fanhao_records(conn: sqlite3.Connection) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[str]]:
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


def export_fanhao_records(matches: List[Tuple[str, Dict[str, Any]]], columns: List[str], output_path: Optional[str] = None, as_csv: bool = False) -> str:
    """将匹配的番号记录导出到独立 SQLite 数据库文件 (默认 .db) 或 CSV"""
    records_with_fanhao = []
    for fanhao, row_dict in matches:
        item = dict(row_dict)
        item["fanhao"] = fanhao
        records_with_fanhao.append(item)

    ts = get_timestamp()
    if as_csv:
        filename = output_path or f"fanhao_export_{ts}.csv"
        return export_to_csv(records_with_fanhao, filename)
    else:
        filename = output_path or f"fanhao_export_{ts}.db"
        return export_records_to_db(records_with_fanhao, filename, table_name="resources", unique_col="url")


def delete_fanhao_records(
    conn: sqlite3.Connection,
    matches: List[Tuple[str, Dict[str, Any]]],
    is_run: bool = False,
    force: bool = False,
) -> None:
    """删除匹配的番号记录并【强制级联删除对应物理 PDF】"""
    ids_to_delete = [row_dict["id"] for _, row_dict in matches]

    print_section(f"准备删除 {len(ids_to_delete)} 条番号记录并【强制级联清理关联 PDF】")

    if not is_run:
        print_step("【预览模式】待删除记录样例 (Top 20):")
        for i, (fanhao, row_dict) in enumerate(matches[:20], 1):
            print(f"  {i:4d}. [{fanhao}] {(row_dict.get('title') or '')[:60]}")
        if len(matches) > 20:
            print(f"  ... 以及其他 {len(matches) - 20} 条记录")
        print_step(f"共 {len(ids_to_delete)} 条记录将被删除。若确认执行，请使用 --run 或在交互菜单中确认。")
        return

    if not force:
        if not confirm_action(f"确定要删除这 {len(ids_to_delete)} 条记录并永久删除关联的 PDF 文件吗？", default=False):
            print_step("已取消删除操作。")
            return

    backup_db(get_db_path(), prefix_tag="fanhao_delete")

    del_rec, del_pdf, fail_pdf, freed_bytes = delete_records_cascade_pdf(conn, ids_to_delete, project_root=PROJECT_ROOT)
    vacuum_db(conn)

    print_banner("番号记录清理完成")
    print(f"  删除数据库记录数:      {del_rec} 条")
    print(f"  级联删除 PDF 文件数:   {del_pdf} 个 (失败: {fail_pdf})")
    print(f"  释放物理磁盘空间:      {format_size(freed_bytes)}")
    print(f"  数据库中剩余记录:      {get_total_count(conn)} 条")
    print("=" * 60)


def run_fanhao_cli(args) -> None:
    """番号过滤与分离 CLI 处理入口"""
    db_path = get_db_path(getattr(args, "db", None))
    if not os.path.exists(db_path):
        print_error(f"数据库文件不存在: {db_path}")
        return

    conn = get_connection(db_path)
    total = get_total_count(conn)
    print_step(f"正在扫描数据库番号: {db_path} (共 {total} 条记录)...")

    matches, columns = scan_fanhao_records(conn)
    print_step(f"找到含番号记录: {len(matches)} 条 (占比: {len(matches)/max(total, 1)*100:.1f}%)")

    if not matches:
        print_success("没有找到任何含番号记录。")
        conn.close()
        return

    mode = getattr(args, "mode", "scan")
    is_run = getattr(args, "run", False) or getattr(args, "yes", False)
    as_csv = getattr(args, "export_csv", False)

    if mode == "scan":
        prefix_stats: Dict[str, int] = {}
        for fanhao, _ in matches:
            prefix = re.sub(r"[\d\-_ ].*$", "", fanhao).upper() or fanhao.upper()
            prefix_stats[prefix] = prefix_stats.get(prefix, 0) + 1
        print("\n[*] 番号前缀分布 (Top 20):")
        for prefix, count in sorted(prefix_stats.items(), key=lambda x: -x[1])[:20]:
            print(f"    {prefix:<12s} : {count:5d} 条")

        if as_csv:
            export_fanhao_records(matches, columns, as_csv=True)

        print("\n提示: 可使用 --mode export (导出独立库) 或 --mode delete (批量清理)。")
        conn.close()
        return

    if mode == "export":
        out_path = getattr(args, "output", None)
        if not is_run:
            print_step(f"【预览模式】共 {len(matches)} 条番号记录将被导出到独立数据库 (默认 .db)。")
            print_step("若确认导出，请附加 --run 参数。")
        else:
            conn.close()
            export_fanhao_records(matches, columns, output_path=out_path, as_csv=as_csv)
    else:  # delete
        delete_fanhao_records(conn, matches, is_run=is_run, force=getattr(args, "yes", False))
        conn.close()


# ===================================================================
# 3. 主入口与全局交互式主菜单 (循环常驻)
# ===================================================================

def interactive_menu() -> None:
    """记录过滤与去重全局主菜单 (常驻循环)"""
    while True:
        print_banner("记录过滤、查重与番号分离工具合集")
        print("  请选择要进入的功能模块：")
        print()
        print("    1. duplicates - 数据库多维查重、默认 DB 导出、批量去重（强制级联删 PDF）")
        print("    2. fanhao     - 严格番号识别、分布统计、导出独立 .db 库或批量清理")
        print()
        print("    0. 退出程序")
        print("=" * 60)

        try:
            choice = input("  请输入序号 [0-2] (直接回车默认 1): ").strip()
            if not choice:
                choice = "1"
        except (KeyboardInterrupt, EOFError):
            print("\n[*] 操作已取消。")
            break

        if choice in ("0", "q", "quit", "exit"):
            print_step("已退出程序。")
            break

        if choice == "1":
            run_duplicates_menu()
        elif choice == "2":
            db_path = get_db_path()
            conn = get_connection(db_path)
            total = get_total_count(conn)
            print_step(f"正在扫描数据库番号: {db_path} (共 {total} 条)...")
            matches, columns = scan_fanhao_records(conn)
            print_step(f"找到含番号记录: {len(matches)} 条")

            if matches:
                prefix_stats: Dict[str, int] = {}
                for fanhao, _ in matches:
                    prefix = re.sub(r"[\d\-_ ].*$", "", fanhao).upper() or fanhao.upper()
                    prefix_stats[prefix] = prefix_stats.get(prefix, 0) + 1
                print("\n[*] 番号前缀分布 (Top 20):")
                for prefix, count in sorted(prefix_stats.items(), key=lambda x: -x[1])[:20]:
                    print(f"    {prefix:<12s} : {count:5d} 条")

                print("\n  操作选项:")
                print("    1. 导出匹配记录到独立 SQLite 数据库 (默认 .db)")
                print("    2. 导出匹配记录到 CSV 审计表")
                print("    3. 批量删除匹配记录（【强制级联删除关联 PDF】）")
                print("    0. 返回主菜单")

                sub_c = input("  请选择 [0-3]: ").strip()
                if sub_c == "1":
                    export_fanhao_records(matches, columns, as_csv=False)
                elif sub_c == "2":
                    export_fanhao_records(matches, columns, as_csv=True)
                elif sub_c == "3":
                    delete_fanhao_records(conn, matches, is_run=True, force=False)

            conn.close()
            pause_for_user()


def main():
    parser = argparse.ArgumentParser(
        description="记录过滤与去重清理工具合集 (查重与清理 / 番号识别与导出清理)"
    )
    subparsers = parser.add_subparsers(dest="command", help="可用的子命令")

    # duplicates
    p_dup = subparsers.add_parser("duplicates", help="数据库查重、默认 DB 导出、批量去重并级联清理 PDF")
    p_dup.add_argument("--field", choices=["url", "resource_link", "title_link", "all"], default="all", help="查重字段维度")
    p_dup.add_argument("--keep", choices=["newest", "oldest"], default="newest", help="保留策略: newest (优先保留有PDF记录 > 最新ID, 默认), oldest (优先保留有PDF记录 > 最旧ID)")
    p_dup.add_argument("--only-no-pdf", action="store_true", default=False, help="仅删除无 PDF 的重复记录/链接（安全保护已有 PDF 记录，不删除任何 PDF 文件）")
    p_dup.add_argument("--run", action="store_true", default=False, help="正式执行去重（默认仅预览）")
    p_dup.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_dup.add_argument("--export-db", action="store_true", default=False, help="导出重复记录为独立 .db 数据库")
    p_dup.add_argument("--export-csv", action="store_true", default=False, help="导出重复记录为 CSV 审计表")
    p_dup.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认直接执行")
    p_dup.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_dup.set_defaults(func=run_duplicates_cli)

    # fanhao
    p_fan = subparsers.add_parser("fanhao", help="严格日本番号识别、分布统计、导出独立库或批量清理")
    p_fan.add_argument("--mode", choices=["scan", "export", "delete"], default="scan", help="操作模式: scan(扫描统计), export(导出), delete(删除)")
    p_fan.add_argument("--output", type=str, default=None, help="导出模式下的输出文件路径")
    p_fan.add_argument("--export-csv", action="store_true", default=False, help="导出为 CSV 审计表 (默认导出为 .db)")
    p_fan.add_argument("--run", action="store_true", default=False, help="正式执行导出或删除（默认预览）")
    p_fan.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_fan.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_fan.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_fan.set_defaults(func=run_fanhao_cli)

    args = parser.parse_args()

    if args.command is None:
        interactive_menu()
    else:
        # 如果传入了子命令但没有任何操作参数，且未指定 --run / --dry-run / 导出等，则进入对应子交互菜单
        if args.command == "duplicates" and len(sys.argv) == 2:
            run_duplicates_menu(args)
        else:
            args.func(args)


if __name__ == "__main__":
    main()

