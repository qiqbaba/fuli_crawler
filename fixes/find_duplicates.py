"""查找并管理数据库中 URL 和磁力链接的重复记录

功能：
1. 扫描数据库中按 url 或 resource_link 分组有重复的记录
2. 将所有重复记录的所有字段信息导出到 CSV 文件
3. 提供交互式删除功能（保留一条记录，删除其余）
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


def delete_duplicates_interactive(
    conn,
    records: List[Dict],
    column: ColumnSpec,
    label: str,
) -> int:
    """交互式删除重复记录

    Args:
        conn: 数据库连接
        records: 重复记录列表
        column: 重复列名
        label: 显示标签

    Returns:
        删除的记录数
    """
    if not records:
        return 0

    groups: Dict[str, List[Dict]] = {}
    for rec in records:
        key = rec["_group_key"]
        groups.setdefault(key, []).append(rec)

    total_deleted = 0
    cursor = conn.cursor()

    is_composite = isinstance(column, tuple)

    for gidx, (key, group) in enumerate(groups.items(), 1):
        print(f"\n{'=' * 70}")
        print(f"  [{gidx}/{len(groups)}] {label} 重复组")
        if is_composite:
            parts = key.split("|||", 1)
            print(f"  标题: {parts[0][:60] if len(parts) > 0 else ''}")
            if len(parts) > 1:
                print(f"  链接: {parts[1][:80]}{'...' if len(parts[1]) > 80 else ''}")
        else:
            print(f"  重复值: {key[:100]}")
        print(f"{'=' * 70}")

        # 按 ID 排序显示
        group_sorted = sorted(group, key=lambda r: r.get("id", 0) or 0)

        for i, rec in enumerate(group_sorted):
            title = (rec.get("title") or "")[:60]
            rid = rec.get("id", "?")
            print(f"    [{i + 1}] ID={rid}, title={title}")

        print(f"\n  该组共 {len(group_sorted)} 条重复记录。")
        print(f"  操作选项：")
        print(f"    k - 保留最新一条 (ID 最大)，删除其余")
        print(f"    o - 保留最旧一条 (ID 最小)，删除其余")
        print(f"    s - 跳过此组")
        print(f"    q - 退出删除流程")

        choice = input(f"  请选择 [k/o/s/q] (默认 s): ").strip().lower()
        if choice == "q":
            print("  退出删除流程。")
            break
        if choice == "s" or not choice:
            print("  已跳过。")
            continue

        # 确定保留的 ID
        ids_sorted = sorted(
            [r.get("id", 0) or 0 for r in group_sorted]
        )
        if choice == "k":
            keep_id = ids_sorted[-1]  # 最大
        elif choice == "o":
            keep_id = ids_sorted[0]  # 最小
        else:
            print("  无效选项，已跳过。")
            continue

        delete_ids = [str(i) for i in ids_sorted if i != keep_id]
        if not delete_ids:
            print("  没有可删除的记录。")
            continue

        placeholders = ",".join("?" for _ in delete_ids)
        cursor.execute(
            f"DELETE FROM resources WHERE id IN ({placeholders})",
            delete_ids,
        )
        conn.commit()
        deleted = cursor.rowcount
        total_deleted += deleted
        print(f"  [✓] 已删除 {deleted} 条记录，保留 ID={keep_id}。")

    return total_deleted


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
            # 重复检查 — 重新扫描
            for col_key, col_label in DUPLICATE_FIELDS:
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

            total_deleted = 0
            for col_key, col_label in targets:
                if not all_dup_data[col_key]:
                    print(f"  {col_label} 无重复记录，跳过。")
                    continue
                deleted = delete_duplicates_interactive(
                    conn, all_dup_data[col_key], col_key, col_label
                )
                total_deleted += deleted
                # 删除后重新扫描
                all_dup_data[col_key] = get_all_duplicates(
                    conn, col_key, all_columns
                )

            if total_deleted:
                print(f"\n  [✓] 共删除 {total_deleted} 条重复记录。")
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