"""
代理IP管理模块
从多个免费代理源获取、验证和管理代理IP
"""
import os
import re
import time
import random
import threading
import requests
import sys
import asyncio
import aiohttp
from aiohttp_socks import ProxyConnector
from typing import List, Optional, Dict
from config import is_local_mode, PROXY_VERIFY_TIMEOUT, PROXY_CACHE_TTL, PROXY_VERIFY_WORKERS


# ========== 代理源配置 ==========
PROXY_SOURCES = {
    # free-proxy-list.net - HTML 解析，稳定更新
    "free_proxy_list": "https://free-proxy-list.net/",
    # sslproxies.org - HTML 解析，HTTPS 专用
    "sslproxies_org": "https://www.sslproxies.org/",
    # proxyscrape - 免费 HTTP/HTTPS/SOCKS5 列表
    "proxyscrape_http": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all",
    "proxyscrape_https": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=https&timeout=10000&country=all",
    "proxyscrape_socks5": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=10000&country=all",
    # TheSpeedX/PROXY-List - 每天更新
    "speedx_http": "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
    "speedx_socks5": "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
    # proxy-list.download - 常见 HTTP/HTTPS 代理
    "proxylistdownload_http": "https://proxy-list.download/api/v1/get?type=http",
    "proxylistdownload_https": "https://proxy-list.download/api/v1/get?type=https",
    # ProxyScraper/ProxyScraper - 每小时自动更新
    "proxyscraper_http": "https://raw.githubusercontent.com/ProxyScraper/ProxyScraper/main/http.txt",
    "proxyscraper_socks4": "https://raw.githubusercontent.com/ProxyScraper/ProxyScraper/main/socks4.txt",
    "proxyscraper_socks5": "https://raw.githubusercontent.com/ProxyScraper/ProxyScraper/main/socks5.txt",
    
    # 新增代理源：
    # proxifly/free-proxy-list - 自动更新
    "proxifly_all": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    # TheSpeedX/PROXY-List - 每天更新的 HTTP 列表
    "speedx_proxy_list_http": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "speedx_proxy_list_socks5": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    # monosans/proxy-list
    "monosans_http": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "monosans_socks5": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    # mmpx12/proxy-list
    "mmpx12_https": "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt",
    "mmpx12_socks5": "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt",
    # proxy-list.download - SOCKS5 代理
    "proxylistdownload_socks5": "https://www.proxy-list.download/api/v1/get?type=socks5",
    # elliottophellia/proxylist - 经检测的代理
    "elliottophellia_http": "https://raw.githubusercontent.com/elliottophellia/proxylist/master/results/http/global/http_checked.txt",
    # roosterkid/openproxylist - 每小时更新的 RAW 列表
    "roosterkid_https": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "roosterkid_socks5": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
    # Anonym0usWork1221/Free-Proxies - 每两小时更新
    "anonym0uswork_http": "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt",
    "anonym0uswork_socks4": "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt",
}


# 测试目标URL（用于验证代理是否可用）
PROXY_TEST_URLS = [
    "https://www.cloudflare.com",
    "https://api.myip.com",
    "https://www.baidu.com",
]

# 代理缓存文件路径
_PROXY_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_profiles")
_PROXY_CACHE_FILE = os.path.join(_PROXY_CACHE_DIR, "proxy_cache.json")


