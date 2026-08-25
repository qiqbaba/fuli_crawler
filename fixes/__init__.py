"""fixes - 数据库与文件维护修复工具包

包含以下核心维护模块：
  - pdf_maintenance.py: PDF 文件检查、路径规范、小文件重下、缺失重建、孤儿管理与缺失记录清理
  - data_cleaner.py: 链接广告/标签清洗、域名替换、Schema 升级与元数据提取、磁力大小补全与空链接回填
  - record_filter.py: URL/磁力多维查重、CSV 导出、去重批量清理、严格日本番号识别/导出/清理
  - db_utils.py: 共享数据库连接、字段探测、路径处理与备份公共工具
"""

from fixes.db_utils import (
    setup_fixes_module,
    get_connection,
    get_columns,
    get_total_count,
    get_db_path,
    resolve_pdf_path,
    backup_db,
    format_size,
    vacuum_db,
)

__all__ = [
    "setup_fixes_module",
    "get_connection",
    "get_columns",
    "get_total_count",
    "get_db_path",
    "resolve_pdf_path",
    "backup_db",
    "format_size",
    "vacuum_db",
]
