import re
import random
import time
import base64
import threading
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
from curl_cffi import requests

from crawlers.base_crawler import DecryptSiteBaseCrawler, CrawlConfig


class DashenCrawler(DecryptSiteBaseCrawler):
    CATEGORIES = ["guochan", "oumei"]
    default_end_page = 50
    default_workers = 8

    def __init__(self, db_manager):
        config = CrawlConfig(
            source_name="dashen",
            categories=["guochan", "oumei"],
            initial_domains=["yjt.336292.xyz"],
            main_domain="https://j4f4.com",
            domain_pattern=r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)',
        )
        super().__init__(db_manager, "dashen", config=config)
        self.current_class = "guochan"

        # 尝试从本地缓存加载之前发现的最新域名
        self._load_domains_from_cache()

        # 配置大神的特定 PDF 渲染和广告屏蔽规则
        from utils.pdf_generator import PDFRenderConfig
        self.pdf_config = PDFRenderConfig(
            emulate_media="screen",
            ad_selectors=[
                'div[style*="height:60px"]',
                'div[style*="height:140px"]',
                'div[style*="height:150px"]',
                '#bottom_float',
                '.bottom_float',
                '.layui-layer',
                '.layui-layer-shade',
                '[id*="layui-layer"]',
            ],
            ad_block_js="""() => {
                document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
                if (document.body) document.body.style.overflow = 'auto';
                if (document.documentElement) document.documentElement.style.overflow = 'auto';
                const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:140px"], div[style*="height:150px"]');
                adDivs.forEach(div => div.remove());
                const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float');
                if (bottomFloat) {
                    bottomFloat.remove();
                }
            }""",
            ad_url_patterns=[
                r'(?:doubleclick|googleads|googlesyndication|google-analytics)\.com',
                r'(?:adservice|pagead2|partnerads)\.googlesyndication',
                r'(?:cas\.pm|syndication|adsystem)\.com',
                r'(?:googleadservices|googletagmanager)\.com',
                r'\.css\?ver=.*&(?:ad|ads|banner)',
                r'(?:popup|pop-under|popunder)',
                r'(?:layer|float)_?(?:ad|adv|ads)',
                r'/ad(?:s|sense|unit|server|frame|script)\.',
                r'(?:s\d+\.cnzz|cnzz\.com|h5\.cnzz)',
                r'(?:hm\.baidu|posbaidu|cpro\.baidu)',
                r'(?:tanx|alimama|mmstat)\.com',
                r'(?:qzs\.qq|qq\.com)/ad',
            ]
        )

    def decrypt_html(self, raw_html):
        """重写解密 HTML：解密后，将 document.write(d('...')) 进行全局还原，并过滤混淆 span 标签"""
        decrypted = super().decrypt_html(raw_html)
        if not decrypted:
            decrypted = raw_html

        def repl_script(match):
            b64_val = match.group(1)
            try:
                dec = base64.b64decode(b64_val).decode('utf-8')
                return self._clean_text_with_spans(dec)
            except Exception:
                return ""

        # 匹配 <script ...>document.write(d('...'));</script>
        pattern = re.compile(r'<script[^>]*>\s*document\.write\(d\(\s*[\'"](.*?)[\'"]\s*\)\);?\s*</script>', re.DOTALL)
        decrypted_replaced = pattern.sub(repl_script, decrypted)
        return decrypted_replaced

    def _clean_text_with_spans(self, text_html):
        """清除带有 display:none 的干扰 span 标签"""
        if not text_html:
            return ""
        if "<span" not in text_html:
            return text_html
        try:
            soup = BeautifulSoup(text_html, "lxml")
            for span in soup.find_all("span"):
                style = span.get("style", "")
                if "display:none" in style.replace(" ", "").lower():
                    span.decompose()
            return soup.get_text().strip()
        except Exception:
            return text_html

    def _is_valid_list_page(self, html):
        """判断 Playwright 兜底时页面是否有效"""
        return 'class="list"' in html or "class='list'" in html or "大神" in html

    def parse_list_page(self, list_page_content, page_num):
        """解析列表页内容（解密并还原后），提取条目信息"""
        soup = BeautifulSoup(list_page_content, "lxml")
        ul = soup.find('ul', class_='list')
        if not ul:
            return []

        parsed_items = []
        rows = ul.find_all('li')
        for li in rows:
            a = li.find('a')
            if not a:
                continue
            href = a.get('href', '')
            if not href or "open.php" in href:
                # 过滤“打开本页所有链接”以及无效空项
                continue

            url = urljoin(self.base_domain, href)
            # 在解密时已经用 document.write 还原并去除了干扰，这里直接 get_text() 获取干净标题
            title = a.get_text().strip()
            if not title:
                continue

            # 提取发布时间 [MM-DD] 作为临时值
            li_text = li.get_text()
            date_str = ""
            date_match = re.search(r"\[(\d{2}-\d{2})\]", li_text)
            if date_match:
                date_str = date_match.group(1)

            parsed_items.append({
                'title': title,
                'url': url,
                'date_str': date_str,
                'class_name': self.current_class
            })
        return parsed_items

    def _is_valid_detail_page(self, html):
        """判断 Playwright 兜底时详情页是否有效"""
        return "【发布时间】" in html or "【影片大小】" in html or "download.php" in html

    def _should_rewrite_url(self, netloc):
        """判断是否应使用当前域名重写 URL"""
        return any(d in netloc for d in self.domains) or "336292.xyz" in netloc

    def _get_category_map(self):
        return {"guochan": "国产", "oumei": "欧美"}

    def _fetch_domains_from_main_station(self):
        """依次尝试两个永久域名获取最新镜像站列表"""
        permanent_urls = ["https://j4f4.com", "https://f5e5.com"]
        for p_url in permanent_urls:
            self.log.info("[*] DASHEN 开始从主站 %s 动态获取最新域名列表...", p_url)
            self.main_domain = p_url
            success = super()._fetch_domains_from_main_station()
            if success and self.domains:
                self.log.info("[+] DASHEN 从主站 %s 动态获取域名成功: %s", p_url, self.domains)
                return True
        return False

    def process_sub_page_if_needed(self, raw_item, idx):
        """请求详情页，解析资源元数据（请求下载页获取磁力），最后生成 PDF"""
        original_url = raw_item['url']
        is_existing = False

        if getattr(self, 'no_pdf', False):
            time.sleep(random.uniform(0.3, 0.8))
        else:
            time.sleep(random.uniform(2.0, 5.0))

        detail_html = None
        url = original_url

        # 最多尝试轮换所有域名的次数
        for _ in range(len(self.domains)):
            from urllib.parse import urlparse, urlunparse
            parsed_url = urlparse(original_url)
            with self._domain_lock:
                current_base = self.base_domain
            parsed_base = urlparse(current_base)
            if self._should_rewrite_url(parsed_url.netloc):
                parsed_url = parsed_url._replace(netloc=parsed_base.netloc, scheme=parsed_base.scheme)
                url = urlunparse(parsed_url)
            else:
                url = original_url

            try:
                list_url = self.base_list_url.format(cat=self.current_class, page=1)
            except (KeyError, ValueError, IndexError):
                list_url = self.base_list_url.replace("{cat}", str(self.current_class)).replace("{page}", "1")
            headers = self._build_headers(referer=list_url)
            redirect_content = None

            # 1. 优先使用 requests
            for attempt in range(3):
                proxies = None
                if attempt < 2:
                    from config import get_effective_proxy
                    proxies = get_effective_proxy()

                try:
                    response = requests.get(url, headers=headers, timeout=15, proxies=proxies, impersonate="chrome120")
                    if response.status_code == 200:
                        decrypted = self.decrypt_html(response.text)
                        if decrypted and "正在检测" not in decrypted and "403 Forbidden" not in decrypted:
                            detail_html = decrypted
                            break
                        if decrypted and "正在检测" in decrypted:
                            redirect_content = decrypted
                        elif "正在检测" in response.text:
                            redirect_content = response.text
                    elif response.status_code == 403:
                        self.log.warning("[!] 详情页返回 403，疑似触发反爬: %s", url)
                        break
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
                    if self._is_valid_detail_page(html):
                        detail_html = html
                        break
                    else:
                        decrypted = self.decrypt_html(html)
                        if decrypted and "正在检测" not in decrypted and "403 Forbidden" not in decrypted:
                            detail_html = decrypted
                            break
                        if decrypted and "正在检测" in decrypted:
                            redirect_content = decrypted
                        elif "正在检测" in html:
                            redirect_content = html
                except Exception as e:
                    self.log.error("[-] Playwright 兜底抓取详情页异常 (%s): %s", url, e)

            if detail_html:
                break

            # 尝试从跳转页面提取最新域名
            if redirect_content and self._update_domains_from_redirect(redirect_content):
                continue

            # 当前域名请求失败，冷却等待后轮换域名重试
            time.sleep(random.uniform(5.0, 10.0))
            self._rotate_domain()

        if not detail_html:
            self.log.error("[-] 详情页 %s 抓取失败（最终尝试 URL: %s）", original_url, url)
            return False, None

        # 从详情页提取下载跳转链接
        download_match = re.search(r'href=["\'](/download\.php\?[^"\']+)["\']', detail_html)
        if not download_match:
            self.log.error("[-] 在详情页中未找到下载页面链接: %s", original_url)
            return False, None

        download_url = urljoin(url, download_match.group(1))

        # 请求下载页面提取磁力链接
        magnet_link = self._fetch_magnet_from_download_page(download_url, url)
        if not magnet_link:
            self.log.error("[-] 无法从下载页面获取磁力链接: %s", download_url)
            return False, None

        date_str, size_val, res_format = self._extract_detail_metadata(detail_html, raw_item)

        category_map = self._get_category_map()
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
                self.log.info("[%s] 磁力链接已存在，跳过 PDF 生成: %s...", idx, magnet_link[:60])
                data['source'] = self.source_name
                return True, data

        # 处理 PDF 文件生成
        if self.is_test:
            self.log.info("-> 测试模式下跳过保存 PDF 以节省时间")
        else:
            data['pdf_path'] = self.retry_generate_pdf(
                url, date_str, raw_item['title'],
                max_retries=4, no_proxy_last=True
            )

        return is_existing, data

    def _fetch_magnet_from_download_page(self, download_url, referer_url):
        """辅助方法：请求下载页并解密提取磁力链接"""
        headers = self._build_headers(referer=referer_url)
        for attempt in range(3):
            proxies = None
            if attempt < 2:
                from config import get_effective_proxy
                proxies = get_effective_proxy()
            try:
                response = requests.get(download_url, headers=headers, timeout=15, proxies=proxies, impersonate="chrome120")
                if response.status_code == 200:
                    clean_html = self.decrypt_html(response.text)
                    magnet_match = re.search(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+", clean_html)
                    if magnet_match:
                        return magnet_match.group(0)
                    magnet_match = re.search(r"magnet:\?[^\s'\"<>\)]+", clean_html)
                    if magnet_match:
                        return magnet_match.group(0)
            except Exception as e:
                self.log.warning("[!] 请求下载页面异常 (尝试 %s/3): %s", attempt + 1, e)
            time.sleep(random.uniform(1.0, 2.5))
        return ""
