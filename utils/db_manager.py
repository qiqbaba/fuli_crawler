import sqlite3
import threading

class DBManager:
    """本地 SQLite 数据库管理器（本地开发使用）"""
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.cursor = self.conn.cursor()
        self.lock = threading.Lock()
        self.init_db()

    def init_db(self):
        """初始化 SQLite 数据库与表结构，添加所需的所有扩展列"""
        self.cursor.execute('''
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
        
        # 兼容性升级逻辑：如果已有表缺少以下列，则动态添加
        columns_to_add = [
            ("pdf_path", "TEXT"),
            ("source", "TEXT"),
            ("pikpak_link", "TEXT"),
            ("size", "TEXT"),
            ("resource_format", "TEXT")
        ]
        
        for col_name, col_type in columns_to_add:
            try:
                self.cursor.execute(f"ALTER TABLE resources ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass
                
        self.cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_url ON resources(url)")

    def check_url_exists(self, url):
        """检查 URL 是否已存在于数据库"""
        with self.lock:
            self.cursor.execute("SELECT 1 FROM resources WHERE url = ?", (url,))
            return self.cursor.fetchone() is not None

    def insert_resource(self, data):
        """
        向数据库写入数据字典
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
            
            if self.cursor.rowcount == 0:
                return False
            return True

    def commit(self):
        """手动提交"""
        with self.lock:
            self.conn.commit()

    def close(self):
        """关闭数据库连接"""
        with self.lock:
            if self.conn:
                try:
                    self.conn.commit()
                except:
                    pass
                self.conn.close()


class SupabaseDBManager:
    """Supabase PostgreSQL 数据库管理器（云端 GitHub Actions 使用）"""

    def __init__(self, supabase_url, supabase_key):
        from supabase import create_client
        from urllib.parse import urlparse
        
        # 清洗 URL，防止因带有 /rest/v1 等路径或末尾斜杠导致 PostgREST (PGRST125) 404 错误
        parsed = urlparse(supabase_url.strip())
        clean_url = f"{parsed.scheme}://{parsed.netloc}"
        
        self.client = create_client(clean_url, supabase_key)
        self.table = "resources"
        print(f"[*] 已连接 Supabase: {clean_url}")

    def check_url_exists(self, url):
        """检查 URL 是否已存在于 Supabase 表中"""
        try:
            resp = (
                self.client.table(self.table)
                .select("id")
                .eq("url", url)
                .limit(1)
                .execute()
            )
            return len(resp.data) > 0
        except Exception as e:
            print(f"[-] Supabase check_url_exists 失败: {e}")
            return False

    def insert_resource(self, data):
        """
        向 Supabase 表写入数据字典（upsert，url 为唯一键）
        返回：
            True: 写入成功（新记录）
            False: 已存在（被 ignore）或写入失败
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
            # 使用 upsert + ignore_duplicates=True 实现幂等插入
            resp = (
                self.client.table(self.table)
                .upsert(record, on_conflict="url", ignore_duplicates=True)
                .execute()
            )
            # 若 data 为空列表，表示已存在被忽略
            if resp.data:
                return True
            return False
        except Exception as e:
            print(f"[-] Supabase insert_resource 失败: {e}")
            return False

    def commit(self):
        """兼容接口，Supabase 自动提交，此处为空操作"""
        pass

    def close(self):
        """兼容接口，Supabase HTTP 无需显式关闭"""
        pass
