import sqlite3
import requests
from datetime import datetime, timezone

class SqliteCrawlStateService:
    """基于本地 SQLite 的爬虫断点状态管理服务"""
    def __init__(self, conn, lock):
        self.conn = conn
        self.cursor = conn.cursor()
        self.lock = lock

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


class SupabaseCrawlStateService:
    """基于 Supabase PostgreSQL 的爬虫断点状态管理服务"""
    def __init__(self, client, supabase_url, supabase_key):
        self.client = client
        self.supabase_url = supabase_url.strip()
        self.supabase_key = supabase_key

    def _ensure_crawl_state_table(self):
        """通过 Supabase REST API 的 /sql 端点执行建表语句"""
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
