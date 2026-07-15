import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from crawlers.base_crawler import DecryptSiteBaseCrawler, CrawlConfig


class TaoseCrawler(DecryptSiteBaseCrawler):
    CATEGORIES = ["guochan", "oumei"]
    default_end_page = 70
    default_workers = 8

    def __init__(self, db_manager):
        config = CrawlConfig(
            source_name="taose",
            categories=["guochan", "oumei"],
            initial_domains=["ctk.558226.xyz", "hyx.396225.xyz", "ckp.635599.xyz"],
            main_domain="https://taosebt.com",
            domain_pattern=r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)',
        )
        super().__init__(db_manager, "taose", config=config)
        self.current_class = "guochan"

        # 尝试从本地缓存加载之前发现的最新域名
        self._load_domains_from_cache()

        from utils.pdf_generator import PDFRenderConfig
        self.pdf_config = PDFRenderConfig(
            emulate_media="screen",
            ad_selectors=[
                'div[style*="height:60px"]',
                'div[style*="height:55px"]',
                'div[style*="height:70px"]',
                '#bottom_float',
                '.layui-layer',
                '.layui-layer-shade',
                '[id*="layui-layer"]',
                '.modal',
                '.modal-backdrop'
            ],
            ad_block_js="""() => {
                document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
                if (document.body) document.body.style.overflow = 'auto';
                if (document.documentElement) document.documentElement.style.overflow = 'auto';
                const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
                adDivs.forEach(div => div.remove());
                const bottomFloat = document.getElementById('bottom_float');
                if (bottomFloat) {
                    bottomFloat.remove();
                }
            }""",
            ad_url_patterns=[
                # 通用广告联盟/统计
                r'(?:doubleclick|googleads|googlesyndication|google-analytics)\.com',
                r'(?:adservice|pagead2|partnerads)\.googlesyndication',
                r'(?:cas\.pm|syndication|adsystem)\.com',
                r'(?:googleadservices|googletagmanager)\.com',
                r'\.css\?ver=.*&(?:ad|ads|banner)',
                # 弹窗/浮层广告
                r'(?:popup|pop-under|popunder)',
                r'(?:layer|float)_?(?:ad|adv|ads)',
                r'/ad(?:s|sense|unit|server|frame|script)\.',
                r'(?:s\d+\.cnzz|cnzz\.com|h5\.cnzz)',
                r'(?:hm\.baidu|posbaidu|cpro\.baidu)',
                r'(?:tanx|alimama|mmstat)\.com',
                r'(?:qzs\.qq|qq\.com)/ad',
            ]
        )

    def _is_valid_list_page(self, html):
        """判断 Playwright 兜底时页面是否有效"""
        return 'class="list"' in html or "class='list'" in html or "桃色BT" in html

    def parse_list_page(self, list_page_content, page_num):
        """解析解密后的列表页，提取条目信息"""
        soup = BeautifulSoup(list_page_content, "lxml")
        ul = soup.find('ul', class_='list')
        if not ul:
            return []

        parsed_items = []
        rows = ul.find_all('li')
        for li in rows:
            date_str = ""
            li_text = li.get_text()
            date_match = re.search(r"\[(\d{2}-\d{2})\]", li_text)
            if date_match:
                date_str = date_match.group(1)

            a = li.find('a')
            if not a:
                continue
            href = a.get('href', '')
            if not href:
                continue
            url = urljoin(self.base_domain, href)

            # 提取加密 title
            title = ""
            script = a.find('script')
            if script:
                script_text = script.string or ""
                title_match = re.search(r"d\(['\"](.*?)['\"]\)", script_text)
                if title_match:
                    encrypted_title = title_match.group(1)
                    title = self.decrypt_title(encrypted_title)

            if not title:
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
        return "【发布时间】" in html or "【影片格式】" in html or "magnet:?" in html

    def _should_rewrite_url(self, netloc):
        """判断是否应使用当前域名重写 URL"""
        if "taosebt.com" in self.base_domain:
            return False
        return any(d in netloc for d in self.domains)

    def _get_category_map(self):
        return {"guochan": "国产", "oumei": "欧美"}
