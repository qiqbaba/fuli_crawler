import os
import re
import random
import time
import threading
from curl_cffi import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from crawlers.base_crawler import DecryptSiteBaseCrawler
from utils.proxy_manager import get_proxy_dict


class DatangCrawler(DecryptSiteBaseCrawler):
    CATEGORIES = ["guochan", "wuma", "oumei"]

    def __init__(self, db_manager):
        # source_name 设为 datang
        super().__init__(db_manager, "datang")
        self.main_domain = "https://dtbt7.com"
        self.domain_pattern = r'([a-z]{2,5}\.\d{5,7}\.xyz)'
        self.domains = []
        self.current_domain_idx = 0
        self.base_domain = self.main_domain
        self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
        self.current_class = "guochan"
        self._domain_cooldown = {}
        self._cooldown_seconds = 60
        self._domain_lock = threading.Lock()

        # 尝试从本地缓存加载之前发现的最新域名
        self._load_domains_from_cache()

        from utils.pdf_generator import PDFRenderConfig
        self.pdf_config = PDFRenderConfig(
            ad_block_js="""() => {
                const breadcrumbs = document.querySelector('.breadcrumbs');
                if (breadcrumbs) {
                    let prev = breadcrumbs.previousElementSibling;
                    while (prev) {
                        if (prev.classList.contains('gs-isgood') && 
                            !prev.textContent.includes('永久地址') && 
                            !prev.textContent.includes('永久')) {
                            prev.remove();
                        }
                        prev = prev.previousElementSibling;
                    }
                }
                const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"]');
                adDivs.forEach(div => div.remove());
                const bottomFloat = document.getElementById('bottom_float');
                if (bottomFloat) {
                    bottomFloat.remove();
                }
            }"""
        )

    def _is_valid_list_page(self, html):
        """判断 Playwright 兜底时页面是否有效"""
        return "class=\"bt_ul\"" in html or "class='bt_ul'" in html or "bt_ul" in html

    def parse_list_page(self, list_page_content, page_num):
        """解析解密后的列表页，提取条目信息"""
        soup = BeautifulSoup(list_page_content, "lxml")
        ul = soup.find('ul', class_='bt_ul')
        if not ul:
            return []
            
        parsed_items = []
        for li in ul.find_all('li'):
            a = li.find('a')
            if not a:
                continue
            href = a.get('href', '')
            if not href:
                continue
            url = urljoin(self.base_domain, href)
            
            # 提取加密 title (如果是明文则可以直接取 text)
            title = ""
            a_str = str(a)
            title_match = re.search(r"d\(['\"](.*?)['\"]\)", a_str)
            if title_match:
                encrypted_title = title_match.group(1)
                title = self.decrypt_title(encrypted_title)
            else:
                title = a.get_text().strip()
                
            if not title:
                continue
            
            # 提取时间 [MM-DD] 作为临时值
            span = li.find('span', class_='list_item')
            date_str = ""
            if span:
                span_text = span.get_text()
                date_match = re.search(r"\[\d{2}-\d{2}\]", span_text)
                if date_match:
                    date_str = date_match.group(0).strip('[]')

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
        is_existing = False  # 外部 BaseCrawler 已进行过批量去重过滤

        # 每个详情页请求前随机延迟，模拟人类浏览行为
        if getattr(self, 'no_pdf', False):
            time.sleep(random.uniform(0.3, 0.8))
        else:
            time.sleep(random.uniform(2.0, 5.0))
        
        detail_html = None
        url = original_url  # Bug 6 修复：提前初始化 url，避免循环外 NameError
        
        # 最多尝试轮换所有域名的次数
        for _ in range(len(self.domains)):
            # 动态替换域名为当前的最优域名（线程安全读取）
            from urllib.parse import urlparse, urlunparse
            parsed_url = urlparse(original_url)
            with self._domain_lock:
                current_base = self.base_domain
            parsed_base = urlparse(current_base)
            if any(d in parsed_url.netloc for d in self.domains) or "685835.xyz" in parsed_url.netloc:
                parsed_url = parsed_url._replace(netloc=parsed_base.netloc, scheme=parsed_base.scheme)
                url = urlunparse(parsed_url)
            else:
                url = original_url

            # 使用完整浏览器请求头
            list_url = self.base_list_url.format(self.current_class, 1)
            headers = self._build_headers(referer=list_url)
            redirect_content = None  # 追踪跳转页面内容

            # Bug 10 修复：删除外层冗余死代码，代理配置统一在内层循环中按需获取
            # 1. 优先使用 requests
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
                        break  # 跳出重试，直接换域名
                except Exception:
                    pass
                time.sleep(random.uniform(2.0, 4.0))

            if detail_html:
                break

            # 2. 兜底使用 Playwright
            if not detail_html:
                try:
                    _, _, context = self._get_thread_resources()
                    page = context.new_page()
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    time.sleep(random.uniform(2.0, 4.0))
                    html = page.content()
                    page.close()
                    if "video-description" in html or "class=\"video-description\"" in html:
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
                
            # 当前域名请求失败或解密结果为虚假发布页，冷却等待后轮换域名重试
            time.sleep(random.uniform(5.0, 10.0))
            self._rotate_domain()

        if not detail_html:
            # Bug 6 修复：使用 original_url 而非循环内局部 url，确保错误日志准确
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

        # 匹配详细发布时间
        date_match = re.search(r"【发布时间】：\s*(\d{4}-\d{2}-\d{2})", detail_html)
        if date_match:
            date_str = date_match.group(1)

        # 匹配影片大小
        size_match = re.search(r"【影片大小】：\s*([^<]+)", detail_html)
        if size_match:
            size_val = size_match.group(1).strip()

        # 匹配影片格式
        format_match = re.search(r"【影片格式】：\s*([^<]+)", detail_html)
        if format_match:
            res_format = format_match.group(1).strip()

        category_map = {
            "guochan": "国产",
            "wuma": "无码",
            "oumei": "欧美"
        }
        category = category_map.get(raw_item['class_name'], raw_item['class_name'])

        # 数据结构清洗
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
            saved_pdf = self.retry_generate_pdf(
                url, date_str, raw_item['title'],
                max_retries=4, no_proxy_last=True
            )
            data['pdf_path'] = saved_pdf

        return is_existing, data
