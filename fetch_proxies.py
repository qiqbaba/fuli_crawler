import requests
import re
import os
import base64
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 动态生成 datiya.com 的源
today_str = datetime.datetime.now().strftime("%Y%m%d")
yesterday_str = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y%m%d")

# 代理源列表
PROXY_SOURCES = [
    # 1. GitHub: proxifly/free-proxy-list
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    # 2. GitHub: Surfboardv2ray/TGParse
    "https://raw.githubusercontent.com/Surfboardv2ray/TGParse/main/python/socks",
    # 3. GitHub: freenodes/freenodes
    "https://raw.githubusercontent.com/freenodes/freenodes/main/clash.yaml",
    "https://raw.githubusercontent.com/freenodes/freenodes/main/ClashPremiumFree.yaml",
    # 4. GitHub: awesome-vpn/awesome-vpn
    "https://raw.githubusercontent.com/awesome-vpn/awesome-vpn/master/all",
    # 5. free.datiya.com (动态生成今日与昨日的 Clash/V2ray 订阅)
    f"https://free.datiya.com/uploads/{today_str}-clash.yaml",
    f"https://free.datiya.com/uploads/{today_str}-v2ray.txt",
    f"https://free.datiya.com/uploads/{yesterday_str}-clash.yaml",
    f"https://free.datiya.com/uploads/{yesterday_str}-v2ray.txt",
    # 6. GitHub: sunny9577/proxy-scraper
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt",

    # 新增代理源
    # 7. GitHub: Thordata/awesome-free-proxy-list (每天更新)
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/all.txt",
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/socks5.txt",
    # 8. GitHub: VPSLabCloud/VPSLab-Free-Proxy-List (15分钟更新)
    "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/all_elite.txt",
    "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/http_all.txt",
    "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks5_all.txt",
    # 9. GitHub: Au1rxx/free-vpn-subscriptions (每小时更新，Clash/V2ray)
    "https://raw.githubusercontent.com/Au1rxx/free-vpn-subscriptions/main/output/clash.yaml",
    "https://raw.githubusercontent.com/Au1rxx/free-vpn-subscriptions/main/output/v2ray-base64.txt",
    # 10. GitHub: freefq/free (长期维护，V2ray)
    "https://raw.githubusercontent.com/freefq/free/master/v2",

    # 原有代理源
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=10000&country=all",
]

# 输出文件路径
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "proxies.txt")

# 匹配 IP:PORT 的正则
PROXY_PATTERN = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5})')


def fetch_proxies_from_url(url):
    """从单个URL抓取代理"""
    proxies = set()
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        text = resp.text

        # 1. 尝试判断是否为 Base64 编码（通常是 V2Ray 订阅，且不含常见 HTML 字符或空格）
        stripped_text = text.strip()
        if stripped_text and not any(c in stripped_text for c in [' ', '<', '>', '{', '}']):
            try:
                padding = len(stripped_text) % 4
                if padding > 0:
                    stripped_text += "=" * (4 - padding)
                decoded_bytes = base64.b64decode(stripped_text)
                decoded_text = decoded_bytes.decode('utf-8', errors='ignore')
                if decoded_text:
                    text = decoded_text
            except Exception:
                pass

        # 2. 判断是否是 YAML 格式 (Clash 配置文件)
        if "clash" in url.lower() or url.endswith(".yaml") or url.endswith(".yml") or "proxies:" in text:
            # 简单的 YAML 块级提取
            blocks = text.split("  - ")
            for block in blocks:
                server_match = re.search(r'server:\s*["\']?(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})["\']?', block)
                port_match = re.search(r'port:\s*(\d{1,5})', block)
                if server_match and port_match:
                    ip = server_match.group(1)
                    port = port_match.group(1)
                    port_num = int(port)
                    if 1 <= port_num <= 65535:
                        proxies.add(f"{ip}:{port}")

        # 3. 默认使用 IP:PORT 正则从文本提取
        matches = PROXY_PATTERN.findall(text)
        for m in matches:
            ip, port = m.rsplit(":", 1)
            port_num = int(port)
            if 1 <= port_num <= 65535:
                proxies.add(m)
        print(f"  [OK] {url} -> {len(proxies)} proxies")
    except Exception as e:
        print(f"  [FAIL] {url} -> {e}")
    return proxies


def main():
    all_proxies = set()
    print(f"Fetching proxies from {len(PROXY_SOURCES)} sources (concurrent)...\n")

    # 使用线程池并发请求，大幅加速
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_proxies_from_url, url): url for url in PROXY_SOURCES}
        for future in as_completed(futures):
            found = future.result()
            all_proxies.update(found)

    # 去重后写入文件
    sorted_proxies = sorted(all_proxies, key=lambda x: (x.split(".")[0], int(x.split(":")[1])))

    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for p in sorted_proxies:
            f.write(p + "\n")

    print(f"\nDone! Total unique proxies: {len(sorted_proxies)}")
    print(f"Saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
