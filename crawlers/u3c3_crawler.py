import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from config import USER_AGENTS
from crawlers.base_crawler import BaseCrawler
from utils.proxy_manager import get_proxy_dict, get_proxy_manager

try:
    from utils.pikpak_extractor import get_pikpak_link
except ImportError:
    def get_pikpak_link(url, timeout=30, poll_interval=2, quiet=False):
        return url

class U3c3Crawler(BaseCrawler):
    def __init__(self, db_manager):
        super().__init__(db_manager, "u3c3")
        self.base_url = "https://u3c3.com/?p={}"
        self.max_consecutive_existing = None
        self.max_consecutive_duplicate_pages = 3

    def on_start(self):
        """初始化代理管理器"""
        from config import is_proxy_manager_enabled
        if is_proxy_manager_enabled():
            print("[*] 代理管理器已启用，正在获取和验证代理IP...")
            from config import PROXY_VERIFY_WORKERS
            manager = get_proxy_manager()
            if manager:
                manager.fetch_proxies(force=True)
                manager.verify_proxies(force=True, max_workers=PROXY_VERIFY_WORKERS)
                stats = manager.get_stats()
                print(f"[*] 代理管理器就绪: 总计 {stats['total']} 个，可用 {stats['working']} 个")

    def fetch_list_page(self, page_num):
        """抓取页面 HTML 内容，包含重试逻辑"""
        url = self.base_url.format(page_num)
        headers = {
            "User-Agent": random.choice(USER_AGENTS)
        }
        
        # 获取代理配置
        proxies = None
        from config import get_crawler_proxy, is_proxy_manager_enabled
        crawler_proxy = get_crawler_proxy()
        if crawler_proxy:
            proxies = {"http": crawler_proxy, "https": crawler_proxy}
        elif is_proxy_manager_enabled():
            proxies = get_proxy_dict()
        
        for attempt in range(3):
            try:
                response = requests.get(url, headers=headers, timeout=20, proxies=proxies)
                if response.status_code == 200:
                    return response.text
                print(f"[*] 页面 {page_num} 抓取失败 (HTTP {response.status_code})，尝试重试 ({attempt + 1}/3)...")
            except Exception as e:
                print(f"[*] 页面 {page_num} 抓取异常 ({e})，尝试重试 ({attempt + 1}/3)...")
            time.sleep(random.uniform(2.0, 4.0))
            
        return None

    def parse_list_page(self, list_page_content, page_num):
        """解析列表页 HTML，提取列表项"""
        soup = BeautifulSoup(list_page_content, "lxml")
        table = soup.find('table', class_='torrent-list')
        if not table:
            return []
            
        tbody = table.find('tbody')
        if not tbody:
            return []
            
        parsed_items = []
        for tr in tbody.find_all('tr'):
            tds = tr.find_all('td')
            if len(tds) < 6:
                continue
                
            # 1. 提取分类
            cat_td = tds[0]
            cat_a = cat_td.find('a')
            category = ""
            if cat_a:
                category = cat_a.get('title', '').strip()
                if not category:
                    cat_img = cat_a.find('img')
                    if cat_img:
                        category = cat_img.get('alt', '').strip()
                        
            # 2. 提取名称和详情 URL
            name_td = tds[1]
            name_a = name_td.find('a')
            if not name_a:
                continue
                
            href = name_a.get('href', '')
            # 过滤广告与置顶贴
            if "/view?id=" not in href:
                continue
                
            url = urljoin("https://u3c3.com", href)
            title = name_a.get('title', '').strip()
            if not title:
                title = name_a.text.strip()
                
            # 3. 提取链接 (仅磁力链接)
            link_td = tds[2]
            magnet_link = ""
            for a_link in link_td.find_all('a'):
                href_val = a_link.get('href', '')
                if href_val.lower().startswith('magnet:'):
                    magnet_link = href_val.strip()
                    break
                    
            if not magnet_link:
                continue
                
            # 4. 提取大小
            size_table = tds[3].text.strip()
            
            # 5. 提取日期
            date_str = tds[4].text.strip()
            
            # 6. 提取云盘 (PikPak 链接)
            cloud_td = tds[5]
            cloud_a = cloud_td.find('a')
            pikpak_link = ""
            if cloud_a:
                pikpak_link = cloud_a.get('href', '').strip()
                
            parsed_items.append({
                'title': title,
                'url': url,
                'category': category,
                'magnet_link': magnet_link,
                'size_table': size_table,
                'date_str': date_str,
                'pikpak_link': pikpak_link
            })
            
        return parsed_items

    def process_sub_page_if_needed(self, raw_item, idx):
        """
        数据获取与二次转换。
        因为 u3c3 是直接在列表获取完了所有字段，所以这里不需要发网络请求解析子页面。
        但在这里需要通过 pikpak_extractor 获取真实的 pikpak 链接。
        """
        url = raw_item['url']
        
        # 1. 检查是否已存在
        is_existing = self.db_manager.check_url_exists(url)
        if is_existing and not self.is_test:
            return True, None
            
        pikpak_link = raw_item['pikpak_link']
        real_pikpak = None
        if pikpak_link:
            try:
                # 限制超时为 5 秒，防止未缓存链接导致爬虫长时间阻塞
                real_pikpak = get_pikpak_link(pikpak_link, timeout=5, quiet=self.quiet)
            except Exception:
                pass
            if not real_pikpak:
                real_pikpak = pikpak_link
                
        # 2. 调用公共清洗逻辑
        data = self.clean_common_metadata(
            title=raw_item['title'],
            date_str=raw_item['date_str'],
            resource_link=raw_item['magnet_link'],
            category=raw_item['category'],
            url=url,
            pikpak_link=real_pikpak,
            pdf_path=''
        )
        
        # 3. 覆盖大小字段为网页列表自带的更精准的大小值
        data['size'] = raw_item['size_table']
        
        return is_existing, data
