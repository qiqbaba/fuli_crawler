"""fixes - 本地数据库、记录与 PDF 文件维护修复工具包

本目录提供爬虫系统数据与文件的离线维护、修复、去重清洗与生命周期管理工具，包含以下核心模块：

1. pdf_maintenance.py: PDF 文件全生命周期维护合集
   - check-dates   : 扫描物理 PDF 与数据库发布日期比对，生成审计报告
   - fix-paths     : 将 Unknown_Year 及各年份下文件名日期不一致的 PDF 纠偏移动至正确年份目录
   - redownload    : 自动扫描小于 20KB 的损坏/空白 PDF 并重新抓取渲染覆盖
   - rebuild       : 检查数据库有效记录，将路径相对化并对物理缺失的 PDF 自动重新生成
   - orphan        : 扫描无数据库记录的多余 PDF 隔离至根目录，或将隔离文件还原归位
   - clean-missing : 清理数据库中对应物理 PDF 文件已丢失的残留脏记录

2. data_cleaner.py: 数据清洗与元数据修复工具合集
   - clean-noise       : 清洗 resource_link 中的残留广告、推广行与标签噪声（支持多云同步）
   - replace-domain    : 批量替换数据库中失效/过期的旧域名与 URL 镜像前缀
   - upgrade-db        : 自动升级 SQLite 表结构对齐标准 12 字段，并从历史数据提取 size/format/pikpak 元数据
   - fetch-sizes       : 并发调用 Darklyn API 批量补全磁力链接文件大小，支持看门狗自动重试入库
   - fetch-empty-links : 针对 resource_link 为空的记录，拉起 Playwright 重新访问页面解析回填

3. record_filter.py: 记录多维去重过滤与导出工具合集
   - duplicates : 按 URL / 磁力链接 / 标题+磁力组合进行多维查重，导出 CSV，批量去重并级联删除 PDF
   - fanhao     : 严格日本 AV 番号识别算法扫描、前缀分布统计、导出独立 SQLite 库或批量删除

4. db_utils.py: 共享数据库与维护基础设施公共库
   - 提供环境初始化、控制台编码配置、数据库连接、备份、表字段探测、相对路径转换及 VACUUM 压缩等公共函数
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
