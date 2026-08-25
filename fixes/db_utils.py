"""fixes/ 目录下的数据库与维护操作公共工具函数

提供 fixes/ 中各脚本通用的 sys.path 引导、控制台配置、SQLite 连接、列检测、备份及路径解析等功能。
"""

import os
import sys
import shutil
import sqlite3
from datetime import datetime
from typing import List, Optional, Tuple


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
    cursor = conn.cursor()
    cursor.execute("VACUUM")
    print("[+] 数据库压缩完成！")