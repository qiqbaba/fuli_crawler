"""fixes/ 目录数据库与文件维护操作公共工具库 (fixes/db_utils.py)

主要用途与功能概览：
1. 模块环境引导 (setup_fixes_module):
   - 自动定位项目根目录并安全插入 sys.path，保证 fixes/ 内脚本可直接作为入口独立执行。
   - 初始化 Windows 控制台 UTF-8 编码输出，防止中文乱码。

2. 数据库连接与元数据探测:
   - get_db_path: 统一解析并返回有效的 SQLite 数据库路径（优先使用参数指定路径，缺省回退到 config.get_db_path()）。
   - get_connection: 获取标准 SQLite 数据库连接。
   - get_columns: 使用 PRAGMA table_info 探测指定数据表（默认 resources）的全部现有列名字段。
   - get_total_count: 快速统计指定数据表中的记录总行数。

3. 路径安全解析 (resolve_pdf_path):
   - 兼容处理数据库中存储的历史相对路径与绝对路径，统一转换为项目根目录下的有效绝对路径。

4. 统一导出规范与导出器 (get_export_dir / export_records_to_db / export_to_csv):
   - 导出目录优先级：优先 D:\\ 盘；若 D 盘不可用则导出至 temp_profiles/ 目录；支持 EXPORT_DIR / CSV_OUTPUT_DIR 环境变量覆盖。
   - 默认导出为 SQLite .db 独立文件，同时支持 utf-8-sig CSV 审计表与 JSON 数据导出。

5. 级联数据安全与物理文件清理 (delete_records_cascade_pdf):
   - 所有删除数据库记录操作强制且必须同步级联删除关联的物理 PDF 文件，彻底杜绝磁盘孤儿文件残留。
   - backup_db: 在执行任何破坏性写入前自动创建 .bak 副本。
   - format_size / vacuum_db: 容量易读格式化与磁盘碎片压缩。

6. 统一控制台 UI 与交互循环支持:
   - print_banner / print_section / print_step / print_success / print_warning / print_error
   - confirm_action / pause_for_user / ask_choice
"""

import csv
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


