import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from crawlers.base_crawler import DecryptSiteBaseCrawler, CrawlConfig


class MadouCrawler(DecryptSiteBaseCrawler):
    CATEGORIES = ["guochan", "oumei"]

    def __init__(self, db_manager):
        config = CrawlConfig(
            source_name="madou",
            categories=["guochan", "oumei"],
            initial_domains=["hfc.232668.xyz"],
        )
        super().__init__(db_manager, "madou", config=config)
        self.current_class = "guochan"

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

    # ------------------------------------------------------------------ #
    #  钩子方法：覆盖 DecryptSiteBaseCrawler 中的差异化逻辑
    # ------------------------------------------------------------------ #

    def _is_valid_detail_page(self, html):
        """判断 Playwright 兜底时详情页是否有效"""
        return "panel-title" in html or "torrent-description" in html

    def _extract_detail_metadata(self, detail_html, raw_item):
        """从详情页提取发布时间、大小、格式（麻豆兼容两种 HTML 结构）"""
        date_str = raw_item['date_str']
        size_val = ""
        res_format = ""

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

        return date_str, size_val, res_format

    def _get_category_map(self):
        return {"guochan": "国产", "oumei": "欧美"}

    # process_sub_page_if_needed 继承自 DecryptSiteBaseCrawler

    def run(self, is_test=False, start_page=1, end_page=1, max_workers=None, **kwargs):
        """麻豆爬虫入口，对两个板块依次进行爬取，支持断点续爬"""
        return super().run(is_test=is_test, start_page=start_page, end_page=end_page, max_workers=max_workers, **kwargs)
