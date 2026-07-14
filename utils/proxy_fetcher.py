"""
代理IP获取模块
负责从多个免费代理源抓取原始IP列表
"""
import re
import asyncio
import aiohttp
from typing import List, Dict
from utils.logger import get_logger

logger = get_logger(__name__)

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
    # elliottophellia/proxylist - 经检测的代理
    "elliottophellia_http": "https://raw.githubusercontent.com/elliottophellia/proxylist/master/results/http/global/http_checked.txt",
    # roosterkid/openproxylist - 每小时更新的 RAW 列表
    "roosterkid_https": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "roosterkid_socks5": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
    # Anonym0usWork1221/Free-Proxies - 每两小时更新
    "anonym0uswork_http": "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt",
    "anonym0uswork_socks4": "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt",

    # 新增代理源：
    # Thordata/awesome-free-proxy-list (每天更新)
    "thordata_all": "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/all.txt",
    "thordata_http": "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/http.txt",
    "thordata_socks5": "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/socks5.txt",
    # VPSLabCloud/VPSLab-Free-Proxy-List (15分钟更新)
    "vpslab_all_elite": "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/all_elite.txt",
    "vpslab_http_all": "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/http_all.txt",
    "vpslab_socks5_all": "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks5_all.txt",

    # 新增代理源（第三批）：
    # r00tee/Proxy-List（每5分钟更新）
    "r00tee_https": "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt",
    "r00tee_socks5": "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks5.txt",
    # gfpcom/free-proxy-list（高容量代理列表）
    "gfpcom_http": "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/http.txt",
    "gfpcom_socks5": "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/socks5.txt",
    # databay-labs/free-proxy-list（SSL校验，5分钟更新）
    "databay_http": "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt",
    "databay_socks5": "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt",
    # vakhov/fresh-proxy-list（5-20分钟更新）
    "vakhov_http": "https://vakhov.github.io/fresh-proxy-list/http.txt",
    "vakhov_socks5": "https://vakhov.github.io/fresh-proxy-list/socks5.txt",
    # komutan234/Proxy-List-Free（高频更新）
    "komutan_http": "https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/http.txt",
    "komutan_socks5": "https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/socks5.txt",
}


class ProxyFetcher:
    """代理IP获取器 - 从多个免费代理源获取代理IP"""

    def __init__(self, sources: Dict[str, str] = None):
        self.sources = sources or PROXY_SOURCES

    # ---- 同步兼容接口 ----

    def fetch_all(self, max_workers: int = 20) -> List[Dict[str, str]]:
        """
        从所有配置的源并发获取代理IP并去重返回（同步入口，内部以完全异步方式执行）

        Args:
            max_workers: 保留参数，已由异步并发替代

        Returns:
            获取到的代理元信息列表，格式例如:
            [{"protocol": "http", "address": "ip:port", "source": "..."}]
        """
        return asyncio.run(self._fetch_all_async())

    async def _fetch_all_async(self) -> List[Dict[str, str]]:
        """
        完全异步地从所有配置的源获取代理IP并去重返回
        """
        logger.info("开始从 %s 个源异步并发获取代理IP...", len(self.sources))
        all_proxies = {}

        connector = aiohttp.TCPConnector(limit_per_host=10, limit=100)
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            tasks = {
                self._fetch_from_source_async(session, name, url): name
                for name, url in self.sources.items()
            }

            for coro in asyncio.as_completed(tasks):
                source_name = tasks[coro]
                try:
                    proxies = await coro
                    for proxy in proxies:
                        key = f"{proxy['protocol']}://{proxy['address']}"
                        if key not in all_proxies:
                            all_proxies[key] = proxy
                    logger.info("  %s: 获取到 %s 个代理", source_name, len(proxies))
                except Exception as e:
                    logger.warning("  %s: 获取失败 - %s", source_name, e)

        logger.info("共获取到 %s 个唯一代理", len(all_proxies))
        return list(all_proxies.values())

    async def _fetch_from_source_async(self, session: aiohttp.ClientSession, source_name: str, url: str) -> List[Dict[str, str]]:
        """从单个源异步获取代理列表"""
        proxies = []
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            }
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                text = await response.text()

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
            logger.warning("解析源 %s 失败: %s", source_name, e)

        return proxies