class ProxyManager:
    """代理IP管理器 - 支持从多个源获取、验证和轮换代理IP"""
    
    def __init__(self, cache_ttl=43200):
        """
        初始化代理管理器
        
        Args:
            cache_ttl: 缓存有效期（秒），默认12小时 (43200秒)
        """
        self.cache_ttl = cache_ttl
        self._proxies: List[Dict[str, str]] = []  # [{"protocol": "http", "address": "ip:port", "source": "..."}]
        self._working_proxies: List[Dict[str, str]] = []
        self._lock = threading.Lock()
        self._last_fetch_time = 0
        self._last_verify_time = 0
        self._current_proxy_idx = 0
        self._thread_proxy_map: Dict[int, str] = {}  # thread_id -> proxy_url
        self._is_replenishing = False
        
        # 确保缓存目录存在
        os.makedirs(_PROXY_CACHE_DIR, exist_ok=True)
        
        # 加载缓存
        self._load_cache()
    
    def _load_cache(self):
        """从本地缓存加载代理列表"""
        if os.path.exists(_PROXY_CACHE_FILE):
            try:
                import json
                with open(_PROXY_CACHE_FILE, 'r') as f:
                    data = json.load(f)
                    self._proxies = data.get("proxies", [])
                    self._last_fetch_time = data.get("timestamp", 0)
                    self._working_proxies = data.get("working_proxies", [])
                    self._last_verify_time = data.get("last_verify_time", 0)
                    print(f"[ProxyManager] 从缓存加载了 {len(self._proxies)} 个代理 (其中已验证可用 {len(self._working_proxies)} 个)")
            except Exception as e:
                print(f"[ProxyManager] 加载缓存失败: {e}")
    
    def _save_cache(self):
        """保存代理列表及验证结果到本地缓存"""
        try:
            import json
            data = {
                "timestamp": self._last_fetch_time,
                "proxies": self._proxies,
                "working_proxies": self._working_proxies,
                "last_verify_time": self._last_verify_time
            }
            with open(_PROXY_CACHE_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[ProxyManager] 保存缓存失败: {e}")
    
    def fetch_proxies(self, force=False) -> int:
        """
        从所有配置的源获取代理IP
        
        Args:
            force: 是否强制刷新（忽略缓存）
            
        Returns:
            获取到的代理数量
        """
        now = time.time()
        if not force and (now - self._last_fetch_time) < self.cache_ttl and self._proxies:
            print(f"[ProxyManager] 使用缓存的代理列表（{len(self._proxies)} 个）")
            return len(self._proxies)
        
        print(f"[ProxyManager] 开始从 {len(PROXY_SOURCES)} 个源获取代理IP...")
        all_proxies = {}  # 使用字典去重
        
        for source_name, url in PROXY_SOURCES.items():
            try:
                proxies = self._fetch_from_source(source_name, url)
                for proxy in proxies:
                    key = f"{proxy['protocol']}://{proxy['address']}"
                    if key not in all_proxies:
                        all_proxies[key] = proxy
                print(f"[ProxyManager]   {source_name}: 获取到 {len(proxies)} 个代理")
            except Exception as e:
                print(f"[ProxyManager]   {source_name}: 获取失败 - {e}")
        
        with self._lock:
            self._proxies = list(all_proxies.values())
            self._last_fetch_time = now
        
        print(f"[ProxyManager] 共获取到 {len(self._proxies)} 个唯一代理")
        
        # 保存缓存
        self._save_cache()
        
        return len(self._proxies)
    
    def _fetch_from_source(self, source_name: str, url: str) -> List[Dict[str, str]]:
        """从单个源获取代理列表"""
        proxies = []
        
        try:
            response = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            response.raise_for_status()

            # HTTP 源页面可能是纯文本或 HTML，需要特殊处理
            text = response.text
            if source_name in ("free_proxy_list", "sslproxies_org"):
                # 提取 HTML 中的 ip:port 列表
                matches = re.findall(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{1,5})", text)
                for address in matches:
                    protocol = "http" if source_name == "free_proxy_list" else "https"
                    proxies.append({
                        "protocol": protocol,
                        "address": address,
                        "source": source_name
                    })
                return proxies

            # 确定协议类型
            if "socks5" in source_name.lower():
                protocol = "socks5"
            elif "socks4" in source_name.lower():
                protocol = "socks4"
            elif "https" in source_name.lower():
                protocol = "https"
            else:
                protocol = "http"

            # 解析代理列表，支持多种格式
            for line in text.strip().split('\n'):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                address = None
                if '://' in line:
                    parts = line.split('://', 1)
                    if len(parts) == 2:
                        protocol = parts[0].strip().lower()
                        address = parts[1].strip()
                else:
                    address = line

                if not address:
                    continue

                # 处理 JSON/CSV/HTML 中常见额外内容
                address = re.sub(r"[\[\]\"']", "", address)
                address = address.split()[0]
                address = address.strip(',;')

                if ':' in address:
                    ip, port = address.rsplit(':', 1)
                    if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", ip) and port.isdigit() and 1 <= int(port) <= 65535:
                        proxies.append({
                            "protocol": protocol,
                            "address": f"{ip}:{port}",
                            "source": source_name
                        })
        except Exception as e:
            print(f"[ProxyManager] 解析源 {source_name} 失败: {e}")

        return proxies
    
    def verify_proxies(self, force=False, max_workers=None, target_count=300) -> int:
        """
        验证代理IP是否可用
        
        Args:
            force: 是否强制重新验证
            max_workers: 并发验证协程数，默认使用 config 中的 PROXY_VERIFY_WORKERS
            target_count: 目标可用代理数量，达到后提前退出
            
        Returns:
            可用代理数量
        """
        now = time.time()
        # 如果不是强制验证，且上次验证结果在 6 小时 (21600 秒) 以内，直接使用
        if not force and (now - self._last_verify_time) < 21600 and self._working_proxies:
            print(f"[ProxyManager] 使用缓存的验证代理列表（{len(self._working_proxies)} 个，上次验证于 {int((now - self._last_verify_time)/60)} 分钟前）")
            return len(self._working_proxies)
        
        if not self._proxies:
            self.fetch_proxies()
        
        if not self._proxies:
            print("[ProxyManager] 没有可验证的代理")
            return 0
        
        if max_workers is None:
            max_workers = PROXY_VERIFY_WORKERS
        
        # 验证超时取配置值，但不超过 5 秒以加速验证
        verify_timeout = min(PROXY_VERIFY_TIMEOUT, 5)
        
        print(f"[ProxyManager] 开始异步验证 {len(self._proxies)} 个代理（并发协程数: {max_workers}，超时: {verify_timeout}s，目标数: {target_count}）...")
        
        working = []
        total = len(self._proxies)
        verified_count = [0]
        start_time = time.time()
        
        # 使用百度作为核心测试源
        test_url = "http://www.baidu.com"
        
        # 异步事件循环的停止信号与信号量
        stop_event = asyncio.Event()
        sem = asyncio.Semaphore(max_workers)
        
        async def verify_proxy(proxy):
            if stop_event.is_set():
                return
            
            protocol = proxy["protocol"].lower()
            address = proxy["address"]
            proxy_url = f"{protocol}://{address}"
            
            connector = None
            client_proxy = None
            try:
                # 配置代理类型
                if protocol in ("socks5", "socks4"):
                    connector = ProxyConnector.from_url(proxy_url)
                else:
                    client_proxy = proxy_url
                
                async with sem:
                    if stop_event.is_set():
                        return
                    
                    # 禁用 SSL 验证，因为我们只需要测试连接和 HTTP 连通性，不用关心证书
                    async with aiohttp.ClientSession(connector=connector) as session:
                        async with session.get(
                            test_url,
                            proxy=client_proxy,
                            timeout=aiohttp.ClientTimeout(total=verify_timeout),
                            headers={"User-Agent": "Mozilla/5.0"},
                            ssl=False
                        ) as resp:
                            if resp.status == 200:
                                if not stop_event.is_set():
                                    working.append(proxy)
                                    if len(working) >= target_count:
                                        stop_event.set()
            except Exception:
                pass
            finally:
                verified_count[0] += 1
                curr_count = verified_count[0]
                if curr_count % 50 == 0 or curr_count == total:
                    elapsed = time.time() - start_time
                    print(f"[ProxyManager]   进度: {curr_count}/{total}（已找到 {len(working)} 个可用，耗时 {elapsed:.1f}s）")

        async def main_verify():
            tasks = [asyncio.create_task(verify_proxy(p)) for p in self._proxies]
            await asyncio.gather(*tasks, return_exceptions=True)

        # 为当前线程配置独立的事件循环并同步执行，确保外部同步接口兼容性
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(main_verify())
        except Exception as e:
            print(f"[ProxyManager] 异步事件循环发生异常: {e}")
        finally:
            loop.close()

        with self._lock:
            self._working_proxies = working
            self._last_verify_time = time.time()

        # 将验证成功后的 working_proxies 保存到磁盘缓存
        self._save_cache()

        elapsed = time.time() - start_time
        print(f"[ProxyManager] 验证完成: {len(working)}/{total} 个代理可用（总耗时 {elapsed:.1f}s）")

        return len(working)
    
    def report_failure(self, proxy_url: str):
        """
        当使用代理发生网络失败或连接超时等异常时，调用该方法安全剔除该代理
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

                if len(self._working_proxies) < initial_len:
                    print(f"[ProxyManager] 剔除失效代理: {proxy_url}，当前剩余可用: {len(self._working_proxies)} 个")
                    should_save = True  # 标记需要保存，但在锁外执行
            except Exception as e:
                print(f"[ProxyManager] 剔除代理失败: {e}")

        # Bug 4 修复：_save_cache() 涉及文件 I/O（json.dump），
        # 在持有锁期间调用会导致高并发（50线程）时所有线程阻塞等待。
        # 将 I/O 操作移到锁外执行，彻底消除该问题。
        if should_save:
            self._save_cache()

    def check_and_replenish(self, threshold=200, target_count=300):
        """
        如果当前可用代理数少于 threshold，启动异步后台守护线程进行补充，直至 target_count
        """
        with self._lock:
            if len(self._working_proxies) >= threshold or self._is_replenishing:
                return
            self._is_replenishing = True
            
        def _run():
            try:
                print(f"[ProxyManager] 可用代理数仅剩 {len(self._working_proxies)}，低于阈值 {threshold}，启动后台线程自动补充...")
                # 强制重新拉取
                self.fetch_proxies(force=True)
                # 重新验证并补足
                self.verify_proxies(force=True, target_count=target_count)
            except Exception as e:
                print(f"[ProxyManager] 后台补充代理出现异常: {e}")
            finally:
                with self._lock:
                    self._is_replenishing = False

        threading.Thread(target=_run, daemon=True).start()

    def get_thread_exclusive_proxy(self) -> Optional[str]:
        """
        根据线程 ID 进行无重复队列轮询（Round-Robin），
        确保任意时刻一个代理 IP 尽可能只被一个活动线程独占使用。
        """
        # 前置检测并执行动态补充
        self.check_and_replenish(threshold=200, target_count=300)
        
        import threading
        current_thread_id = threading.get_ident()
        
        with self._lock:
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
                
    def get_random_proxy(self) -> Optional[str]:
        """
        获取当前线程独占的代理（原随机获取改为独占队列轮询模式）
        """
        return self.get_thread_exclusive_proxy()
    
    def get_next_proxy(self) -> Optional[str]:
        """
        按顺序获取当前线程独占的代理（原普通轮询改为独占队列轮询模式）
        """
        return self.get_thread_exclusive_proxy()
    
    def get_proxy_for_requests(self) -> Optional[Dict[str, str]]:
        """
        获取适用于 requests 库的代理字典
        
        Returns:
            {"http": "...", "https": "..."} 或 None
        """
        proxy_url = self.get_random_proxy()
        if proxy_url:
            return {"http": proxy_url, "https": proxy_url}
        return None
    
    def get_proxy_for_playwright(self) -> Optional[Dict[str, str]]:
        """
        获取适用于 Playwright 的代理配置
        
        Returns:
            {"server": "..."} 或 None
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


# ========== 全局代理管理器实例 ==========
_proxy_manager: Optional[ProxyManager] = None


def get_proxy_manager() -> Optional[ProxyManager]:
    """获取全局代理管理器实例"""
    global _proxy_manager
    if _proxy_manager is None:
        # 仅在本地模式下启用代理管理（云端使用环境变量配置）
        if is_local_mode():
            from config import PROXY_CACHE_TTL
            _proxy_manager = ProxyManager(cache_ttl=PROXY_CACHE_TTL)
        else:
            return None
    return _proxy_manager


def init_proxy_manager(force_fetch=False, force_verify=False) -> Optional[ProxyManager]:
    """
    初始化并预加载代理管理器
    
    Args:
        force_fetch: 是否强制获取新代理
        force_verify: 是否强制验证代理
        
    Returns:
        ProxyManager 实例或 None
    """
    manager = get_proxy_manager()
    if manager is None:
        return None
    
    if force_fetch or not manager._proxies:
        manager.fetch_proxies(force=force_fetch)
    
    if force_verify or not manager._working_proxies:
        manager.verify_proxies(force=force_verify)
    
    return manager


def get_proxy_string() -> str:
    """
    获取当前代理字符串，遵循 config 的运行时参数覆盖及禁用设置
    """
    from config import get_crawler_proxy, is_proxy_manager_enabled
    
    # 1. 优先使用命令行或环境变量中配置的固定代理
    fixed_proxy = get_crawler_proxy()
    if fixed_proxy:
        return fixed_proxy
        
    # 2. 如果没有指定固定代理且启用了代理IP管理器，使用管理器拿随机代理
    if is_proxy_manager_enabled():
        manager = get_proxy_manager()
        if manager:
            return manager.get_random_proxy() or ""
            
    return ""


def get_proxy_dict() -> Optional[Dict[str, str]]:
    """
    获取代理字典（用于 requests 库）
    
    Returns:
        {"http": "...", "https": "..."} 或 None
    """
    proxy_url = get_proxy_string()
    if proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    return None


if __name__ == "__main__":
    # 测试代理管理器
    print("=" * 60)
    print("代理IP管理器测试")
    print("=" * 60)
    
    manager = ProxyManager()
    
    # 获取代理
    count = manager.fetch_proxies(force=True)
    print(f"\n获取到 {count} 个代理")
    
    # 验证代理
    if count > 0:
        working = manager.verify_proxies(force=True, max_workers=100)
        print(f"可用代理: {working} 个")
        
        # 显示统计
        stats = manager.get_stats()
        print(f"\n统计信息: {stats}")
        
        # 获取随机代理
        proxy = manager.get_random_proxy()
        print(f"\n随机代理: {proxy}")
    else:
        print("未获取到代理，请检查网络连接")