def setup_fixes_module() -> str:
    """将项目根目录加入 sys.path 并配置 UTF-8 控制台

    供 fixes/ 下各脚本在模块顶部调用。
    Returns:
        项目根目录绝对路径
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    # 配置 UTF-8 控制台
    from utils import setup_console_utf8  # noqa: E402
    setup_console_utf8()
    return root


# ===================================================================
# 1. 数据库连接、探测与路径解析
# ===================================================================

def get_connection(db_path: str) -> sqlite3.Connection:
    """打开 SQLite 数据库连接"""
    return sqlite3.connect(db_path)


def get_columns(cursor: sqlite3.Cursor, table_name: str = "resources") -> List[str]:
    """获取指定表所有列名（默认 resources 表）"""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def get_total_count(conn: sqlite3.Connection, table_name: str = "resources") -> int:
    """获取指定表的记录总数"""
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    row = cursor.fetchone()
    return row[0] if row else 0


def get_db_path(db_path: Optional[str] = None) -> str:
    """获取数据库路径，若未指定则使用 config 中的默认路径

    Args:
        db_path: 可选的自定义路径，若提供则直接返回

    Returns:
        有效的数据库文件绝对路径
    """
    if db_path:
        return db_path
    from config import get_db_path as _get_default_path  # noqa: E402
    return _get_default_path()


def resolve_pdf_path(pdf_path: str, project_root: Optional[str] = None) -> str:
    """将可能为相对路径的 pdf_path 转换为绝对路径

    Args:
        pdf_path: 数据库中存储的 pdf_path
        project_root: 可选项目根目录，未提供时自动计算

    Returns:
        绝对路径；若为空则返回空字符串
    """
    if not pdf_path:
        return ""
    if os.path.isabs(pdf_path):
        return pdf_path
    root = project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, pdf_path)


# ===================================================================
# 2. 统一导出路径与导出处理器 (默认 DB，支持 CSV/JSON)
# ===================================================================

def get_export_dir(sub_dir: Optional[str] = None) -> str:
    """获取统一的导出目录路径

    规则：
    1. 优先读取环境变量 EXPORT_DIR 或 CSV_OUTPUT_DIR
    2. 优先使用 D:\\ 盘根目录（若 D 盘存在）
    3. 若 D 盘不存在，则使用项目根目录下的 temp_profiles 目录
    4. 若指定了 sub_dir，则在其下方创建子目录

    Returns:
        已确保存在的导出目录绝对路径
    """
    export_env = os.environ.get("EXPORT_DIR") or os.environ.get("CSV_OUTPUT_DIR")
    if export_env:
        base_dir = export_env
    elif os.path.exists("D:\\"):
        base_dir = "D:\\"
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base_dir = os.path.join(root, "temp_profiles")

    if sub_dir:
        base_dir = os.path.join(base_dir, sub_dir)

    try:
        os.makedirs(base_dir, exist_ok=True)
    except Exception as e:
        print(f"[-] 创建导出目录失败 {base_dir}: {e}，回退到当前工作目录")
        base_dir = os.getcwd()

    return base_dir


def get_timestamp() -> str:
    """生成标准时间戳字符串 (YYYYMMDD_HHMMSS)"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def export_records_to_db(
    records: List[Dict[str, Any]],
    filename: str,
    table_name: str = "resources",
    output_dir: Optional[str] = None,
    unique_col: Optional[str] = "url",
) -> str:
    """将记录列表无损导出为独立的 SQLite .db 数据库文件（默认导出格式）

    Args:
        records: 字典记录列表
        filename: 导出文件名（如 'fanhao_export_20260826.db'，若无 .db 后缀会自动补充）
        table_name: 数据表名，默认 'resources'
        output_dir: 指定导出目录，缺省时使用 get_export_dir()
        unique_col: 唯一索引字段，默认 'url'

    Returns:
        导出的数据库文件绝对路径
    """
    if not records:
        print("[-] 待导出的记录列表为空，未生成数据库文件。")
        return ""

    if not filename.lower().endswith(".db") and not filename.lower().endswith(".sqlite"):
        filename = f"{filename}.db"

    out_dir = output_dir or get_export_dir()
    filepath = filename if os.path.isabs(filename) else os.path.join(out_dir, filename)

    if os.path.exists(filepath):
        backup_db(filepath, prefix_tag="export_overwrite")

    # 提取字段列表（过滤内部下划线私有字段）
    all_keys = []
    for r in records:
        for k in r.keys():
            if not k.startswith("_") and k not in all_keys:
                all_keys.append(k)

    if "id" not in all_keys:
        col_defs = ["id INTEGER PRIMARY KEY AUTOINCREMENT"] + [f"{col} TEXT" for col in all_keys]
    else:
        col_defs = [f"{col} TEXT" if col != "id" else "id INTEGER PRIMARY KEY" for col in all_keys]

    conn = sqlite3.connect(filepath)
    cursor = conn.cursor()
    cursor.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(col_defs)})")

    if unique_col and unique_col in all_keys:
        cursor.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table_name}_{unique_col} ON {table_name}({unique_col})")

    col_list = ", ".join(all_keys)
    placeholders = ", ".join(["?" for _ in all_keys])
    sql = f"INSERT OR IGNORE INTO {table_name} ({col_list}) VALUES ({placeholders})"

    rows_data = []
    for r in records:
        rows_data.append([r.get(k) for k in all_keys])

    cursor.executemany(sql, rows_data)
    conn.commit()
    conn.close()

    print(f"[+] 数据库导出完成: {filepath} (共 {len(records)} 条记录)")
    return filepath


