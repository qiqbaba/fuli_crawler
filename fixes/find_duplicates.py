"""查找并管理数据库中 URL 和磁力链接的重复记录

功能：
1. 扫描数据库中按 url 或 resource_link 分组有重复的记录
2. 将所有重复记录的所有字段信息导出到 CSV 文件
3. 提供交互式删除功能（保留一条记录，删除其余，可同时删除关联的 PDF 文件）
"""

import csv
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ========== 项目路径引导 ==========
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root not in sys.path:
    sys.path.insert(0, root)

from utils import setup_console_utf8
setup_console_utf8()

from fixes.db_utils import get_connection, get_columns, get_db_path  # noqa: E402

# ========== 常量 ==========
CSV_OUTPUT_DIR = "D:\\"
DUPLICATE_FIELDS = [
    ("url", "URL 地址"),
    ("resource_link", "磁力/资源链接"),
    (("title", "resource_link"), "标题+磁力链接"),
]


ColumnSpec = str | tuple[str, str]


def resolve_pdf_path(pdf_path: str) -> str:
    """将数据库中可能为相对路径的 pdf_path 转为绝对路径

    Args:
        pdf_path: 数据库中存储的 pdf_path（相对或绝对路径）

    Returns:
        绝对路径；若为空则返回空字符串
    """
    if not pdf_path:
        return ""
    if os.path.isabs(pdf_path):
        return pdf_path
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_dir, pdf_path)


def get_all_duplicates(
    conn,
    column: ColumnSpec,
    columns: List[str],
) -> List[Dict]:
    """查找指定列（或列组合）上有重复值的所有记录，按重复值分组返回

    Args:
        conn: 数据库连接
        column: 要检查重复的列名（如 'url'），或列名元组（如 ('title','resource_link')）
        columns: 要查询的列名列表

    Returns:
        所有重复记录的列表，每条记录包含 group_key（重复值）和所有字段
    """
    cursor = conn.cursor()

    if isinstance(column, tuple):
        # --- 复合键重复检查（如 title + resource_link） ---
        col1, col2 = column
        col_list = ", ".join(columns)

        # 使用临时表避免 SQLite 参数数量上限（默认 999 个 ?）
        cursor.execute("DROP TABLE IF EXISTS _dup_pairs")
        cursor.execute(f"""
            CREATE TEMP TABLE _dup_pairs (
                {col1} TEXT NOT NULL,
                {col2} TEXT NOT NULL
            )
        """)

        cursor.execute(f"""
            INSERT INTO _dup_pairs ({col1}, {col2})
            SELECT {col1}, {col2}
            FROM resources
            WHERE {col1} IS NOT NULL AND {col1} != ''
              AND {col2} IS NOT NULL AND {col2} != ''
            GROUP BY {col1}, {col2}
            HAVING COUNT(*) > 1
        """)

        cursor.execute("SELECT COUNT(*) FROM _dup_pairs")
        if cursor.fetchone()[0] == 0:
            cursor.execute("DROP TABLE IF EXISTS _dup_pairs")
            return []

        r_col_list = ", ".join(f"r.{c}" for c in columns)
        cursor.execute(f"""
            SELECT {r_col_list}
            FROM resources r
            INNER JOIN _dup_pairs d
                ON r.{col1} = d.{col1}
               AND r.{col2} = d.{col2}
            ORDER BY r.{col1}, r.{col2}, r.id
        """)
        rows = cursor.fetchall()
        cursor.execute("DROP TABLE IF EXISTS _dup_pairs")

        result = []
        for row in rows:
            record = dict(zip(columns, row))
            record["_dup_column"] = f"{col1}+{col2}"
            record["_group_key"] = f"{record.get(col1, '')}|||{record.get(col2, '')}"
            result.append(record)
        return result

    else:
        # --- 单列重复检查 ---
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


def export_to_csv(records: List[Dict], column: ColumnSpec, filepath: str) -> str:
    """将重复记录导出到 CSV 文件

    Args:
        records: 重复记录列表
        column: 重复类型（列名）
        filepath: 输出文件路径

    Returns:
        实际写入的文件路径
    """
    if not records:
        return ""

    # 提取所有字段，去掉内部字段
    fieldnames = [k for k in records[0].keys() if not k.startswith("_")]

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            clean = {k: v for k, v in rec.items() if not k.startswith("_")}
            writer.writerow(clean)

    return filepath


