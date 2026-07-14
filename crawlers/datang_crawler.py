import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from crawlers.base_crawler import DecryptSiteBaseCrawler, CrawlConfig


class DatangCrawler(DecryptSiteBaseCrawler):
    CATEGORIES = ["guochan", "wuma", "oumei"]

    def __init__(self, db_manager):
        config = CrawlConfig(
            source_name="datang",
            categories=["guochan", "wuma", "oumei"],
            main_domain="https://dtbt7.com",
            domain_pattern=r'([a-z]{2,5}\.\d{5,7}\.xyz)',
        )
        super().__init__(db_manager, "datang", config=config)
        self.current_class = "guochan"

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

    # ------------------------------------------------------------------ #
    #  钩子方法：覆盖 DecryptSiteBaseCrawler 中的差异化逻辑
    # ------------------------------------------------------------------ #

    def _is_valid_detail_page(self, html):
        """判断 Playwright 兜底时详情页是否有效"""
        return "video-description" in html or "class=\"video-description\"" in html

    def _should_rewrite_url(self, netloc):
        """包含旧域名 685835.xyz 的 URL 重写判断"""
        return any(d in netloc for d in self.domains) or "685835.xyz" in netloc

    def _get_category_map(self):
        return {"guochan": "国产", "wuma": "无码", "oumei": "欧美"}

    # process_sub_page_if_needed 继承自 DecryptSiteBaseCrawler
