"""sync - 多平台云端同步与统计工具包

包含以下核心云端同步与统计模块：
  - dynamodb_sync.py: AWS DynamoDB 增量比对上传与相对路径去重键/Bloom Filter 同步
  - supabase_sync.py: Supabase 云端记录分页拉取合并至 SQLite 及云端清理归档
  - r2_sync.py: Cloudflare R2 对象存储 PDF 多线程下载、断点续传与批量删除管理
  - stats.py: Supabase / Cloudflare R2 / AWS DynamoDB 跨平台数据量与大小统计查询
  - export_urls_magnets.py: 本地数据库 URL 与磁力链接导出为独立轻量库
"""

from sync.stats import query_supabase, query_r2, query_dynamodb
from sync.r2_sync import get_r2_client
from sync.dynamodb_sync import get_dynamodb_client
from sync.supabase_sync import get_supabase_client

__all__ = [
    "query_supabase",
    "query_r2",
    "query_dynamodb",
    "get_r2_client",
    "get_dynamodb_client",
    "get_supabase_client",
]
