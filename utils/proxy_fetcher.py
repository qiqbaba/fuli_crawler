"""
代理IP获取模块
负责从多个免费代理源抓取原始IP列表
"""
import os
import re
import asyncio
import aiohttp
from typing import List, Dict
from utils.logger import get_logger

logger = get_logger(__name__)

# ========== 代理源配置 (HTTPS / SOCKS5 / SOCKS4) ==========
PROXY_SOURCES = {
    # 1. 实时 HTTPS 专用源
    "proxyscrape_https": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=https&timeout=10000&country=all",
    "roosterkid_https": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "r00tee_https": "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt",
    "vmheaven_https": "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/https.txt",
    "ercindedeoglu_https": "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/https.txt",
    "jetkai_https": "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt",
    "zloi_https": "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt",
    "vakhov_https": "https://vakhov.github.io/fresh-proxy-list/https.txt",

    # 2. 实时 SOCKS5 代理源
    "proxyscrape_socks5": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=10000&country=all",
    "speedx_socks5": "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
    "speedx_proxy_list_socks5": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "proxyscraper_socks5": "https://raw.githubusercontent.com/ProxyScraper/ProxyScraper/main/socks5.txt",
    "monosans_socks5": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "roosterkid_socks5": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
    "thordata_socks5": "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/socks5.txt",
    "vpslab_socks5_all": "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks5_all.txt",
    "r00tee_socks5": "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks5.txt",
    "databay_socks5": "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/socks5.txt",
    "vakhov_socks5": "https://vakhov.github.io/fresh-proxy-list/socks5.txt",
    "komutan_socks5": "https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/socks5.txt",
    "gfpcom_socks5": "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/socks5.txt",
    # 新增 SOCKS5 优质源
    "murongpig_socks5": "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt",
    "ercindedeoglu_socks5": "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt",
    "zevtyardt_socks5": "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt",
    "vmheaven_socks5": "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/socks5.txt",
    "jetkai_socks5": "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",
    "zloi_socks5": "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt",
    "aliilapro_socks5": "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks5.txt",
    "shiftytr_socks5": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
    "hendrikbgr_socks5": "https://raw.githubusercontent.com/hendrikbgr/Free-Proxy-Repo/master/proxy_list.txt",
    "hookzof_socks5": "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "rdavydov_socks5": "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt",
    "prxchk_socks5": "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt",
    "sunny9577_socks5": "https://raw.githubusercontent.com/Sunny9577/proxy-scraper/master/generated/socks5_proxies.txt",

    # 3. 实时 SOCKS4 代理源
    "proxyscrape_socks4": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4&timeout=10000&country=all",
    "speedx_socks4": "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt",
    "speedx_proxy_list_socks4": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
    "proxyscraper_socks4": "https://raw.githubusercontent.com/ProxyScraper/ProxyScraper/main/socks4.txt",
    "monosans_socks4": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
    "roosterkid_socks4": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt",
    "anonym0uswork_socks4": "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt",
    "thordata_socks4": "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/socks4.txt",
    "vpslab_socks4_all": "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks4_all.txt",
    "r00tee_socks4": "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks4.txt",
    "vakhov_socks4": "https://vakhov.github.io/fresh-proxy-list/socks4.txt",
    "komutan_socks4": "https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/socks4.txt",
    "gfpcom_socks4": "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/socks4.txt",
    # 新增 SOCKS4 优质源
    "murongpig_socks4": "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt",
    "ercindedeoglu_socks4": "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks4.txt",
    "zevtyardt_socks4": "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks4.txt",
    "vmheaven_socks4": "https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/refs/heads/main/socks4.txt",
    "jetkai_socks4": "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt",
    "zloi_socks4": "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks4.txt",
    "aliilapro_socks4": "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/socks4.txt",
    "shiftytr_socks4": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt",
    "rdavydov_socks4": "https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks4.txt",
    "prxchk_socks4": "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt",
    "sunny9577_socks4": "https://raw.githubusercontent.com/Sunny9577/proxy-scraper/master/generated/socks4_proxies.txt",

    # 4. 高匿/精选综合源
    "proxifly_all": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    "vpslab_all_elite": "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/all_elite.txt",
    "clarketm_proxy": "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
}


class ProxyFetcher:
    """代理IP获取器 - 从多个免费代理源获取代理IP"""

    def __init__(self, sources: Dict[str, str] = None, fetch_proxy: str = None):
        self.sources = sources or PROXY_SOURCES
        self.fetch_proxy = fetch_proxy or os.environ.get("FETCH_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

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

        # 使用较长的超时，避免连接池导致"Session is closed"
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            source_names = list(self.sources.keys())
            tasks = [
                self._fetch_from_source_async(session, name, self.sources[name])
                for name in source_names
            ]
            
            # 使用 asyncio.gather 并发执行抓取任务，子任务抛出的异常会被捕获为结果返回
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for source_name, result in zip(source_names, results):
                if isinstance(result, Exception):
                    logger.warning("  %s: 获取失败 - %s", source_name, result)
                elif isinstance(result, list):
                    for proxy in result:
                        key = f"{proxy['protocol']}://{proxy['address']}"
                        if key not in all_proxies:
                            all_proxies[key] = proxy
                    logger.info("  %s: 获取到 %s 个代理", source_name, len(result))

        logger.info("共获取到 %s 个唯一代理", len(all_proxies))
        return list(all_proxies.values())

    async def _fetch_from_source_async(self, session: aiohttp.ClientSession, source_name: str, url: str) -> List[Dict[str, str]]:
        """从单个源异步获取代理列表"""
        proxies = []
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            }
            # 使用独立的请求超时，避免单个慢源拖垮整个 session
            request_timeout = aiohttp.ClientTimeout(total=25)
            async with session.get(url, headers=headers, proxy=self.fetch_proxy, timeout=request_timeout, ssl=False) as response:
                if response.status != 200:
                    return []
                raw_bytes = await response.read()
                text = raw_bytes.decode('utf-8', errors='ignore')

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
                default_protocol = "socks5"
            elif "socks4" in source_name.lower():
                default_protocol = "socks4"
            elif "https" in source_name.lower():
                default_protocol = "https"
            else:
                default_protocol = "http"

            # 解析代理列表，支持纯文本、CSV、含国家名或带协议头的格式
            for line in text.strip().splitlines():
                line = line.strip()
                if not line or line.startswith(('#', '//', ';')):
                    continue

                line_proto = default_protocol
                if '://' in line:
                    parts = line.split('://', 1)
                    if len(parts) == 2:
                        line_proto = parts[0].strip().lower()
                        line = parts[1].strip()

                match = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})", line)
                if match:
                    ip = match.group(1)
                    port = int(match.group(2))
                    if 1 <= port <= 65535:
                        proxies.append({
                            "protocol": line_proto,
                            "address": f"{ip}:{port}",
                            "source": source_name
                        })
        except Exception as e:
            logger.warning("解析源 %s 失败: %s", source_name, e)

        return proxies
