import sqlite3

class DBManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, isolation_level=None)
        self.cursor = self.conn.cursor()
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
                # 列已存在，忽略
                pass
                
        self.cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_url ON resources(url)")

    def check_url_exists(self, url):
        """检查 URL 是否已存在于数据库"""
        self.cursor.execute("SELECT 1 FROM resources WHERE url = ?", (url,))
        return self.cursor.fetchone() is not None

    def insert_resource(self, data):
        """
        向数据库写入数据字典
        返回：
            True: 写入成功
            False: 因为已存在被 IGNORE
        """
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
        """手动提交（防止部分操作需要手动提交）"""
        self.conn.commit()

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            try:
                self.conn.commit()
            except:
                pass
            self.conn.close()
