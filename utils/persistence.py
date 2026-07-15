import sqlite3
import threading
from utils.logger import get_logger

logger = get_logger(__name__)

class SqlitePersistenceService:
    """本地 SQLite 数据持久化服务"""
    def __init__(self, db_path, conn=None, lock=None):
        self.db_path = db_path
        self.lock = lock or threading.Lock()
        
        if conn is None:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self._owns_conn = True
        else:
            self.conn = conn
            self._owns_conn = False
            
        self.cursor = self.conn.cursor()
        self.init_db()

    def init_db(self):
        """初始化 SQLite 数据库与表结构"""
        self.ensure_tables(self.db_path, self.cursor)
        
    @staticmethod
    def ensure_tables(db_path, cursor=None):
        """
        确保数据库表结构完整（静态方法，无需创建完整实例即可调用）
        """
        if cursor is None:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            close_after = True
        else:
            conn = cursor.connection
            close_after = False
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                publish_time TEXT,
                category TEXT,
                resource_link TEXT NOT NULL,
                pikpak_link TEXT,
                size TEXT,
                resource_format TEXT,
                link_type TEXT,
                url TEXT,
                pdf_path TEXT,
                source TEXT
            )
        ''')
        
        # 兼容性升级逻辑
        columns_to_add = [
            ("pdf_path", "TEXT"),
            ("source", "TEXT"),
            ("pikpak_link", "TEXT"),
            ("size", "TEXT"),
            ("resource_format", "TEXT")
        ]
        
        cursor.execute("PRAGMA table_info(resources)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        
        for col_name, col_type in columns_to_add:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE resources ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
                
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_url ON resources(url)")
        
        # 爬虫断点续爬状态表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS crawl_state (
                source TEXT NOT NULL,
                class_name TEXT NOT NULL,
                page_num INTEGER NOT NULL DEFAULT 1,
                completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (source, class_name)
            )
        ''')
        
        conn.commit()
        if close_after:
            conn.close()

    def insert_resource(self, data):
        """
        向 SQLite 数据库写入数据字典
        返回：
            True: 写入成功
            False: 因为已存在被 IGNORE
        """
        with self.lock:
            self.cursor.execute('''
                INSERT OR IGNORE INTO resources (
                    title, publish_time, category, resource_link, pikpak_link, 
                    size, resource_format, link_type, url, pdf_path, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data.get('title'),
                data.get('publish_time'),
                data.get('category'),
                data.get('resource_link'),
                data.get('pikpak_link'),
                data.get('size'),
                data.get('resource_format'),
                data.get('link_type', ''),
                data.get('url'),
                data.get('pdf_path', ''),
                data.get('source')
            ))
            
            return self.cursor.rowcount > 0

    def commit(self):
        """手动提交"""
        with self.lock:
            self.conn.commit()

    def rollback(self):
        """手动回滚未提交的事务"""
        with self.lock:
            self.conn.rollback()

    def close(self):
        """关闭数据库连接"""
        with self.lock:
            if self.conn and self._owns_conn:
                try:
                    self.conn.commit()
                except Exception as e:
                    logger.error("关闭连接时提交事务失败: %s", e)
                self.conn.close()


class SupabasePersistenceService:
    """Supabase 云端数据持久化服务"""
    def __init__(self, client):
        self.client = client
        self.table = "resources"

    def insert_resource(self, data):
        """
        向 Supabase 表写入数据字典（upsert，url 为唯一键）
        返回：
            True: 写入成功（新记录）
            False: 已存在（被 ignore）
            None: 写入失败（网络/API 错误）
        """
        record = {
            "title":           data.get("title"),
            "publish_time":    data.get("publish_time"),
            "category":        data.get("category"),
            "resource_link":   data.get("resource_link"),
            "pikpak_link":     data.get("pikpak_link"),
            "size":            data.get("size"),
            "resource_format": data.get("resource_format"),
            "link_type":       data.get("link_type", ""),
            "url":             data.get("url"),
            "pdf_path":        data.get("pdf_path", ""),
            "source":          data.get("source"),
        }
        try:
            resp = (
                self.client.table(self.table)
                .upsert(record, on_conflict="url", ignore_duplicates=True)
                .execute()
            )
            if resp.data:
                return True
            return False
        except Exception as e:
            logger.error("[-] Supabase insert_resource 失败 (网络/API 错误): %s", e)
            raise

    def commit(self):
        """Supabase 自动提交，此处为空操作"""
        pass

    def rollback(self):
        """Supabase 自动提交，无需显式回滚"""
        pass

    def close(self):
        """Supabase HTTP 无需显式关闭，此处为空操作"""
        pass