def print_dup_summary(records: List[Dict], column: ColumnSpec, label: str):
    """打印重复记录的摘要信息"""
    if not records:
        print(f"  [✓] 未发现 {label} 重复记录。")
        return

    # 按 group_key 分组统计
    groups: Dict[str, List[Dict]] = {}
    for rec in records:
        key = rec["_group_key"]
        groups.setdefault(key, []).append(rec)

    is_composite = isinstance(column, tuple)

    print(f"\n  [发现] {label} 重复，共 {len(records)} 条记录，{len(groups)} 组重复：")
    print(f"  {'=' * 70}")
    for idx, (key, group) in enumerate(groups.items(), 1):
        if is_composite:
            parts = key.split("|||", 1)
            title_part = parts[0][:60] if len(parts) > 0 else ""
            link_part = (parts[1][:60] + "...") if len(parts) > 1 and len(parts[1]) > 60 else (parts[1] if len(parts) > 1 else "")
            print(f"  [{idx}] 标题: {title_part}")
            print(f"      链接: {link_part}")
        else:
            print(f"  [{idx}] 重复值: {key[:80]}{'...' if len(key) > 80 else ''}")
        print(f"      重复次数: {len(group)} 条")
        for rec in group:
            title = (rec.get("title") or "")[:50]
            rid = rec.get("id", "?")
            print(f"      - ID={rid}, title={title}")
        print()


def print_detail(records: List[Dict], column: ColumnSpec):
    """打印重复记录的详细字段"""
    if not records:
        return

    groups: Dict[str, List[Dict]] = {}
    for rec in records:
        key = rec["_group_key"]
        groups.setdefault(key, []).append(rec)

    all_columns = [k for k in records[0].keys() if not k.startswith("_")]
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
            for col in all_columns:
                val = rec.get(col, "")
                if val is None:
                    val = ""
                if len(str(val)) > 120:
                    val = str(val)[:120] + "..."
                print(f"    {col}: {val}")
            print()


def format_size(num_bytes: float) -> str:
    """将字节数格式化为易读的大小字符串"""
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.2f} GB"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / 1024 ** 2:.2f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes:.0f} B"


def collect_pdf_stats(records: List[Dict], keep_oldest: bool = False) -> Tuple[int, int]:
    """统计按指定保留策略删除重复记录时会移除的 PDF 文件

    Args:
        records: 重复记录列表
        keep_oldest: True 表示保留最旧一条 (ID 最小)，False 表示保留最新一条 (ID 最大)

    Returns:
        (会删除的 PDF 文件数, 总字节数)，仅统计物理存在的文件，跨组去重
    """
    groups: Dict[str, List[Dict]] = {}
    for rec in records:
        key = rec["_group_key"]
        groups.setdefault(key, []).append(rec)

    pdf_files: List[str] = []
    seen: set = set()
    for group in groups.values():
        group_sorted = sorted(group, key=lambda r: r.get("id", 0) or 0)
        # 与删除逻辑一致：优先保留有 PDF 的记录
        with_pdf = [r for r in group_sorted if (r.get("pdf_path") or "").strip()]
        candidates = with_pdf if with_pdf else group_sorted
        kept_id = candidates[0].get("id") if keep_oldest else candidates[-1].get("id")
        for rec in group_sorted:
            if (rec.get("id", 0) or 0) == kept_id:
                continue
            p = rec.get("pdf_path") or ""
            if not p:
                continue
            abs_p = resolve_pdf_path(p)
            key_p = abs_p.lower().replace("\\", "/")
            if key_p in seen:
                continue
            seen.add(key_p)
            if os.path.exists(abs_p):
                pdf_files.append(abs_p)

    total_bytes = sum(os.path.getsize(f) for f in pdf_files)
    return len(pdf_files), total_bytes


