import re
import json
import random
import time
import base64
import threading
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
from curl_cffi import requests

from crawlers.base_crawler import DecryptSiteBaseCrawler, CrawlConfig


class DashenCrawler(DecryptSiteBaseCrawler):
    max_retries = 4  # 大神爬虫默认尝试 4 次
    CATEGORIES = ["guochan", "oumei"]
    default_end_page = 50
    default_workers = 8

    def __init__(self, db_manager):
        config = CrawlConfig(
            source_name="dashen",
            categories=["guochan", "oumei"],
            initial_domains=["exh.638552.xyz", "fst.896259.xyz", "ysp.399893.xyz"],
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
                '.dp-container',
                '#dp-container',
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
                const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float') || document.getElementById('dp-container') || document.querySelector('.dp-container');
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
        """重写解密 HTML：
        新版站点为 SPA 前端渲染架构（HTML 包含 var __data__ 或 __shared_data__），无需整页 Base64 解密；
        旧版站点则进行 Base64 解密并还原 document.write(d('...'))。
        """
        if not raw_html:
            return ""

        # 检查是否为新版 SPA 页面
        if "var __data__" in raw_html or "__shared_data__" in raw_html or "list_items" in raw_html:
            return raw_html

        # 兼容旧版整页 Base64 解密
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

    def _decrypt_title(self, title_enc: str) -> str:
        """解密标题：新版为倒序 Base64 UTF-8，旧版为正序 Base64"""
        if not title_enc:
            return ""
        # 1. 尝试新版倒序 Base64 解密 (atob(reversed))
        try:
            reversed_b64 = title_enc[::-1]
            decoded_bytes = base64.b64decode(reversed_b64)
            title = decoded_bytes.decode('utf-8', errors='ignore')
            clean_title = self._clean_text_with_spans(title)
            if clean_title:
                return clean_title
        except Exception:
            pass

        # 2. 尝试旧版正序 Base64 解密
        try:
            decoded_bytes = base64.b64decode(title_enc)
            title = decoded_bytes.decode('utf-8', errors='ignore')
            clean_title = self._clean_text_with_spans(title)
            if clean_title:
                return clean_title
        except Exception:
            pass

        return title_enc.strip()

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
        """判断 Playwright 兜底时列表页是否有效"""
        return 'var __data__' in html or 'list_items' in html or 'class="list"' in html or "class='list'" in html or "大神" in html

    def parse_list_page(self, list_page_content, page_num):
        """解析列表页内容，优先支持新版 SPA JSON 数据结构，向下兼容旧版 DOM 解析"""
        if not list_page_content:
            return []

        parsed_items = []

        # 1. 优先解析新版 SPA 内嵌数据: var __data__ = Object.assign({}, __shared_data__, {...});
        match = re.search(r'var\s+__data__\s*=\s*Object\.assign\([^,]+,\s*[^,]+,\s*(\{.*?\})\s*\);', list_page_content, re.DOTALL)
        if match:
            try:
                json_str = match.group(1)
                data = json.loads(json_str)
                list_items = data.get("list_items", [])
                for item in list_items:
                    link = item.get("link", "")
                    if not link or "open.php" in link:
                        continue
                    url = urljoin(self.base_domain, link)
                    title = self._decrypt_title(item.get("title_enc", ""))
                    if not title:
                        continue
                    date_str = item.get("date", "")
                    parsed_items.append({
                        'title': title,
                        'url': url,
                        'date_str': date_str,
                        'class_name': self.current_class
                    })
                if parsed_items:
                    return parsed_items
            except Exception as e:
                self.log.warning("[!] 解析新版列表页 JSON 数据异常: %s", e)

        # 2. 兼容旧版 HTML DOM 解析: <ul class="list"><li>...</li></ul>
        soup = BeautifulSoup(list_page_content, "lxml")
        ul = soup.find('ul', class_='list')
        if ul:
            rows = ul.find_all('li')
            for li in rows:
                a = li.find('a')
                if not a:
                    continue
                href = a.get('href', '')
                if not href or "open.php" in href:
                    continue

                url = urljoin(self.base_domain, href)
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
        return "var __data__" in html or "magnet:?" in html or "【发布时间】" in html or "【影片大小】" in html or "download.php" in html

    def _should_rewrite_url(self, netloc):
        """判断是否应使用当前域名重写 URL"""
        return any(d in netloc for d in self.domains) or "336292.xyz" in netloc or "638552.xyz" in netloc

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
        """请求详情页，解析资源元数据（优先从新版 SPA 数据读取磁力，兼容旧版下载页跳转），最后生成 PDF"""
        original_url = raw_item['url']
        is_existing = False

        if getattr(self, 'no_pdf', False):
            time.sleep(random.uniform(0.3, 0.8))
        else:
            time.sleep(random.uniform(1.5, 3.5))

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

            max_retries = self.max_retries
            for attempt in range(max_retries):
                proxies = None
                if attempt < max_retries - 1:
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
                time.sleep(random.uniform(1.5, 3.0))

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

        magnet_link = ""
        date_str = raw_item.get('date_str', '')
        size_val = ""
        res_format = ""
        title = raw_item.get('title', '')

        # 1. 优先从新版 SPA 详情页中的 var __data__ 提取元数据与磁力链接
        d_match = re.search(r'var\s+__data__\s*=\s*Object\.assign\([^,]+,\s*[^,]+,\s*(\{.*?\})\s*\);', detail_html, re.DOTALL)
        if d_match:
            try:
                d_data = json.loads(d_match.group(1))
                magnet_link = d_data.get("magnet", "")
                date_val = d_data.get("date", "")
                if date_val:
                    date_str = date_val
                size_val = d_data.get("size", "")
                res_format = d_data.get("resolution", "")
                dec_title = self._decrypt_title(d_data.get("title_enc", ""))
                if dec_title:
                    title = dec_title
            except Exception as e:
                self.log.warning("[!] 解析新版详情页 __data__ 异常: %s", e)

        # 2. 如果新版未提取到磁力，兼容旧版详情页：从详情页提取下载跳转链接并请求获取磁力
        if not magnet_link:
            download_match = re.search(r'href=["\'](/download\.php\?[^"\']+)["\']', detail_html)
            if download_match:
                download_url = urljoin(url, download_match.group(1))
                magnet_link = self._fetch_magnet_from_download_page(download_url, url)

            date_str_old, size_val_old, res_format_old = self._extract_detail_metadata(detail_html, raw_item)
            if not date_str and date_str_old:
                date_str = date_str_old
            if not size_val and size_val_old:
                size_val = size_val_old
            if not res_format and res_format_old:
                res_format = res_format_old

        if not magnet_link:
            self.log.error("[-] 在详情页中未能获取到有效磁力链接: %s", original_url)
            return False, None

        category_map = self._get_category_map()
        category = category_map.get(raw_item['class_name'], raw_item['class_name'])

        data = self.clean_common_metadata(
            title=title,
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
                url, date_str, title,
                no_proxy_last=True
            )

        self.log.info("[%s] 抓取成功: %s", idx, data.get('title', '')[:60])
        return is_existing, data

    def _fetch_magnet_from_download_page(self, download_url, referer_url):
        """辅助方法：请求旧版下载页并解密提取磁力链接"""
        headers = self._build_headers(referer=referer_url)
        max_retries = self.max_retries
        for attempt in range(max_retries):
            proxies = None
            if attempt < max_retries - 1:
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
                self.log.warning("[!] 请求下载页面异常 (尝试 %s/%s): %s", attempt + 1, max_retries, e)
            time.sleep(random.uniform(1.0, 2.5))
        return ""