def export_to_csv(
    records: List[Dict[str, Any]],
    filename: str,
    fieldnames: Optional[List[str]] = None,
    header_map: Optional[Dict[str, str]] = None,
    output_dir: Optional[str] = None,
) -> str:
    """将记录导出为带 UTF-8 BOM 的 CSV 审计文件

    Args:
        records: 字典记录列表
        filename: 导出文件名（若无 .csv 后缀会自动补充）
        fieldnames: 输出字段顺序，缺省时自动提取
        header_map: 英文键名到中文列名的映射字典（可选）
        output_dir: 指定导出目录，缺省时使用 get_export_dir()

    Returns:
        导出的 CSV 文件绝对路径
    """
    if not records:
        print("[-] 待导出的记录列表为空，未生成 CSV 文件。")
        return ""

    if not filename.lower().endswith(".csv"):
        filename = f"{filename}.csv"

    out_dir = output_dir or get_export_dir()
    filepath = filename if os.path.isabs(filename) else os.path.join(out_dir, filename)

    if fieldnames is None:
        raw_keys = []
        for r in records:
            for k in r.keys():
                if not k.startswith("_") and k not in raw_keys:
                    raw_keys.append(k)
        fieldnames = raw_keys

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        if header_map:
            headers = [header_map.get(k, k) for k in fieldnames]
            writer = csv.writer(f)
            writer.writerow(headers)
            for rec in records:
                writer.writerow([rec.get(k, "") for k in fieldnames])
        else:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for rec in records:
                clean = {k: v for k, v in rec.items() if not k.startswith("_")}
                writer.writerow(clean)

    print(f"[+] CSV 审计表导出完成: {filepath} (共 {len(records)} 条记录)")
    return filepath