def delete_duplicates_batch(
    conn,
    records: List[Dict],
    column: ColumnSpec,
    label: str,
    keep_newest: bool = True,
    delete_pdf: bool = False,
) -> Tuple[int, int, int, int]:
    """批量删除重复记录，统一保留策略应用于所有重复组

    Args:
        conn: 数据库连接
        records: 重复记录列表
        column: 重复列名
        label: 显示标签
        keep_newest: True 保留最新一条 (ID 最大)，False 保留最旧一条 (ID 最小)
        delete_pdf: 是否同时删除关联的 PDF 文件

    Returns:
        (删除的记录数, 删除的 PDF 文件数, 删除失败的 PDF 文件数, 删除的 PDF 总字节数)
    """
    if not records:
        return 0, 0, 0, 0

    groups: Dict[str, List[Dict]] = {}
    for rec in records:
        key = rec["_group_key"]
        groups.setdefault(key, []).append(rec)

    total_deleted = 0
    total_pdf_deleted = 0
    total_pdf_failed = 0
    total_pdf_bytes = 0
    cursor = conn.cursor()

    n_groups = len(groups)
    # 进度间隔：最多 100 行，最少每 5 组一行
    progress_interval = max(5, (n_groups + 99) // 100)

    for gidx, (key, group) in enumerate(groups.items(), 1):
        # 进度指示
        if gidx % progress_interval == 0 or gidx == n_groups:
            print(f"  处理中... {gidx}/{n_groups} 组 ({gidx * 100 // n_groups}%)")

        # 按 ID 排序显示
        group_sorted = sorted(group, key=lambda r: r.get("id", 0) or 0)

        # 确定保留的 ID：优先保留有 PDF 的记录（无 PDF 的记录优先删除），
        # 仅当组内都无 PDF 时才在全部记录中按策略选择
        with_pdf = [r for r in group_sorted if (r.get("pdf_path") or "").strip()]
        candidates = with_pdf if with_pdf else group_sorted
        ids_sorted = sorted(r.get("id", 0) or 0 for r in candidates)
        keep_id = ids_sorted[-1] if keep_newest else ids_sorted[0]  # 最大/最小

        delete_ids = [str(i) for i in ids_sorted if i != keep_id]
        if not delete_ids:
            continue

        # 收集待删除记录关联的 PDF 文件（去重，避免多个记录引用同一文件）
        keep_id_int = int(keep_id)
        pdf_files_to_delete: List[str] = []
        seen: set = set()
        for rec in group_sorted:
            if (rec.get("id", 0) or 0) == keep_id_int:
                continue
            p = rec.get("pdf_path") or ""
            if not p:
                continue
            abs_p = resolve_pdf_path(p)
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
        cursor.execute(
            f"DELETE FROM resources WHERE id IN ({placeholders})",
            delete_ids,
        )
        conn.commit()
        deleted = cursor.rowcount
        total_deleted += deleted
        total_pdf_deleted += deleted_pdfs
        total_pdf_failed += failed_pdfs
        total_pdf_bytes += deleted_pdf_bytes

    return total_deleted, total_pdf_deleted, total_pdf_failed, total_pdf_bytes


def interactive_menu():
    """主交互菜单"""
    # 获取数据库路径
    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(f"[!] 数据库文件不存在: {db_path}")
        return

    print(f"\n{'=' * 60}")
    print(f"  数据库重复记录查找与清理工具")
    print(f"{'=' * 60}")
    print(f"  数据库: {db_path}")
    print()

    conn = get_connection(db_path)
    columns = get_columns(conn.cursor())
    all_columns = columns  # 所有字段

    # 缓存所有重复数据
    all_dup_data: Dict[ColumnSpec, List[Dict]] = {}

    # 初次加载
    for col_key, col_label in DUPLICATE_FIELDS:
        all_dup_data[col_key] = get_all_duplicates(conn, col_key, all_columns)

    while True:
        print(f"\n{'─' * 60}")
        print(f"  主菜单")
        print(f"{'─' * 60}")

        has_any = any(all_dup_data.values())
        for col_key, col_label in DUPLICATE_FIELDS:
            cnt = len(all_dup_data[col_key])
            status = f"{cnt} 条重复" if cnt else "无重复"
            print(f"    1 - {col_label} 重复检查 ({status})")

        print(f"    2 - 查看重复详情")
        print(f"    3 - 导出全部重复到 CSV")
        print(f"    4 - 删除重复记录")
        print(f"    5 - 重新扫描数据库")
        print(f"    0 - 退出")

        choice = input(f"\n  请选择 [0-5]: ").strip()

        if choice == "0":
            print("  再见！")
            break

        elif choice == "1":
            # 重复检查 — 选择检查标准
            print(f"\n  选择要检查的重复类型：")
            for i, (col_key, col_label) in enumerate(DUPLICATE_FIELDS, 1):
                cnt = len(all_dup_data[col_key])
                print(f"    {i} - {col_label} (当前 {cnt} 条)")
            print(f"    a - 全部类型")
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
                all_dup_data[col_key] = get_all_duplicates(
                    conn, col_key, all_columns
                )
                print_dup_summary(all_dup_data[col_key], col_key, col_label)

        elif choice == "2":
            # 查看详情
            print(f"\n  选择要查看的重复类型：")
            for i, (col_key, col_label) in enumerate(DUPLICATE_FIELDS, 1):
                cnt = len(all_dup_data[col_key])
                print(f"    {i} - {col_label} ({cnt} 条)")
            sub = input(f"  请选择 [1-{len(DUPLICATE_FIELDS)}]: ").strip()
            try:
                idx = int(sub) - 1
                if 0 <= idx < len(DUPLICATE_FIELDS):
                    col_key, col_label = DUPLICATE_FIELDS[idx]
                    print_detail(all_dup_data[col_key], col_key)
                else:
                    print("  无效选择。")
            except ValueError:
                print("  无效输入。")

        elif choice == "3":
            # 导出 CSV
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            exported = []
            for col_key, col_label in DUPLICATE_FIELDS:
                if not all_dup_data[col_key]:
                    continue
                # 构建文件名友好键名（元组转字符串）
                if isinstance(col_key, tuple):
                    file_key = "_".join(col_key)
                else:
                    file_key = col_key
                filename = f"duplicates_{file_key}_{timestamp}.csv"
                filepath = os.path.join(CSV_OUTPUT_DIR, filename)
                actual = export_to_csv(all_dup_data[col_key], col_key, filepath)
                if actual:
                    exported.append((col_label, actual, len(all_dup_data[col_key])))

            if exported:
                print(f"\n  导出完成：")
                for label, path, cnt in exported:
                    print(f"    [✓] {label}: {cnt} 条 -> {path}")
            else:
                print(f"\n  没有重复记录可导出。")

        elif choice == "4":
            # 删除重复
            print(f"\n  选择要删除的重复类型：")
            for i, (col_key, col_label) in enumerate(DUPLICATE_FIELDS, 1):
                cnt = len(all_dup_data[col_key])
                print(f"    {i} - {col_label} ({cnt} 条)")
            print(f"    a - 全部类型")
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

            # ===== 删除前概况确认 =====
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
            print(f"  [概况确认] 删除前预览")
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

            # ===== 一次性确认保留策略（应用于全部重复组） =====
            print(f"\n  请选择保留策略（一次性确认，应用于全部重复组）：")
            print(f"    k - 保留最新一条 (ID 最大)，删除其余")
            print(f"    o - 保留最旧一条 (ID 最小)，删除其余")
            keep_input = input("  请选择 [k/o] (默认 k): ").strip().lower()
            keep_newest = keep_input != "o"

            pdf_input = input("  是否同时删除关联的 PDF 文件？[y/N] (默认 N): ").strip().lower()
            delete_pdf = pdf_input == "y"

            print(f"\n  >>> 开始删除（策略: {'保留最新' if keep_newest else '保留最旧'}"
                  f"{', 含 PDF' if delete_pdf else ', 不含 PDF'}）...")

            total_deleted = 0
            total_pdf_deleted = 0
            total_pdf_failed = 0
            total_pdf_bytes = 0
            for col_key, col_label in targets:
                if not all_dup_data[col_key]:
                    print(f"  {col_label} 无重复记录，跳过。")
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
                # 删除后重新扫描
                all_dup_data[col_key] = get_all_duplicates(
                    conn, col_key, all_columns
                )

            if total_deleted:
                msg = f"\n  [✓] 共删除 {total_deleted} 条重复记录"
                if total_pdf_deleted:
                    msg += f"，PDF 文件 {total_pdf_deleted} 个，共 {format_size(total_pdf_bytes)}"
                    if total_pdf_failed:
                        msg += f"（失败 {total_pdf_failed} 个）"
                msg += "。"
                print(msg)
            else:
                print(f"\n  未删除任何记录。")

        elif choice == "5":
            # 重新扫描
            print(f"\n  >>> 正在重新扫描数据库...")
            for col_key, col_label in DUPLICATE_FIELDS:
                print(f"  >>> 正在检查 {col_label} 重复...")
                all_dup_data[col_key] = get_all_duplicates(
                    conn, col_key, all_columns
                )
                print_dup_summary(all_dup_data[col_key], col_key, col_label)

        else:
            print("  无效选择，请重新输入。")

    conn.close()


if __name__ == "__main__":
    interactive_menu()