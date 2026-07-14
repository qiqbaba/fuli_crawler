import sqlite3
import threading
from abc import ABC, abstractmethod
from utils.deduplication import DynamoDBDeduplicationService
from utils.persistence import SqlitePersistenceService, SupabasePersistenceService
from utils.crawl_state import SqliteCrawlStateService, SupabaseCrawlStateService


class BaseDBManager(ABC):
    """数据库管理器抽象基类，封装 AWS DynamoDB 去重与状态管理的公共逻辑"""

    def check_url_exists(self, url):
        """去重检查：单条 URL 是否已存在于 AWS DynamoDB 中"""
        return self.aws_helper.check_url_exists(url)

    def filter_existing_urls(self, urls):
        """去重检查：批量检查哪些 URL 已存在于 AWS DynamoDB 中"""
        return self.aws_helper.filter_existing_urls(urls)

    def filter_existing_resource_links(self, resource_links):
        """去重检查：批量检查哪些 resource_link 已存在于 AWS DynamoDB 中"""
        return self.aws_helper.filter_existing_resource_links(resource_links)

    @abstractmethod
    def insert_resource(self, data):
        """持久化写入并同步去重标记"""
        ...

    def commit(self):
        """提交事务（不同后端实现不同）"""
        self.persistence.commit()

    def close(self):
        """关闭连接，清理去重服务后台线程池"""
        if hasattr(self, 'aws_helper'):
            self.aws_helper.shutdown()
        self.persistence.close()

    # ========== 爬虫断点续爬状态管理 ==========

    def save_crawl_state(self, source, class_name, page_num, completed=False):
        self.state_service.save_crawl_state(source, class_name, page_num, completed)

    def load_crawl_state(self, source):
        return self.state_service.load_crawl_state(source)

    def clear_crawl_state(self, source):
        self.state_service.clear_crawl_state(source)

    def mark_source_completed(self, source):
        self.state_service.mark_source_completed(source)

    def is_source_completed(self, source):
        return self.state_service.is_source_completed(source)


class DBManager(BaseDBManager):
    """本地 SQLite 数据库管理器（本地开发使用）"""
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        
        # 本地 SQLite 底层连接
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        
        # 初始化持久化与爬虫状态底层服务（复用 SQLite 连接和 Lock）
        self.persistence = SqlitePersistenceService(db_path, conn=self.conn, lock=self.lock)
        self.state_service = SqliteCrawlStateService(conn=self.conn, lock=self.lock)
        
        # 初始化去重服务
        try:
            self.aws_helper = DynamoDBDeduplicationService()
            print("[*] 本地 DBManager 已成功集成 AWS DynamoDB 比对源")
        except Exception as e:
            print(f"[-] 初始化 AWS DynamoDB 失败: {e}")
            print("[!] 请检查 AWS 凭证环境变量配置！")
            raise e

    @staticmethod
    def ensure_tables(db_path, cursor=None):
        """确保 SQLite 数据库结构完整（静态方法，支持外部直接调用）"""
        SqlitePersistenceService.ensure_tables(db_path, cursor)

    def insert_resource(self, data):
        """
        持久化与去重双写逻辑：
        1. 本地持久化写入
        2. 写入成功时，同步向 AWS DynamoDB 插入 URL 与资源链接做去重标记
        """
        inserted = self.persistence.insert_resource(data)
        if inserted:
            self.aws_helper.insert_resource(data.get('url'), data.get('resource_link'))
            return True
        return False


class SupabaseDBManager(BaseDBManager):
    """Supabase PostgreSQL 数据库管理器（云端 GitHub Actions 使用）"""

    def __init__(self, supabase_url, supabase_key):
        from supabase import create_client
        from urllib.parse import urlparse
        
        self.supabase_url = supabase_url.strip()
        self.supabase_key = supabase_key
        
        # 清洗 URL
        parsed = urlparse(self.supabase_url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}"
        
        self.client = create_client(clean_url, supabase_key)
        print(f"[*] 已连接 Supabase: {clean_url}")
        
        # 初始化持久化与爬虫状态服务
        self.persistence = SupabasePersistenceService(self.client)
        self.state_service = SupabaseCrawlStateService(self.client, self.supabase_url, self.supabase_key)
        
        # 初始化去重服务
        try:
            self.aws_helper = DynamoDBDeduplicationService()
            print("[*] 云端 SupabaseDBManager 已成功集成 AWS DynamoDB 比对源")
        except Exception as e:
            print(f"[-] 初始化 AWS DynamoDB 失败: {e}")
            print("[!] 请检查 AWS 凭证环境变量配置！")
            raise e

    def insert_resource(self, data):
        """
        持久化与去重双写逻辑：
        1. 云端 Supabase 持久化写入
        2. 写入成功时，同步向 AWS DynamoDB 插入 URL 与资源链接做去重标记
        """
        result = self.persistence.insert_resource(data)
        if result is True:
            self.aws_helper.insert_resource(data.get('url'), data.get('resource_link'))
            return True
        return result
