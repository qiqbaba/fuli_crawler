import os
import re
import time
import random
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from config import is_local_mode
from crawlers.base_crawler import PlaywrightBaseCrawler
from utils.logger import get_logger

logger = get_logger(__name__)


class GcbtCrawler(PlaywrightBaseCrawler):
    default_end_page = 20
    default_workers = 8
    max_retries = 10
    max_pdf_retries = 10

    def __init__(self, db_manager):
        super().__init__(db_manager, "gcbt")
        self.base_url = "https://gcbt.net/"
        self.target_domain = "gcbt.net"
        self.use_persistent_context = True
        self.proxy_test_url = "https://gcbt.net/download/8067.html"
        self.proxy_expected_content = "90231538e5368bb8422500604f01cb25edfeedb4"
        # 默认早停阈值为连续 20 条已存在记录
        self.max_consecutive_existing = 20
        self.max_consecutive_duplicate_pages = 3

        from utils.pdf_generator import PDFRenderConfig
        self.pdf_config = PDFRenderConfig(
            need_img_proxy=True,
            pre_access_url="https://gcbt.net/",
            referer="https://gcbt.net/",
            need_lazy_scroll=True,
            emulate_media="screen",
            ad_selectors=[
                '.layui-layer', '.layui-layer-shade',
                '.modal', '.modal-backdrop',
                '.swal-overlay', '.swal-modal', '.swal2-container',
                '[id*="layui-layer"]'
            ],
            ad_block_js="""() => {
                if (document.body) document.body.style.overflow = 'auto';
                if (document.documentElement) document.documentElement.style.overflow = 'auto';
            }"""
        )


    def get_list_url(self, page_num):
        """获取指定页码的列表页 URL"""
        if page_num == 1:
            return self.base_url
        return urljoin(self.base_url, f"page/{page_num}")

    # _http_get 继承自 BaseCrawler，无需重复实现

    def fetch_list_page(self, page_num):
        """抓取列表页内容"""
        list_url = self.get_list_url(page_num)
        logger.info("正在访问列表页: %s", list_url)
        _, html_text = self._http_get(list_url, timeout=25)
        return html_text

    def parse_list_page(self, list_page_html, page_num):
        """解析列表页卡片，提取子页面详情页链接"""
        if not list_page_html:
            return []
        
        soup = BeautifulSoup(list_page_html, 'html.parser')
        sub_urls = []
        list_url = self.get_list_url(page_num)
        
        for header in soup.find_all(['h2', 'h1']):
            a_tag = header.find('a')
            if a_tag:
                href = a_tag.get('href', '')
                if '/download/' in href and href.endswith('.html'):
                    full_url = urljoin(list_url, href)
                    if full_url not in sub_urls:
                        sub_urls.append(full_url)
                        
        return sub_urls



    # _save_pdf 逻辑已抽象到 base_crawler.py 和 utils/pdf_generator.py 中

    def process_sub_page_if_needed(self, sub_url, idx):
        """处理详情页并转换数据与 PDF"""
        is_existing = False
        _, html_text = self._http_get(sub_url, timeout=25)
        
        if not html_text:
            logger.error("[-] 子页面 %s 抓取失败", sub_url)
            return False, None
            
        soup = BeautifulSoup(html_text, 'html.parser')
        
        # 1. 解析标题
        title_meta = soup.find('meta', property='og:title')
        if title_meta:
            title = title_meta.get('content', '').strip()
        else:
            title = soup.title.text.strip() if soup.title else "无标题"
        if title.endswith(" - GCBT"):
            title = title[:-7].strip()
            
        # 2. 解析发布时间
        pub_time = "Unknown_Date"
        pub_meta = soup.find('meta', property='article:published_time')
        if pub_meta:
            pub_time_val = pub_meta.get('content', '').strip()
            if len(pub_time_val) >= 10:
                pub_time = pub_time_val[:10]
                
        # 3. 解析分类
        category = "视频"
        sec_meta = soup.find('meta', property='article:section')
        if sec_meta:
            category = sec_meta.get('content', '').strip()
            
        # 4. 提取大小与格式
        article = soup.find('article')
        article_text = article.text if article else ""
        
        size = ""
        size_match = re.search(r'【影片大小】\s*[:：]\s*([a-zA-Z0-9\.\s]+)', article_text)
        if size_match:
            size = size_match.group(1).strip()
            
        fmt = ""
        fmt_match = re.search(r'【影片格式】\s*[:：]\s*([a-zA-Z0-9]+)', article_text)
        if fmt_match:
            fmt = fmt_match.group(1).strip()
            
        # 5. 提取资源下载链接（安全匹配策略）
        download_links = []
        if article:
            for a in article.find_all('a'):
                href = a.get('href', '').strip()
                if not href:
                    continue
                # rmdown 种子哈希转换
                if 'rmdown.com/link.php' in href:
                    rm_match = re.search(r'hash=([0-9a-fA-F]{42,43})', href)
                    if rm_match:
                        btih = rm_match.group(1)[-40:].lower()
                        download_links.append(f"magnet:?xt=urn:btih:{btih}")
                # 磁力链接直采
                elif href.lower().startswith('magnet:'):
                    mag_match = re.search(r'magnet:\?xt=urn:btih:([0-9a-fA-F]{40})', href, re.IGNORECASE)
                    if mag_match:
                        download_links.append(href)
                # 其他外部种子跳转页
                elif any(domain in href for domain in ['82bt.com', 'rlink.php']) or href.endswith('.torrent'):
                    if 'javascript:' not in href and not any(ad in href for ad in ['t66y.com', 'bitbucket.org', 'chinaurl.github.io', 'madouqu.com', 'taocili.com']):
                        download_links.append(href)
        else:
            # 备用解析：当页面缺少 <article> 标签时，尝试从 body 中提取链接
            for a in soup.find_all('a'):
                href = a.get('href', '').strip()
                if not href:
                    continue
                if href.lower().startswith('magnet:'):
                    mag_match = re.search(r'magnet:\?xt=urn:btih:([0-9a-fA-F]{40})', href, re.IGNORECASE)
                    if mag_match:
                        download_links.append(href)
                elif 'rmdown.com/link.php' in href:
                    rm_match = re.search(r'hash=([0-9a-fA-F]{42,43})', href)
                    if rm_match:
                        btih = rm_match.group(1)[-40:].lower()
                        download_links.append(f"magnet:?xt=urn:btih:{btih}")

        if download_links:
            # 优先采用磁力链接
            magnet_links = [l for l in download_links if l.startswith('magnet:')]
            if magnet_links:
                resource_link = magnet_links[0]
            else:
                resource_link = download_links[0]
        else:
            resource_link = ""
            logger.warning("[%s] 未找到下载链接，resource_link 置空", idx)
            
        logger.info("[%s] 抓取成功: %s | 发布时间: %s | 大小: %s | 链接: %s...", idx, title, pub_time, size, resource_link[:60])
        
        # 6. 生成并渲染 PDF 文件（直接保存原网页，测试模式跳过）
        pdf_path = ''
        if not self.is_test and article:
            pdf_path = self.retry_generate_pdf(sub_url, pub_time, title)
            
        # 7. 调用通用清洗逻辑
        data = self.clean_common_metadata(
            title=title,
            date_str=pub_time,
            resource_link=resource_link,
            category=category,
            url=sub_url,
            pdf_path=pdf_path
        )
        
        # 覆盖精细匹配的字段
        data['size'] = size
        data['resource_format'] = fmt
        data['source'] = self.source_name
        
        return is_existing, data
