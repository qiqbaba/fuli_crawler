import sqlite3
import threading

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
        
        # 爬虫断点续爬状态表（按 source + class_name 分别记录）
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS crawl_state (
                source TEXT NOT NULL,
                class_name TEXT NOT NULL,
                page_num INTEGER NOT NULL DEFAULT 1,
                completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (source, class_name)
            )
        ''')
        
        self.conn.commit()

    def check_url_exists(self, url):
        """检查 URL 是否已存在于数据库"""
        with self.lock:
            self.cursor.execute("SELECT 1 FROM resources WHERE url = ?", (url,))
            return self.cursor.fetchone() is not None

    def filter_existing_urls(self, urls):
        """批量检查哪些 URL 已存在于数据库中，返回已存在的 URL 集合"""
        if not urls:
            return set()
        with self.lock:
            existing = set()
            urls_list = list(urls)
            for i in range(0, len(urls_list), 100):
                chunk = urls_list[i:i+100]
                placeholders = ",".join(["?"] * len(chunk))
                self.cursor.execute(f"SELECT url FROM resources WHERE url IN ({placeholders})", chunk)
                for row in self.cursor.fetchall():
                    existing.add(row[0])
            return existing

    def filter_existing_resource_links(self, resource_links):
        """批量检查哪些 resource_link 已存在于数据库中，返回已存在的 resource_link 集合"""
        if not resource_links:
            return set()
        with self.lock:
            existing = set()
            links_list = list(resource_links)
            for i in range(0, len(links_list), 100):
                chunk = links_list[i:i+100]
                placeholders = ",".join(["?"] * len(chunk))
                self.cursor.execute(f"SELECT resource_link FROM resources WHERE resource_link IN ({placeholders})", chunk)
                for row in self.cursor.fetchall():
                    existing.add(row[0])
            return existing

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
            self.conn.commit()  # 立即提交，防止崩溃丢数据
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

    def filter_existing_urls(self, urls):
        """批量检查哪些 URL 已存在于 Supabase 表中，返回已存在的 URL 集合"""
        if not urls:
            return set()
        existing = set()
        urls_list = list(urls)
        try:
            for i in range(0, len(urls_list), 100):
                chunk = urls_list[i:i+100]
                resp = (
                    self.client.table(self.table)
                    .select("url")
                    .in_("url", chunk)
                    .execute()
                )
                if resp.data:
                    for row in resp.data:
                        existing.add(row["url"])
        except Exception as e:
            print(f"[-] Supabase filter_existing_urls 失败: {e}")
        return existing

    def filter_existing_resource_links(self, resource_links):
        """批量检查哪些 resource_link 已存在于 Supabase 表中，返回已存在的 resource_link 集合"""
        if not resource_links:
            return set()
        existing = set()
        links_list = list(resource_links)
        try:
            for i in range(0, len(links_list), 100):
                chunk = links_list[i:i+100]
                resp = (
                    self.client.table(self.table)
                    .select("resource_link")
                    .in_("resource_link", chunk)
                    .execute()
                )
                if resp.data:
                    for row in resp.data:
                        existing.add(row["resource_link"])
        except Exception as e:
            print(f"[-] Supabase filter_existing_resource_links 失败: {e}")
        return existing

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
        pass

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
            # 注意: 使用 text/plain 发送原始 SQL 文本
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "text/plain"
            }
            resp = requests.post(
                f"{self.supabase_url}/rest/v1/sql",
                data=create_sql.strip(),
                headers=headers,
                timeout=15
            )
            if resp.status_code in (200, 201):
                print("[+] crawl_state 表已自动创建")
                # 通知 PostgREST 重新加载 schema cache
                self._reload_schema_cache()
                return
            else:
                # 如果 text/plain 失败，尝试以 JSON 格式重试
                headers["Content-Type"] = "application/json"
                resp2 = requests.post(
                    f"{self.supabase_url}/rest/v1/sql",
                    json={"query": create_sql.strip()},
                    headers=headers,
                    timeout=15
                )
                if resp2.status_code in (200, 201):
                    print("[+] crawl_state 表已自动创建")
                    self._reload_schema_cache()
                    return
                else:
                    print(f"[*] 建表请求返回状态码 {resp2.status_code}: {resp2.text}")
        except Exception as exc:
            print(f"[*] 建表请求异常: {exc}")

        print("[!] 无法自动创建 crawl_state 表，请在 Supabase Dashboard -> SQL Editor 中执行：")
        print(create_sql)

    def _reload_schema_cache(self):
        """通知 PostgREST 重新加载 schema cache，使新创建的表立即可见"""
        import requests
        # 方式1: 通过 /rest/v1/rpc/pgrst_reload_schema RPC
        try:
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "application/json"
            }
            resp = requests.post(
                f"{self.supabase_url}/rest/v1/rpc/pgrst_reload_schema",
                headers=headers,
                timeout=10
            )
            if resp.status_code in (200, 201, 204):
                print("[+] PostgREST schema cache 已刷新 (via rpc)")
                return
        except Exception:
            pass
        # 方式2: 通过 SQL 通知 PostgREST 重新加载 schema
        try:
            headers = {
                "apikey": self.supabase_key,
                "Authorization": f"Bearer {self.supabase_key}",
                "Content-Type": "text/plain"
            }
            resp = requests.post(
                f"{self.supabase_url}/rest/v1/sql",
                data="NOTIFY pgrst, 'reload schema'",
                headers=headers,
                timeout=10
            )
            if resp.status_code in (200, 201, 204):
                print("[+] PostgREST schema cache 已刷新 (via NOTIFY)")
                return
        except Exception:
            pass
        # 方式3: 等待 3 秒让 schema cache 自动过期
        import time
        print("[*] 等待 3 秒让 PostgREST schema cache 自动刷新...")
        time.sleep(3)

    def save_crawl_state(self, source, class_name, page_num, completed=False):
        """保存爬虫断点状态到 Supabase（upsert），按 source + class_name 分别记录"""
        from datetime import datetime, timezone

        try:
            record = {
                "source": source,
                "class_name": class_name,
                "page_num": page_num,
                "completed": completed,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            self.client.table("crawl_state").upsert(record, on_conflict="source,class_name").execute()
        except Exception as e:
            # 如果报错是表不存在，则自动建表并重试一次
            err_str = str(e)
            if "crawl_state" in err_str and ("PGRST205" in err_str or "relation" in err_str.lower() or "does not exist" in err_str.lower()):
                print("[*] 检测到 crawl_state 表不存在，尝试自动创建...")
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
