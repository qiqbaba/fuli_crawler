"""fixes/ 目录下的数据库操作公共工具函数

提供 fixes/ 中各脚本通用的 sys.path 引导、控制台配置、SQLite 连接和列检测功能。
"""

import os
import sys
import sqlite3
from typing import List, Optional


def setup_fixes_module() -> None:
    """将项目根目录加入 sys.path 并配置 UTF-8 控制台

    供 fixes/ 下各脚本在模块顶部调用，替代重复的:
        sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from utils import setup_console_utf8
        setup_console_utf8()
    """
    # 将项目根目录加入 sys.path
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    # 配置 UTF-8 控制台
    from utils import setup_console_utf8  # noqa: E402
    setup_console_utf8()


def get_connection(db_path: str) -> sqlite3.Connection:
    """打开 SQLite 数据库连接"""
    return sqlite3.connect(db_path)


def get_columns(cursor: sqlite3.Cursor) -> List[str]:
    """获取 resources 表所有列名"""
    cursor.execute("PRAGMA table_info(resources)")
    return [row[1] for row in cursor.fetchall()]


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