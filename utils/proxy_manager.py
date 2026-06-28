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
                    print(f"[ProxyManager] 从缓存加载了 {len(self._proxies)} 个代理")
            except Exception as e:
                print(f"[ProxyManager] 加载缓存失败: {e}")
    
    def _save_cache(self):
        """保存代理列表到本地缓存"""
        try:
            import json
            data = {
                "timestamp": time.time(),
                "proxies": self._proxies
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
    
    def verify_proxies(self, force=False, max_workers=None) -> int:
        """
        验证代理IP是否可用
        
        Args:
            force: 是否强制重新验证
            max_workers: 并发验证线程数，默认使用 config 中的 PROXY_VERIFY_WORKERS
            
        Returns:
            可用代理数量
        """
        now = time.time()
        if not force and (now - self._last_verify_time) < self.cache_ttl and self._working_proxies:
            print(f"[ProxyManager] 使用已验证的代理列表（{len(self._working_proxies)} 个）")
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
        
        print(f"[ProxyManager] 开始验证 {len(self._proxies)} 个代理（并发数: {max_workers}，超时: {verify_timeout}s）...")
        
        working = []
        total = len(self._proxies)
        verified_count = 0
        start_time = time.time()
        
        # 轻量级测试 URL 列表（按速度排序，优先用快的）
        test_urls = [
            "http://httpbin.org/ip",
            "https://api.myip.com",
            "https://www.cloudflare.com",
        ]
        
        def verify_proxy(proxy):
            """验证单个代理 - 使用轻量级 HEAD 请求加速"""
            protocol = proxy["protocol"]
            address = proxy["address"]
            proxy_url = f"{protocol}://{address}"
            
            proxies_dict = {
                "http": proxy_url,
                "https": proxy_url,
            }
            
            # 优先尝试 HEAD 请求（更快），失败再尝试 GET
            for test_url in test_urls:
                try:
                    resp = requests.head(
                        test_url,
                        proxies=proxies_dict,
                        timeout=verify_timeout,
                        headers={"User-Agent": "Mozilla/5.0"}
                    )
                    if resp.status_code < 500:
                        return proxy
                except Exception:
                    pass
                # HEAD 失败，尝试 GET（某些服务器不支持 HEAD）
                try:
                    resp = requests.get(
                        test_url,
                        proxies=proxies_dict,
                        timeout=verify_timeout,
                        headers={"User-Agent": "Mozilla/5.0"}
                    )
                    if resp.status_code == 200:
                        return proxy
                except Exception:
                    pass
            return None
        
        # 使用线程池并发验证
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(verify_proxy, p): p for p in self._proxies}
            for future in concurrent.futures.as_completed(futures):
                verified_count += 1
                result = future.result()
                if result:
                    working.append(result)
                # 每验证 50 个打印一次进度
                if verified_count % 50 == 0 or verified_count == total:
                    elapsed = time.time() - start_time
                    print(f"[ProxyManager]   进度: {verified_count}/{total}（已找到 {len(working)} 个可用，耗时 {elapsed:.1f}s）")
        
        with self._lock:
            self._working_proxies = working
            self._last_verify_time = time.time()
        
        elapsed = time.time() - start_time
        print(f"[ProxyManager] 验证完成: {len(working)}/{total} 个代理可用（总耗时 {elapsed:.1f}s）")
        
        return len(working)
    
    def get_random_proxy(self) -> Optional[str]:
        """
        随机获取一个可用代理
        
        Returns:
            代理URL字符串，如 "http://123.45.67.89:8080"，如果没有可用代理则返回 None
        """
        with self._lock:
            if not self._working_proxies:
                return None
            proxy = random.choice(self._working_proxies)
            return f"{proxy['protocol']}://{proxy['address']}"
    
    def get_next_proxy(self) -> Optional[str]:
        """
        按顺序获取下一个代理（轮询方式）
        
        Returns:
            代理URL字符串
        """
        with self._lock:
            if not self._working_proxies:
                return None
            proxy = self._working_proxies[self._current_proxy_idx % len(self._working_proxies)]
            self._current_proxy_idx += 1
            return f"{proxy['protocol']}://{proxy['address']}"
    
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
    获取当前代理字符串（兼容原有的 CRAWLER_PROXY 配置）
    
    Returns:
        代理URL字符串，如果没有可用代理则返回空字符串
    """
    # 优先使用环境变量配置的代理
    env_proxy = os.environ.get("CRAWLER_PROXY", "")
    if env_proxy:
        return env_proxy
    
    # 否则使用代理管理器
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
