import os
import re
import random
import time
import threading
from curl_cffi import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from crawlers.base_crawler import DecryptSiteBaseCrawler


class MadouCrawler(DecryptSiteBaseCrawler):
    CATEGORIES = ["guochan", "oumei"]

    def __init__(self, db_manager):
        super().__init__(db_manager, "madou")
        self.check_resource_link = True  # 启用磁力链接二次去重
        self.domains = [
            "hfc.232668.xyz"
        ]
        self.current_domain_idx = 0
        self.base_domain = f"https://{self.domains[self.current_domain_idx]}"
        self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
        self.current_class = "guochan"
        self._domain_cooldown = {}
        self._cooldown_seconds = 60
        self._domain_lock = threading.Lock()

        # 尝试从本地缓存加载之前发现的最新域名
        self._load_domains_from_cache()

        from utils.pdf_generator import PDFRenderConfig
        self.pdf_config = PDFRenderConfig(
            ad_selectors=[
                'div[style*="height:60px"]',
                'div[style*="height:55px"]',
                'div[style*="height:70px"]',
                '#bottom_float'
            ]
        )

    # _build_headers 已提取到 BaseCrawler 基类中


    # on_start 继承自 PlaywrightBaseCrawler，无需重复实现



    def _is_valid_list_page(self, html):
        """判断 Playwright 兜底时页面是否有效"""
        return "torrent-list" in html or "class=\"torrent-list\"" in html

    def parse_list_page(self, list_page_content, page_num):
        """解析解密后的列表页，提取条目信息"""
        soup = BeautifulSoup(list_page_content, "lxml")
        table = soup.find('table', class_='torrent-list')
        if not table:
            return []
            
        parsed_items = []
        tbody = table.find('tbody')
        rows = tbody.find_all('tr') if tbody else table.find_all('tr')[1:]
        for tr in rows:
            tds = tr.find_all('td')
            if len(tds) < 2:
                continue
                
            date_str = tds[0].get_text().strip()
            
            a = tds[1].find('a')
            if not a:
                continue
            href = a.get('href', '')
            if not href:
                continue
            url = urljoin(self.base_domain, href)
            
            title = ""
            a_str = str(a)
            title_match = re.search(r"d\(['\"](.*?)['\"]\)", a_str)
            if title_match:
                encrypted_title = title_match.group(1)
                decrypted_title_html = self.decrypt_title(encrypted_title)
                if decrypted_title_html:
                    soup_title = BeautifulSoup(decrypted_title_html, "html.parser")
                    for span in soup_title.find_all('span'):
                        style = span.get('style', '')
                        if 'display:none' in style.replace(' ', '').lower():
                            span.decompose()
                    title = soup_title.get_text().strip()
            else:
                title = a.get_text().strip()
                
            if not title:
                continue

            parsed_items.append({
                'title': title,
                'url': url,
                'date_str': date_str,
                'class_name': self.current_class
            })
        return parsed_items

    def process_sub_page_if_needed(self, raw_item, idx):
        """请求详情页，解析资源元数据并生成 PDF，支持域名轮换重试"""
        original_url = raw_item['url']
        is_existing = False

        if getattr(self, 'no_pdf', False):
            time.sleep(random.uniform(0.3, 0.8))
        else:
            time.sleep(random.uniform(2.0, 5.0))
        
        detail_html = None
        url = original_url
        
        for _ in range(len(self.domains)):
            from urllib.parse import urlparse, urlunparse
            parsed_url = urlparse(original_url)
            with self._domain_lock:
                current_base = self.base_domain
            parsed_base = urlparse(current_base)
            if any(d in parsed_url.netloc for d in self.domains):
                parsed_url = parsed_url._replace(netloc=parsed_base.netloc, scheme=parsed_base.scheme)
                url = urlunparse(parsed_url)
            else:
                url = original_url

            list_url = self.base_list_url.format(self.current_class, 1)
            headers = self._build_headers(referer=list_url)
            redirect_content = None  # 追踪跳转页面内容

            for attempt in range(3):
                # 前两次尝试用代理，第三次降级为直连（避免代理异常导致误判网站不可达）
                proxies = None
                if attempt < 2:
                    from config import get_crawler_proxy, is_proxy_manager_enabled
                    crawler_proxy = get_crawler_proxy()
                    if crawler_proxy:
                        proxies = {"http": crawler_proxy, "https": crawler_proxy}
                    elif is_proxy_manager_enabled():
                        proxies = get_proxy_dict()

                try:
                    response = requests.get(url, headers=headers, timeout=15, proxies=proxies, impersonate="chrome120")
                    if response.status_code == 200:
                        decrypted = self.decrypt_html(response.text)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            detail_html = decrypted
                            break
                        # 记录跳转页面内容
                        if decrypted and "正在检测最新可用线路" in decrypted:
                            redirect_content = decrypted
                        elif "正在检测最新可用线路" in response.text:
                            redirect_content = response.text
                    elif response.status_code == 403:
                        print(f"[!] 详情页返回 403，疑似触发反爬: {url}")
                        break
                except Exception:
                    pass
                time.sleep(random.uniform(2.0, 4.0))

            if detail_html:
                break

            if not detail_html:
                try:
                    _, _, context = self._get_thread_resources()
                    page = context.new_page()
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    time.sleep(random.uniform(2.0, 4.0))
                    html = page.content()
                    page.close()
                    if "panel-title" in html or "torrent-description" in html:
                        detail_html = html
                        break
                    else:
                        decrypted = self.decrypt_html(html)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            detail_html = decrypted
                            break
                        # 记录跳转页面内容
                        if decrypted and "正在检测最新可用线路" in decrypted:
                            redirect_content = decrypted
                        elif "正在检测最新可用线路" in html:
                            redirect_content = html
                except Exception as e:
                    print(f"[-] Playwright 兜底抓取详情页异常 ({url}): {e}")

            if detail_html:
                break
            
            # 尝试从跳转页面提取最新域名
            if redirect_content and self._update_domains_from_redirect(redirect_content):
                continue  # 直接用新域名重试，跳过冷却等待
                
            time.sleep(random.uniform(5.0, 10.0))
            self._rotate_domain()

        if not detail_html:
            print(f"[-] 详情页 {original_url} 抓取失败（最终尝试 URL: {url}）")
            return False, None

        # 提取磁力链接
        magnet_link = ""
        magnet_match = re.search(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+", detail_html)
        if magnet_match:
            magnet_link = magnet_match.group(0)
        else:
            magnet_match = re.search(r"magnet:\?[^\s'\"<>\)]+", detail_html)
            if magnet_match:
                magnet_link = magnet_match.group(0)

        if not magnet_link:
            print(f"[-] 在详情页中未找到磁力链接: {original_url}")
            return False, None

        size_val = ""
        res_format = ""
        date_str = raw_item['date_str']

        # 兼容匹配详细发布时间
        date_match = re.search(r"发布时间[:：]\s*</div>\s*<div[^>]*>\s*([^\s<]+)", detail_html)
        if not date_match:
            date_match = re.search(r"【发布时间】：\s*(\d{4}-\d{2}-\d{2})", detail_html)
        if date_match:
            date_str = date_match.group(1).strip()

        # 兼容匹配影片大小
        size_match = re.search(r"影片大小[:：]\s*</div>\s*<div[^>]*>\s*([^\s<]+(\s*[a-zA-Z]+)?)", detail_html)
        if not size_match:
            size_match = re.search(r"【影片大小】：\s*([^<]+)", detail_html)
        if size_match:
            size_val = size_match.group(1).strip()

        # 兼容匹配影片格式
        format_match = re.search(r"影片格式[:：]\s*</div>\s*<div[^>]*>\s*([^\s<]+)", detail_html)
        if not format_match:
            format_match = re.search(r"【影片格式】：\s*([^<]+)", detail_html)
        if format_match:
            res_format = format_match.group(1).strip()

        category_map = {
            "guochan": "国产",
            "oumei": "欧美"
        }
        category = category_map.get(raw_item['class_name'], raw_item['class_name'])

        data = self.clean_common_metadata(
            title=raw_item['title'],
            date_str=date_str,
            resource_link=magnet_link,
            category=category,
            url=url,
            pikpak_link='',
            pdf_path=''
        )

        if size_val:
            data['size'] = size_val
        if res_format:
            data['resource_format'] = res_format

        # === 提前去重：在 PDF 生成前检查磁力链接是否已存在 ===
        if self.check_resource_link and magnet_link:
            existing_links = self.db_manager.filter_existing_resource_links([magnet_link])
            if magnet_link in existing_links:
                print(f"[{idx}] 磁力链接已存在，跳过 PDF 生成: {magnet_link[:60]}...")
                data['source'] = self.source_name
                return True, data

        # 处理 PDF 文件生成
        if self.is_test:
            print("-> 测试模式下跳过保存 PDF 以节省时间")
        else:
            data['pdf_path'] = self.retry_generate_pdf(url, date_str, raw_item['title'], max_retries=4, no_proxy_last=True)

        return is_existing, data

    def run(self, is_test=False, start_page=1, end_page=1, max_workers=None, **kwargs):
        """麻豆爬虫入口，对两个板块依次进行爬取，支持断点续爬"""
        return super().run(is_test=is_test, start_page=start_page, end_page=end_page, max_workers=max_workers, **kwargs)
