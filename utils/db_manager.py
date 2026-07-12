import sqlite3
import threading
import os
import time
import boto3
from botocore.exceptions import ClientError

class DBManager:
    """本地 SQLite 数据库管理器（本地开发使用）"""
    def __init__(self, db_path):
        self.db_path = db_path
        # Bug 5 修复：使用默认隔离级别（DEFERRED），而非 isolation_level=None（autocommit）
        # 这样 commit() 调用才有实际意义，保证事务一致性
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.lock = threading.Lock()
        self.init_db()
        # 初始化 AWS DynamoDB Helper
        try:
            self.aws_helper = AWSDynamoDBHelper()
            print("[*] 本地 DBManager 已成功集成 AWS DynamoDB 比对源")
        except Exception as e:
            print(f"[-] 初始化 AWS DynamoDB 失败: {e}")
            print("[!] 请检查 AWS 凭证环境变量配置！")
            raise e

    def init_db(self):
        """初始化 SQLite 数据库与表结构，添加所需的所有扩展列"""
        self.ensure_tables(self.db_path, self.cursor)
        
    @staticmethod
    def ensure_tables(db_path, cursor=None):
        """
        确保数据库表结构完整（静态方法，无需创建完整 DBManager 实例即可调用）
        
        Args:
            db_path: 仅用于日志显示
            cursor: sqlite3 cursor 对象；若为 None 则内部创建临时连接
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
        
        # 兼容性升级逻辑：如果已有表缺少以下列，则动态添加
        columns_to_add = [
            ("pdf_path", "TEXT"),
            ("source", "TEXT"),
            ("pikpak_link", "TEXT"),
            ("size", "TEXT"),
            ("resource_format", "TEXT")
        ]
        
        # 先检查已有哪些列，避免每次连接都运行 ALTER TABLE
        cursor.execute("PRAGMA table_info(resources)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        
        for col_name, col_type in columns_to_add:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE resources ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass
                
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_url ON resources(url)")
        
        # 爬虫断点续爬状态表（按 source + class_name 分别记录）
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

    def check_url_exists(self, url):
        """检查 URL 是否已存在于数据库 (修改为比对 AWS DynamoDB)"""
        return self.aws_helper.check_url_exists(url)

    def filter_existing_urls(self, urls):
        """批量检查哪些 URL 已存在于数据库中，返回已存在的 URL 集合 (修改为比对 AWS DynamoDB)"""
        return self.aws_helper.filter_existing_urls(urls)

    def filter_existing_resource_links(self, resource_links):
        """批量检查哪些 resource_link 已存在于数据库中，返回已存在的 resource_link 集合 (修改为比对 AWS DynamoDB)"""
        return self.aws_helper.filter_existing_resource_links(resource_links)

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
            
            # 本地数据库写入成功，异步同步写入到 AWS DynamoDB
            self.aws_helper.insert_resource(data.get('url'), data.get('resource_link'))
            return True

    def commit(self):
        """手动提交"""
        with self.lock:
            self.conn.commit()

    def close(self):
        """关闭数据库连接"""
        if hasattr(self, 'aws_helper'):
            self.aws_helper.shutdown()
        with self.lock:
            if self.conn:
                try:
                    self.conn.commit()
                except Exception as e:
                    print(f"[-] 关闭连接时提交事务失败: {e}")
                self.conn.close()

    # ========== 爬虫断点续爬状态管理 ==========

    def save_crawl_state(self, source, class_name, page_num, completed=False):
        """保存爬虫断点状态（upsert），按 source + class_name 分别记录"""
        with self.lock:
            self.cursor.execute('''
                INSERT INTO crawl_state (source, class_name, page_num, completed, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(source, class_name) DO UPDATE SET
                    page_num=excluded.page_num,
                    completed=excluded.completed,
                    updated_at=datetime('now')
            ''', (source, class_name, page_num, 1 if completed else 0))
            self.conn.commit()

    def load_crawl_state(self, source):
        """
        加载爬虫所有板块的断点状态，返回 dict {class_name: {"page_num": int, "completed": bool}}
        如果没有记录则返回空 dict
        """
        with self.lock:
            self.cursor.execute(
                "SELECT class_name, page_num, completed FROM crawl_state WHERE source = ?",
                (source,)
            )
            rows = self.cursor.fetchall()
            result = {}
            for row in rows:
                result[row[0]] = {
                    "page_num": row[1],
                    "completed": bool(row[2])
                }
            return result

    def clear_crawl_state(self, source):
        """清除爬虫所有断点状态"""
        with self.lock:
            self.cursor.execute("DELETE FROM crawl_state WHERE source = ?", (source,))
            self.conn.commit()

    def mark_source_completed(self, source):
        """将指定爬虫标记为完全完成（在所有板块都完成后调用）"""
        with self.lock:
            self.cursor.execute('''
                INSERT INTO crawl_state (source, class_name, page_num, completed, updated_at)
                VALUES (?, '__all__', 0, 1, datetime('now'))
                ON CONFLICT(source, class_name) DO UPDATE SET
                    completed=1,
                    updated_at=datetime('now')
            ''', (source,))
            self.conn.commit()

    def is_source_completed(self, source):
        """检查爬虫是否已全部完成"""
        with self.lock:
            self.cursor.execute(
                "SELECT completed FROM crawl_state WHERE source = ? AND class_name = '__all__'",
                (source,)
            )
            row = self.cursor.fetchone()
            return row is not None and bool(row[0])


class SupabaseDBManager:
    """Supabase PostgreSQL 数据库管理器（云端 GitHub Actions 使用）"""

    def __init__(self, supabase_url, supabase_key):
        from supabase import create_client
        from urllib.parse import urlparse
        
        self.supabase_url = supabase_url.strip()
        self.supabase_key = supabase_key
        
        # 清洗 URL，防止因带有 /rest/v1 等路径或末尾斜杠导致 PostgREST (PGRST125) 404 错误
        parsed = urlparse(self.supabase_url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}"
        
        self.client = create_client(clean_url, supabase_key)
        self.table = "resources"
        print(f"[*] 已连接 Supabase: {clean_url}")
        
        # 初始化 AWS DynamoDB Helper
        try:
            self.aws_helper = AWSDynamoDBHelper()
            print("[*] 云端 SupabaseDBManager 已成功集成 AWS DynamoDB 比对源")
        except Exception as e:
            print(f"[-] 初始化 AWS DynamoDB 失败: {e}")
            print("[!] 请检查 AWS 凭证环境变量配置！")
            raise e

    def check_url_exists(self, url):
        """检查 URL 是否已存在于 Supabase 表中 (修改为比对 AWS DynamoDB)"""
        return self.aws_helper.check_url_exists(url)

    def filter_existing_urls(self, urls):
        """批量检查哪些 URL 已存在于 Supabase 表中，返回已存在的 URL 集合 (修改为比对 AWS DynamoDB)"""
        return self.aws_helper.filter_existing_urls(urls)

    def filter_existing_resource_links(self, resource_links):
        """批量检查哪些 resource_link 已存在于 Supabase 表中，返回已存在的 resource_link 集合 (修改为比对 AWS DynamoDB)"""
        return self.aws_helper.filter_existing_resource_links(resource_links)

    def insert_resource(self, data):
        """
        向 Supabase 表写入数据字典（upsert，url 为唯一键）
        返回：
            True: 写入成功（新记录）
            False: 已存在（被 ignore）
            None: 写入失败（网络/API 错误），调用方应区分此值与 False
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
                # 写入 Supabase 成功，同步写入到 AWS DynamoDB
                self.aws_helper.insert_resource(data.get('url'), data.get('resource_link'))
                return True
            return False
        except Exception as e:
            print(f"[-] Supabase insert_resource 失败 (网络/API 错误): {e}")
            return None

    def commit(self):
        """兼容接口，Supabase 自动提交，此处为空操作"""
        pass

    def close(self):
        """兼容接口，Supabase HTTP 无需显式关闭"""
        if hasattr(self, 'aws_helper'):
            self.aws_helper.shutdown()

    # ========== 爬虫断点续爬状态管理 ==========

    def _ensure_crawl_state_table(self):
        """通过 Supabase REST API 的 /sql 端点执行建表语句"""
        import requests
        from datetime import datetime, timezone

        create_sql = """
        CREATE TABLE IF NOT EXISTS crawl_state (
            source TEXT NOT NULL,
            class_name TEXT NOT NULL,
            page_num INTEGER,
            completed BOOLEAN,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (source, class_name)
        );
        """
        try:
            # 使用 Supabase 的 /rest/v1/sql 端点执行原始 SQL
            resp = requests.post(
                f"{self.supabase_url}/rest/v1/sql",
                headers={
                    "apikey": self.supabase_key,
                    "Authorization": f"Bearer {self.supabase_key}",
                    "Content-Type": "application/json"
                },
                json={"query": create_sql},
                timeout=10
            )
            if resp.status_code == 200:
                # 刷新 PostgREST 架构缓存以使新表立即可见
                requests.post(
                    f"{self.supabase_url}/rest/v1/rpc/pgrst_reload_schema",
                    headers={
                        "apikey": self.supabase_key,
                        "Authorization": f"Bearer {self.supabase_key}"
                    },
                    timeout=5
                )
                print("[+] 自动创建 crawl_state 表及重新加载架构成功")
                return True
        except Exception as e:
            print(f"[-] 尝试自动创建 Supabase crawl_state 表失败: {e}")
        
        print("[!] 无法自动创建 crawl_state 表，请在 Supabase Dashboard -> SQL Editor 中执行：")
        print(create_sql)
        return False

    def save_crawl_state(self, source, class_name, page_num, completed=False):
        """保存爬虫断点状态到 Supabase（upsert），按 source + class_name 分别记录"""
        from datetime import datetime, timezone

        record = {
            "source": source,
            "class_name": class_name,
            "page_num": page_num,
            "completed": completed,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        try:
            self.client.table("crawl_state").upsert(record, on_conflict="source,class_name").execute()
        except Exception as e:
            # 可能是表不存在，尝试自动创建
            if "relation \"crawl_state\" does not exist" in str(e):
                print("[*] crawl_state 表不存在，正在尝试自动创建...")
                self._ensure_crawl_state_table()
                # 重试一次
                try:
                    self.client.table("crawl_state").upsert(record, on_conflict="source,class_name").execute()
                    print("[+] 自动创建 crawl_state 表成功，状态已保存")
                    return
                except Exception as e2:
                    print(f"[-] Supabase save_crawl_state 重试仍失败: {e2}")
            else:
                print(f"[-] Supabase save_crawl_state 失败: {e}")

    def load_crawl_state(self, source):
        """
        从 Supabase 加载爬虫所有板块的断点状态
        返回 dict {class_name: {"page_num": int, "completed": bool}}
        """
        try:
            resp = (
                self.client.table("crawl_state")
                .select("class_name, page_num, completed")
                .eq("source", source)
                .execute()
            )
            result = {}
            if resp.data:
                for row in resp.data:
                    if row["class_name"] == "__all__":
                        continue
                    result[row["class_name"]] = {
                        "page_num": row["page_num"],
                        "completed": bool(row["completed"])
                    }
            return result
        except Exception as e:
            print(f"[-] Supabase load_crawl_state 失败: {e}")
            return {}

    def clear_crawl_state(self, source):
        """清除 Supabase 中的爬虫断点状态"""
        try:
            self.client.table("crawl_state").delete().eq("source", source).execute()
        except Exception as e:
            print(f"[-] Supabase clear_crawl_state 失败: {e}")

    def mark_source_completed(self, source):
        """将指定爬虫标记为完全完成"""
        from datetime import datetime, timezone

        try:
            record = {
                "source": source,
                "class_name": "__all__",
                "page_num": 0,
                "completed": True,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            self.client.table("crawl_state").upsert(record, on_conflict="source,class_name").execute()
        except Exception as e:
            print(f"[-] Supabase mark_source_completed 失败: {e}")

    def is_source_completed(self, source):
        """检查爬虫是否已全部完成"""
        try:
            resp = (
                self.client.table("crawl_state")
                .select("completed")
                .eq("source", source)
                .eq("class_name", "__all__")
                .eq("completed", True)
                .limit(1)
                .execute()
            )
            return len(resp.data) > 0
        except Exception as e:
            print(f"[-] Supabase is_source_completed 失败: {e}")
            return False


class AWSDynamoDBHelper:
    """AWS DynamoDB 数据库助手，用于比对重复项和保存资源"""
    def __init__(self):
        # 导入 config 以统一获取配置
        import config as _cfg
        self.aws_access_key_id = _cfg.AWS_ACCESS_KEY_ID
        self.aws_secret_access_key = _cfg.AWS_SECRET_ACCESS_KEY
        self.region_name = _cfg.AWS_REGION
        self.table_name = "fuli_resources"
        self.use_gsi = True
        self._lock = threading.Lock()          # 线程安全锁
        self._scanned_resource_links = None    # 扫描结果本地缓存
        self._cached_urls = set()              # 新插入的 URL 缓存
        self._cached_resource_links = set()    # 新插入的磁力链接缓存

        if not self.aws_access_key_id or not self.aws_secret_access_key:
            raise ValueError(
                "AWS 凭证未配置！请检查相关环境变量（AWS 标准凭证变量）是否设置正确。"
            )

        self.client = boto3.client(
            "dynamodb",
            region_name=self.region_name,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
        )
        self.ensure_table_exists()
        from concurrent.futures import ThreadPoolExecutor
        self._executor = ThreadPoolExecutor(max_workers=5)

    def ensure_table_exists(self):
        """确保 DynamoDB 表已存在，若不存在则创建"""
        try:
            existing_tables = self.client.list_tables()["TableNames"]
            if self.table_name in existing_tables:
                return

            print(f"[*] AWS DynamoDB 表 {self.table_name} 不存在，正在自动创建...")
            self.client.create_table(
                TableName=self.table_name,
                AttributeDefinitions=[{"AttributeName": "url", "AttributeType": "S"}],
                KeySchema=[{"AttributeName": "url", "KeyType": "HASH"}],
                BillingMode="PAY_PER_REQUEST"
            )
            # 等待表激活
            waiter = self.client.get_waiter("table_exists")
            waiter.wait(TableName=self.table_name)
            print(f"[+] AWS DynamoDB 表 {self.table_name} 创建成功！")
        except Exception as e:
            print(f"[-] 创建 AWS DynamoDB 表失败: {e}")
            raise

    def check_url_exists(self, url):
        """检查单条 URL 是否已存在于 AWS DynamoDB"""
        if not url:
            return False
        with self._lock:
            if url in self._cached_urls:
                return True
        try:
            response = self.client.get_item(
                TableName=self.table_name,
                Key={"url": {"S": url}},
                ProjectionExpression="#u",
                ExpressionAttributeNames={"#u": "url"}
            )
            return "Item" in response
        except Exception as e:
            print(f"[-] AWS DynamoDB check_url_exists 失败: {e}")
            return False

    def filter_existing_urls(self, urls):
        """批量检查哪些 URL 已存在于 AWS DynamoDB 中，返回已存在的 URL 集合"""
        if not urls:
            return set()
        existing = set()
        
        # 优先使用内存中刚刚成功写入的缓存判定（线程安全）
        urls_to_query = []
        with self._lock:
            for url in urls:
                if not url:
                    continue
                if url in self._cached_urls:
                    existing.add(url)
                else:
                    urls_to_query.append(url)
                
        if not urls_to_query:
            return existing

        urls_list = list(urls_to_query)
        # batch_get_item 每次最多获取 100 个
        for i in range(0, len(urls_list), 100):
            chunk = urls_list[i:i+100]
            try:
                request_items = {
                    self.table_name: {
                        "Keys": [{"url": {"S": url}} for url in chunk],
                        "ProjectionExpression": "#u",
                        "ExpressionAttributeNames": {"#u": "url"}
                    }
                }
                response = self.client.batch_get_item(RequestItems=request_items)
                
                # 处理已返回的 Items
                responses = response.get("Responses", {}).get(self.table_name, [])
                for item in responses:
                    url_val = item.get("url", {}).get("S")
                    if url_val:
                        existing.add(url_val)
                
                # 处理未处理完的 Keys
                unprocessed = response.get("UnprocessedKeys", {}).get(self.table_name, {})
                while unprocessed and "Keys" in unprocessed and unprocessed["Keys"]:
                    time.sleep(0.5)  # 退避重试
                    response = self.client.batch_get_item(RequestItems=unprocessed)
                    responses = response.get("Responses", {}).get(self.table_name, [])
                    for item in responses:
                        url_val = item.get("url", {}).get("S")
                        if url_val:
                            existing.add(url_val)
                    unprocessed = response.get("UnprocessedKeys", {}).get(self.table_name, {})
            except Exception as e:
                print(f"[-] AWS DynamoDB filter_existing_urls 失败: {e}")
        return existing

    def filter_existing_resource_links(self, resource_links):
        """批量检查哪些 resource_link 已存在于 AWS DynamoDB 中，返回已存在的 resource_link 集合"""
        if not resource_links:
            return set()
        
        valid_links = [l for l in resource_links if l]
        if not valid_links:
            return set()

        existing = set()

        # 优先比对本地内存中新写入的缓存磁力（线程安全）
        links_to_query = []
        with self._lock:
            for link in valid_links:
                if link in self._cached_resource_links:
                    existing.add(link)
                else:
                    links_to_query.append(link)

        if not links_to_query:
            return existing

        if self.use_gsi:
            try:
                for link in links_to_query:
                    response = self.client.query(
                        TableName=self.table_name,
                        IndexName="resource_link-index",
                        KeyConditionExpression="resource_link = :rl",
                        ExpressionAttributeValues={":rl": {"S": link}},
                        ProjectionExpression="resource_link"
                    )
                    if response.get("Items"):
                        existing.add(link)
                return existing
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code")
                error_msg = e.response.get("Error", {}).get("Message", "")
                if error_code == "ValidationException" and "index" in error_msg.lower():
                    print("[!] 检测到 AWS DynamoDB 表中未创建 resource_link-index 索引。")
                    print("[*] 正在回退到 Scan 缓存兼容模式。")
                    print("[*] 为了更好的性能，建议您在 AWS DynamoDB 控制台中为表 fuli_resources 创建二级索引（分区键: resource_link, 索引名: resource_link-index）。")
                    self.use_gsi = False
                else:
                    print(f"[-] AWS DynamoDB query GSI 失败: {e}")
                    return existing
            except Exception as e:
                print(f"[-] AWS DynamoDB query GSI 失败: {e}")
                return existing

        # 回退到 Scan 扫描缓存模式（仅首次扫描全表获取所有 resource_link）
        if self._scanned_resource_links is None:
            print("[*] 正在执行首次 AWS DynamoDB 全表扫描以同步磁力链接缓存...")
            with self._lock:
                if self._scanned_resource_links is None:  # 双重检查锁定
                    self._scanned_resource_links = self.get_all_resource_links_by_scan()
                    print(f"[+] 首次扫描缓存同步完成，已加载 {len(self._scanned_resource_links)} 条磁力链接。")

        with self._lock:
            for link in links_to_query:
                if link in self._scanned_resource_links:
                    existing.add(link)
        return existing

    def get_all_resource_links_by_scan(self):
        """全表扫描获取所有的 resource_link 集合（无索引时的兼容模式）"""
        existing_links = set()
        last_evaluated_key = None
        page_count = 0
        while True:
            kwargs = {
                "TableName": self.table_name,
                "ProjectionExpression": "resource_link",
            }
            if last_evaluated_key:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            try:
                response = self.client.scan(**kwargs)
                page_count += 1
                items = response.get("Items", [])
                for item in items:
                    link_val = item.get("resource_link", {})
                    if "S" in link_val and link_val["S"]:
                        existing_links.add(link_val["S"])
                
                if page_count % 5 == 0:
                    print(f"[*] 扫描进度: 已处理 {page_count} 页数据，当前缓存 {len(existing_links)} 条磁力链接...")

                last_evaluated_key = response.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break
            except Exception as e:
                print(f"[-] AWS DynamoDB Scan 失败: {e}")
                break
        return existing_links

    def insert_resource(self, url, resource_link):
        """向 AWS DynamoDB 异步写入一条数据"""
        if not url:
            return False
        
        # 立即更新本地内存缓存，防去重击穿（线程安全）
        with self._lock:
            self._cached_urls.add(url)
            if resource_link:
                self._cached_resource_links.add(resource_link)
                if self._scanned_resource_links is not None:
                    self._scanned_resource_links.add(resource_link)

        # 异步提交写入任务
        self._executor.submit(self._async_put_item, url, resource_link)
        return True

    def _async_put_item(self, url, resource_link):
        """实际在线程池中运行的 DynamoDB 写入任务"""
        item = {"url": {"S": url}}
        if resource_link:
            item["resource_link"] = {"S": resource_link}

        try:
            self.client.put_item(
                TableName=self.table_name,
                Item=item
            )
        except Exception as e:
            # 异步写入失败不应影响主流程，记录即可
            pass
            print(f"[-] AWS DynamoDB 异步写入记录失败 ({url}): {e}")

    def shutdown(self):
        """在爬虫关闭时清理后台线程池"""
        try:
            self._executor.shutdown(wait=True)
        except Exception as e:
            print(f"[-] AWSDynamoDBHelper shutdown 异常: {e}")