def export_to_json(data: Any, filename: str, output_dir: Optional[str] = None) -> str:
    """将数据导出为格式化的 JSON 文件"""
    if not filename.lower().endswith(".json"):
        filename = f"{filename}.json"

    out_dir = output_dir or get_export_dir()
    filepath = filename if os.path.isabs(filename) else os.path.join(out_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[+] JSON 文件导出完成: {filepath}")
    return filepath


# ===================================================================
# 3. 级联数据安全与物理文件清理 (删除记录时强制删除 PDF)
# ===================================================================

def delete_records_cascade_pdf(
    conn: sqlite3.Connection,
    record_ids: Sequence[Union[int, str]],
    table_name: str = "resources",
    project_root: Optional[str] = None,
) -> Tuple[int, int, int, int]:
    """批量删除数据库记录，并【强制同步级联删除】对应的物理 PDF 文件

    Args:
        conn: 数据库连接
        record_ids: 要删除的记录 ID 列表
        table_name: 表名，默认 'resources'
        project_root: 项目根目录

    Returns:
        (deleted_records, deleted_pdfs, failed_pdfs, freed_bytes)
    """
    if not record_ids:
        return 0, 0, 0, 0

    root = project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cursor = conn.cursor()

    # 1. 查询这些记录对应的 pdf_path
    id_list = [str(i) for i in record_ids]
    pdf_paths_to_delete: List[str] = []
    seen_pdf_keys = set()

    batch_size = 500
    for i in range(0, len(id_list), batch_size):
        batch = id_list[i : i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        cursor.execute(
            f"SELECT pdf_path FROM {table_name} WHERE id IN ({placeholders}) AND pdf_path IS NOT NULL AND pdf_path != ''",
            batch,
        )
        for (p,) in cursor.fetchall():
            if not p:
                continue
            abs_p = resolve_pdf_path(p, root)
            norm_key = abs_p.lower().replace("\\", "/")
            if norm_key not in seen_pdf_keys:
                seen_pdf_keys.add(norm_key)
                if os.path.exists(abs_p):
                    pdf_paths_to_delete.append(abs_p)

    # 2. 物理删除 PDF 文件
    deleted_pdfs = 0
    failed_pdfs = 0
    freed_bytes = 0

    for pdf_path in pdf_paths_to_delete:
        try:
            sz = os.path.getsize(pdf_path)
            os.remove(pdf_path)
            deleted_pdfs += 1
            freed_bytes += sz
        except Exception as e:
            failed_pdfs += 1
            print(f"  [-] 级联删除物理 PDF 失败 {pdf_path}: {e}")

    # 3. 批量删除数据库记录
    total_deleted_records = 0
    for i in range(0, len(id_list), batch_size):
        batch = id_list[i : i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        cursor.execute(f"DELETE FROM {table_name} WHERE id IN ({placeholders})", batch)
        total_deleted_records += cursor.rowcount

    conn.commit()
    return total_deleted_records, deleted_pdfs, failed_pdfs, freed_bytes


def backup_db(db_path: str, prefix_tag: str = "") -> str:
    """正式执行写入前备份 SQLite 数据库

    Args:
        db_path: 数据库文件路径
        prefix_tag: 备份文件名可选标记

    Returns:
        备份文件的完整路径
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"_{prefix_tag}" if prefix_tag else ""
    backup_path = f"{db_path}.bak{tag}_{timestamp}"
    try:
        shutil.copy2(db_path, backup_path)
        print(f"[+] 数据库备份成功: {backup_path}")
        return backup_path
    except Exception as e:
        print(f"[-] 备份数据库失败: {e}")
        raise


def format_size(num_bytes: float) -> str:
    """将字节数格式化为易读的大小字符串"""
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / (1024 ** 3):.2f} GB"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / (1024 ** 2):.2f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes:.0f} B"


def vacuum_db(conn: sqlite3.Connection) -> None:
    """执行 VACUUM 释放已删除记录占用的数据库磁盘空间"""
    print("[*] 正在执行 VACUUM 压缩数据库以回收磁盘空间...")
    try:
        conn.commit()
    except Exception:
        pass
    old_iso = conn.isolation_level
    try:
        conn.isolation_level = None
        conn.execute("VACUUM")
        print("[+] 数据库压缩完成！")
    except Exception as e:
        print(f"[-] VACUUM 执行失败: {e}")
    finally:
        try:
            conn.isolation_level = old_iso
        except Exception:
            pass


# ===================================================================
# 4. 统一控制台 UI、提示符与交互循环辅助函数
# ===================================================================

def print_banner(title: str, width: int = 60) -> None:
    """打印标准风格的主标题 Banner"""
    print("\n" + "=" * width)
    print(title.center(width))
    print("=" * width)


def print_section(title: str, width: int = 60) -> None:
    """打印标准风格的子分节标题"""
    print("\n" + "─" * width)
    print(f"  {title}")
    print("─" * width)


def print_step(msg: str) -> None:
    """打印步骤/常规信息"""
    print(f"[*] {msg}")


def print_success(msg: str) -> None:
    """打印成功状态信息"""
    print(f"[+] {msg}")


def print_warning(msg: str) -> None:
    """打印警告信息"""
    print(f"[!] {msg}")


def print_error(msg: str) -> None:
    """打印错误信息"""
    print(f"[-] {msg}")


def confirm_action(prompt: str, default: bool = False) -> bool:
    """统一的交互式确认提示

    Args:
        prompt: 提示文本
        default: 回车时的默认值 (True 为 Y, False 为 N)

    Returns:
        用户是否确认 (True/False)
    """
    tag = "[Y/n]" if default else "[y/N]"
    full_prompt = f"{prompt} {tag}: "
    try:
        ans = input(full_prompt).strip().lower()
        if not ans:
            return default
        return ans in ("y", "yes", "true", "1")
    except (KeyboardInterrupt, EOFError):
        print()
        return False


def pause_for_user(msg: str = "按回车键继续...") -> None:
    """暂停并等待用户回车，防止控制台直接退出"""
    try:
        input(f"\n{msg}")
    except (KeyboardInterrupt, EOFError):
        print()