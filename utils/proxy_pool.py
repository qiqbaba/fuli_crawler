"""
代理池管理模块
负责管理代理缓存、状态分发、评分机制以及按需补给
"""
import os
import time
import random
import sqlite3
import threading
from typing import List, Dict, Optional

# 本地导入
from utils.proxy_fetcher import ProxyFetcher
from utils.proxy_verifier import ProxyVerifier
from utils.logger import get_logger

logger = get_logger(__name__)

_PROXY_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_profiles")
_PROXY_CACHE_DB = os.path.join(_PROXY_CACHE_DIR, "proxy_cache.db")


class ProxyPool:
    """代理池核心管理器 - 负责存储、多线程分配、评分及缓存"""

    def __init__(self, cache_ttl: int = 43200):
        """
        初始化代理池
        
        Args:
            cache_ttl: 缓存有效期（秒），默认12小时 (43200秒)
        """
        self.cache_ttl = cache_ttl
        self._proxies: List[Dict[str, str]] = []  # [{"protocol": "http", "address": "ip:port", "source": "..."}]
        self._working_proxies: List[Dict[str, str]] = []
        self._lock = threading.RLock()
        self._last_fetch_time = 0
        self._last_verify_time = 0
        self._current_proxy_idx = 0
        self._thread_proxy_map: Dict[int, str] = {}  # thread_id -> proxy_url
        self._is_replenishing = False

        # 延迟写入缓存相关（优化：避免高频写入 SQLite）
        self._pending_save = False
        self._last_save_time = time.time()
        self._save_interval = 5.0  # 最小写入间隔（秒）
        
        # 初始化获取器与验证器
        self.fetcher = ProxyFetcher()
        self.verifier = ProxyVerifier()
        
        # 确保缓存目录存在
        os.makedirs(_PROXY_CACHE_DIR, exist_ok=True)
        
        # 清理过期的 .bak 备份文件
        self._cleanup_bak_files()
        
        # 初始化 SQLite 缓存数据库表结构
        self._init_cache_db()
        
        # 尝试从旧 JSON 缓存迁移到 SQLite
        self._migrate_from_json_cache()
        
        # 加载缓存
        self._load_cache()

    def _cleanup_bak_files(self):
        """清理 temp_profiles 目录下过期的 .bak 备份文件"""
        try:
            bak_dir = _PROXY_CACHE_DIR
            for fname in os.listdir(bak_dir):
                if fname.endswith(".bak"):
                    fpath = os.path.join(bak_dir, fname)
                    try:
                        os.remove(fpath)
                        logger.info("清理过期备份文件: %s", fname)
                    except Exception as e:
                        logger.warning("清理备份文件失败 %s: %s", fname, e)
        except Exception as e:
            logger.warning("扫描备份文件时出错: %s", e)

    def _init_cache_db(self):
        """初始化 SQLite 缓存数据库表结构，启用 WAL 模式以提升并发读写性能"""
        try:
            conn = sqlite3.connect(_PROXY_CACHE_DB, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL;")
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS proxy_cache (
                    protocol TEXT NOT NULL,
                    address TEXT NOT NULL,
                    source TEXT,
                    success_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    score REAL DEFAULT 0.0,
                    last_verified REAL DEFAULT 0,
                    PRIMARY KEY (protocol, address)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cache_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("初始化缓存数据库失败: %s", e)

    def _migrate_from_json_cache(self):
        """从旧的 JSON 缓存文件迁移数据到 SQLite"""
        old_json = os.path.join(_PROXY_CACHE_DIR, "proxy_cache.json")
        if not os.path.exists(old_json):
            return
        try:
            import json
            with open(old_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            proxies = data.get("proxies", [])
            meta = data.get("meta", {})
            if not proxies:
                return
            conn = sqlite3.connect(_PROXY_CACHE_DB, timeout=10)
            conn.execute("BEGIN TRANSACTION")
            try:
                for p in proxies:
                    conn.execute(
                        "INSERT OR IGNORE INTO proxy_cache (protocol, address, source, success_count, fail_count, score, last_verified) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (p.get("protocol", "http"), p.get("address", ""), p.get("source", ""),
                         p.get("success_count", 0), p.get("fail_count", 0), p.get("score", 0.0),
                         p.get("last_verified", 0))
                    )
                for k, v in meta.items():
                    conn.execute("INSERT OR REPLACE INTO cache_meta (key, value) VALUES (?, ?)", (k, str(v)))
                conn.commit()
                logger.info("已将 %s 个代理从 JSON 缓存迁移到 SQLite", len(proxies))
            except Exception:
                conn.rollback()
            finally:
                conn.close()
            # 重命名旧 JSON 文件以防重复迁移
            os.rename(old_json, old_json + ".bak")
        except Exception as e:
            logger.warning("迁移 JSON 缓存失败: %s", e)

    def _load_cache(self):
        """从 SQLite 缓存加载代理列表"""
        if not os.path.exists(_PROXY_CACHE_DB):
            return
        try:
            conn = sqlite3.connect(_PROXY_CACHE_DB, timeout=10)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 读取元数据
            cursor.execute("SELECT key, value FROM cache_meta")
            meta = {row["key"]: row["value"] for row in cursor.fetchall()}
            self._last_fetch_time = int(meta.get("last_fetch_time", 0))
            self._last_verify_time = int(meta.get("last_verify_time", 0))

            # 读取代理列表
            cursor.execute("SELECT protocol, address, source, success_count, fail_count, score, last_verified FROM proxy_cache")
            self._proxies = []
            self._working_proxies = []
            for row in cursor.fetchall():
                p = {
                    "protocol": row["protocol"],
                    "address": row["address"],
                    "source": row["source"],
                    "success_count": row["success_count"],
                    "fail_count": row["fail_count"],
                    "score": row["score"],
                }
                self._proxies.append(p)
                # 如果 last_verified 非零，说明已验证通过，加入 working 列表
                if row["last_verified"] > 0:
                    self._working_proxies.append(p.copy())

            conn.close()
            logger.info("从缓存加载了 %s 个代理 (其中已验证可用 %s 个)", len(self._proxies), len(self._working_proxies))
        except Exception as e:
            logger.warning("加载缓存失败: %s", e)
            self._proxies = []
            self._working_proxies = []

    def _save_cache(self):
        """立即保存代理列表及验证结果到 SQLite 缓存（用于 fetch/verify 等同步点）
        
        先刷新所有挂起的延迟写入，再执行全量写入以确保一致性。
        """
        self._flush_pending_save()
        # 即使 _flush_pending_save 已经写入，也执行一次确保 fetch/verify 的结果落盘
        self._do_save_cache()
        with self._lock:
            self._last_save_time = time.time()

    def _do_save_cache(self):
        """实际执行 SQLite 写入 — 使用 UPSERT 增量更新，避免全量重写"""
        conn = None
        try:
            conn = sqlite3.connect(_PROXY_CACHE_DB, timeout=10)
            cursor = conn.cursor()

            # 使用事务批量 UPSERT
            cursor.execute("BEGIN TRANSACTION")

            # 写入元数据（始终更新）
            cursor.execute(
                "INSERT OR REPLACE INTO cache_meta (key, value) VALUES (?, ?)",
                ("last_fetch_time", str(int(self._last_fetch_time)))
            )
            cursor.execute(
                "INSERT OR REPLACE INTO cache_meta (key, value) VALUES (?, ?)",
                ("last_verify_time", str(int(self._last_verify_time)))
            )

            # 增量 UPSERT 代理记录
            for p in self._proxies:
                last_verified = 1.0 if any(
                    w["address"] == p["address"] and w["protocol"] == p["protocol"]
                    for w in self._working_proxies
                ) else 0.0
                cursor.execute(
                    """INSERT INTO proxy_cache (protocol, address, source, success_count, fail_count, score, last_verified)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(protocol, address) DO UPDATE SET
                           source = EXCLUDED.source,
                           success_count = EXCLUDED.success_count,
                           fail_count = EXCLUDED.fail_count,
                           score = EXCLUDED.score,
                           last_verified = EXCLUDED.last_verified""",
                    (
                        p["protocol"],
                        p["address"],
                        p.get("source", ""),
                        p.get("success_count", 0),
                        p.get("fail_count", 0),
                        p.get("score", 0.0),
                        last_verified
                    )
                )

            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("保存缓存失败: %s", e)
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass

    def _flush_pending_save(self):
        """在合适的同步点强制执行所有挂起的延迟写入"""
        with self._lock:
            if not self._pending_save:
                return
            self._pending_save = False
        self._do_save_cache()
        with self._lock:
            self._last_save_time = time.time()

    def _save_cache_delayed(self):
        """延迟批量写入缓存 — 合并高频写入（如 report_failure），降低 SQLite 压力
        
        仅在距离上次写入超过 _save_interval 时才真正落盘，
        否则只标记 pending，由下次同步点一次性写入。
        """
        with self._lock:
            self._pending_save = True
            now = time.time()
            if (now - self._last_save_time) >= self._save_interval:
                self._pending_save = False
                self._last_save_time = now
            else:
                return  # 未达到间隔，仅标记 pending
        self._do_save_cache()

    def fetch_proxies(self, force: bool = False) -> int:
        """
        从所有配置的源获取代理IP并保存
        
        Args:
            force: 是否强制刷新（忽略缓存）
            
        Returns:
            获取到的代理数量
        """
        now = time.time()
        if not force and (now - self._last_fetch_time) < self.cache_ttl and self._proxies:
            logger.info("使用缓存的代理列表（%s 个）", len(self._proxies))
            return len(self._proxies)

        # 1. 提取当前已存在代理的历史数据映射以供继承
        existing_history = {}
        with self._lock:
            for p in self._proxies:
                key = f"{p['protocol']}://{p['address']}"
                existing_history[key] = {
                    "success_count": p.get("success_count", 0),
                    "fail_count": p.get("fail_count", 0),
                    "score": p.get("score", 0.0)
                }

        # 调用 fetcher 获取新抓取的代理
        fetched_list = self.fetcher.fetch_all()
        
        # 2. 对抓取出的代理继承原有历史积分状态
        all_proxies = {}
        for proxy in fetched_list:
            key = f"{proxy['protocol']}://{proxy['address']}"
            if key not in all_proxies:
                history = existing_history.get(key)
                if history:
                    proxy["success_count"] = history["success_count"]
                    proxy["fail_count"] = history["fail_count"]
                    proxy["score"] = history["score"]
                else:
                    proxy["success_count"] = 0
                    proxy["fail_count"] = 0
                    proxy["score"] = 0.0
                all_proxies[key] = proxy

        with self._lock:
            self._proxies = list(all_proxies.values())
            self._last_fetch_time = now

        logger.info("共合并去重得到 %s 个代理IP", len(self._proxies))
        
        # 保存缓存
        self._save_cache()

        return len(self._proxies)

    def verify_proxies(
        self,
        force: bool = False,
        max_workers: Optional[int] = None,
        target_count: int = 300,
        test_url: Optional[str] = None,
        expected_content: Optional[str] = None
    ) -> int:
        """
        验证代理IP可用性并更新可用池
        
        Args:
            force: 是否强制校验
            max_workers: 最大并发校验协程数
            target_count: 目标数量
            test_url: 测试网页 URL
            expected_content: 期望包含的网页文本
            
        Returns:
            可用代理数量
        """
        now = time.time()
        # 如果不是强制验证，且上次验证结果在 6 小时以内，直接使用
        if not force and (now - self._last_verify_time) < 21600 and self._working_proxies:
            logger.info("使用缓存的验证代理列表（%s 个，上次验证于 %s 分钟前）", len(self._working_proxies), int((now - self._last_verify_time)/60))
            return len(self._working_proxies)

        if not self._proxies:
            self.fetch_proxies()

        if not self._proxies:
            logger.warning("没有可验证的代理")
            return 0

        # 调用 verifier 执行高并发检验
        working = self.verifier.verify_proxies(
            proxies=self._proxies,
            force=force,
            max_workers=max_workers,
            target_count=target_count,
            test_url=test_url,
            expected_content=expected_content
        )

        with self._lock:
            self._working_proxies = working
            self._last_verify_time = time.time()

        # 将验证成功后的 working_proxies 保存到磁盘缓存
        self._save_cache()

        return len(working)

    def report_failure(self, proxy_url: str):
        """
        当使用代理发生网络失败或连接超时等异常时，安全剔除并扣分
        （使用延迟批量写入缓存，降低高并发下的 SQLite 写入压力）
        """
        if not proxy_url:
            return

        should_save = False
        with self._lock:
            try:
                parts = proxy_url.split("://", 1)
                protocol = parts[0]
                address = parts[1]

                initial_len = len(self._working_proxies)
                self._working_proxies = [
                    p for p in self._working_proxies
                    if not (p["protocol"] == protocol and p["address"] == address)
                ]

                # 清理线程独占绑定中该失效代理的分配记录
                tids_to_del = [tid for tid, p_url in self._thread_proxy_map.items() if p_url == proxy_url]
                for tid in tids_to_del:
                    del self._thread_proxy_map[tid]

                # 更新历史评分数据：增加 fail_count，扣减 score
                updated_history = False
                for p in self._proxies:
                    if p["protocol"] == protocol and p["address"] == address:
                        p["fail_count"] = p.get("fail_count", 0) + 1
                        p["score"] = p.get("success_count", 0) - 3 * p["fail_count"]
                        updated_history = True
                        break

                if len(self._working_proxies) < initial_len or updated_history:
                    if len(self._working_proxies) < initial_len:
                        logger.info("剔除失效代理: %s，当前剩余可用: %s 个", proxy_url, len(self._working_proxies))
                    should_save = True  # 标记需要保存，合并高频写入
            except Exception as e:
                logger.warning("剔除代理失败: %s", e)

        if should_save:
            self._save_cache_delayed()  # 改为延迟写入，不直接写 SQLite

    def check_and_replenish(self, threshold: int = 200, target_count: int = 300):
        """
        若当前可用代理数少于 threshold，同步补给代理
        """
        with self._lock:
            if len(self._working_proxies) >= threshold:
                return
            if self._is_replenishing:
                # 已有其他线程在补充中，等待其完成后返回
                need_wait = True
            else:
                self._is_replenishing = True
                need_wait = False

        if need_wait:
            for _ in range(60):  # 最多等 30 秒
                with self._lock:
                    if not self._is_replenishing:
                        return
                time.sleep(0.5)
            return

        # 同步执行代理补充（阻塞），确保调用方拿到代理后再继续
        try:
            logger.info("可用代理数仅剩 %s，低于阈值 %s，正在同步补充...", len(self._working_proxies), threshold)
            self.fetch_proxies(force=True)
            self.verify_proxies(force=True, target_count=target_count)
            logger.info("代理补充完成: 可用 %s 个", len(self._working_proxies))
        except Exception as e:
            logger.warning("补充代理出现异常: %s", e)
        finally:
            with self._lock:
                self._is_replenishing = False

    def _should_replenish(self) -> bool:
        """在锁内快速判断是否需要触发补给（不执行实际补给）"""
        return len(self._working_proxies) < 200 and not self._is_replenishing

    def get_thread_exclusive_proxy(self) -> Optional[str]:
        """
        根据线程 ID 进行无重复队列轮询（Round-Robin）
        确保任意时刻一个代理 IP 尽可能只被一个活动线程独占使用。
        """
        current_thread_id = threading.get_ident()
        
        # 先快速检查是否需要触发补给（锁内仅做判断，不执行耗时操作）
        with self._lock:
            need_replenish = self._should_replenish()
            
            if not self._working_proxies:
                return None
            
            # 1. 清理已死亡线程的分配记录
            active_thread_ids = {t.ident for t in threading.enumerate() if t.ident is not None}
            dead_threads = [tid for tid in self._thread_proxy_map if tid not in active_thread_ids]
            for tid in dead_threads:
                del self._thread_proxy_map[tid]
                
            # 2. 如果当前线程已经分配了代理，直接返回已分配的
            if current_thread_id in self._thread_proxy_map:
                return self._thread_proxy_map[current_thread_id]
                
            # 3. 找出所有正在被活动线程使用的代理
            in_use_proxies = set(self._thread_proxy_map.values())
            
            # 获取所有可用代理 URL
            all_proxy_urls = [f"{p['protocol']}://{p['address']}" for p in self._working_proxies]
            
            # 4. 寻找未被占用的代理
            available_proxies = [p for p in all_proxy_urls if p not in in_use_proxies]
            
            if available_proxies:
                # 还有未占用的代理，通过轮询顺序选择一个，并记录分配
                selected_proxy = available_proxies[self._current_proxy_idx % len(available_proxies)]
                self._current_proxy_idx += 1
                self._thread_proxy_map[current_thread_id] = selected_proxy
                return selected_proxy
            else:
                # 所有代理都在使用中（线程数 > 代理数），则分配当前分配给最少线程的代理
                proxy_usage = {p: 0 for p in all_proxy_urls}
                for p in self._thread_proxy_map.values():
                    if p in proxy_usage:
                        proxy_usage[p] += 1
                
                min_usage = min(proxy_usage.values())
                candidates = [p for p, usage in proxy_usage.items() if usage == min_usage]
                
                selected_proxy = candidates[self._current_proxy_idx % len(candidates)]
                self._current_proxy_idx += 1
                self._thread_proxy_map[current_thread_id] = selected_proxy
                return selected_proxy
        
        # 锁外执行补给，避免阻塞其他获取代理的线程
        if need_replenish:
            self.check_and_replenish(threshold=200, target_count=300)

    def get_random_pool_proxy(self) -> Optional[str]:
        """
        随机从已验证可用的代理池中获取一个代理 IP，不与线程绑定，每次调用都可能不同。
        """
        # 先快速检查是否需要触发补给（锁内仅做判断，不执行耗时操作）
        with self._lock:
            need_replenish = self._should_replenish()
            if not self._working_proxies:
                return None
            p = random.choice(self._working_proxies)
            proxy_url = f"{p['protocol']}://{p['address']}"
        
        # 锁外执行补给，避免阻塞其他获取代理的线程
        if need_replenish:
            self.check_and_replenish(threshold=200, target_count=300)
        
        return proxy_url

    def get_random_proxy(self) -> Optional[str]:
        """获取当前线程独占的代理（原随机获取改为独占队列轮询模式）"""
        return self.get_thread_exclusive_proxy()

    def get_next_proxy(self) -> Optional[str]:
        """按顺序获取当前线程独占的代理（原普通轮询改为独占队列轮询模式）"""
        return self.get_thread_exclusive_proxy()

    def get_proxy_for_requests(self) -> Optional[Dict[str, str]]:
        """
        获取适用于 requests 库的代理字典
        """
        proxy_url = self.get_random_proxy()
        if proxy_url:
            return {"http": proxy_url, "https": proxy_url}
        return None

    def get_proxy_for_playwright(self) -> Optional[Dict[str, str]]:
        """
        获取适用于 Playwright 的代理配置
        """
        proxy_url = self.get_random_proxy()
        if proxy_url:
            return {"server": proxy_url}
        return None

    def get_stats(self) -> Dict:
        """获取代理统计信息"""
        return {
            "total": len(self._proxies),
            "working": len(self._working_proxies),
            "last_fetch": self._last_fetch_time,
            "last_verify": self._last_verify_time,
        }
