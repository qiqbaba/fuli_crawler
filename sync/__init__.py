"""sync - 多平台云端同步、数据迁移与统计工具包

本目录提供爬虫系统与各云端基础设施（AWS DynamoDB / Supabase / Cloudflare R2）之间的数据同步、双向流转、去重键维护与用量统计工具，包含以下核心模块：

1. dynamodb_sync.py: AWS DynamoDB 去重与数据同步中心
   - upload    : 全量扫描本地 SQLite 与云端 DynamoDB，增量批量上传云端缺失的 URL 与磁力链接
   - sync-keys : 提取规范化相对路径去重键，同步构建本地 Bloom Filter 并批量写入 DynamoDB（支持 QPS 限速）

2. supabase_sync.py: Supabase (PostgreSQL) 云端数据同步与归档
   - 自动备份本地 SQLite，使用 ID 游标分页高效拉取 Supabase 云端数据并无损 INSERT OR IGNORE 合并
   - 支持在本地合并成功后安全分批清理云端已同步的历史数据，释放 Supabase 500MB 免费配额

3. r2_sync.py: Cloudflare R2 对象存储 PDF 同步与生命周期管理
   - 支持多线程并行列举、多线程并发下载 PDF、自动断点续传与实时速率/ETA 汇报
   - 支持本地副本比对，自动安全批量删除 R2 上已有本地副本的文件或指定年份/前缀的文件

4. stats.py: 跨平台多云存储与数据库用量统计监控
   - Supabase (PostgreSQL) : 统计总记录数、按采集来源分布、通过 RPC/估算查询表物理占用大小
   - Cloudflare R2 (S3)     : 递归扫描各年份前缀，统计 PDF 文件总数、总大小及按年份分布
   - AWS DynamoDB (NoSQL)   : 查询表状态、记录总数、表大小字节数及读写容量模式配置

5. export_urls_magnets.py: 本地数据库 URL 与磁力链接独立导出
   - 将本地 SQLite 中的 URL 与 magnet 磁力链接抽取并导出为结构精简的独立 SQLite 数据库 (D:\\urls_only.db)
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
