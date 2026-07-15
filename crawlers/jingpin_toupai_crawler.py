import os
import re
import base64
import random
import time
import threading
import json
from curl_cffi import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from crawlers.base_crawler import PlaywrightBaseCrawler, DomainRotationMixin, DecryptMixin
from utils.logger import get_logger

logger = get_logger(__name__)


class JingpinToupaiCrawler(PlaywrightBaseCrawler, DomainRotationMixin, DecryptMixin):
    default_end_page = 20
    default_workers = 8

    def __init__(self, db_manager):
        super().__init__(db_manager, "jingpin_toupai")
        self.check_resource_link = True  # 启用磁力链接二次去重
        self.domains = [
            "pms.532862.xyz"
        ]
        self.current_domain_idx = 0
        self.base_domain = f"https://{self.domains[self.current_domain_idx]}"
        self.base_list_url = f"{self.base_domain}/list/{{}}-{{}}.html"
        self.current_class = "2935277"
        self.max_consecutive_existing = 15  # 连续抓到历史数据时早停
        self.category_map = {
            "2935277": "国产", 
            "2965277": "欧美", 
            "2975277": "国产"
        }
        
        # 域名冷却机制
        self._domain_cooldown = {}
        self._cooldown_seconds = 60
        self._domain_lock = threading.Lock()

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

    # _build_headers 已提取到 BaseCrawler 基类中

    # _save_pdf 逻辑已抽象到 base_crawler.py 和 utils/pdf_generator.py 中

    def fetch_list_page(self, page_num):
        """拉取列表页 HTML 并解码（解密全页倒序 Base64）"""
        for _ in range(len(self.domains)):
            url = self.base_list_url.format(self.current_class, page_num)
            headers = self._build_headers()

            for attempt in range(3):
                # 前两次尝试用代理，第三次降级为直连（避免代理异常导致误判网站不可达）
                proxies = None
                if attempt < 2:
                    from config import get_effective_proxy
                    proxies = get_effective_proxy()

                try:
                    logger.info("[*] 正在拉取列表页 (尝试 %s/3): %s", attempt + 1, url)
                    r = requests.get(url, headers=headers, impersonate="chrome110", timeout=20, proxies=proxies)
                    r.encoding = 'utf-8'
                    if r.status_code == 200:
                        decrypted = self.decrypt_html(r.text)
                        if decrypted:
                            return decrypted
                        else:
                            logger.error("[-] 列表页解密失败")
                    else:
                        logger.error("[-] 列表页请求失败，状态码: %s", r.status_code)
                except Exception as e:
                    logger.error("[-] 请求列表页发生异常: %s", e)

                if attempt < 2:
                    time.sleep(random.uniform(1.0, 2.0))

            # 如果失败，轮换域名再试
            self._rotate_domain()

        return None

    def parse_list_page(self, list_page_html, page_num):
        """解析列表页解密后的 HTML，从 JSON 中提取详情页 URL 列表"""
        if not list_page_html:
            return []

        # 匹配其中的 JSON 密文
        start_marker = "var j_b64 = '"
        start_idx = list_page_html.find(start_marker)
        if start_idx == -1:
            logger.error("[-] 未在解密的列表页中匹配到 'j_b64' 变量")
            return []

        val_start = start_idx + len(start_marker)
        end_idx = list_page_html.find("'", val_start)
        if end_idx == -1:
            logger.error("[-] 未在解密的列表页中匹配到 'j_b64' 变量结束符")
            return []

        j_b64_str = list_page_html[val_start:end_idx]
        try:
            decoded_bytes = base64.b64decode(j_b64_str)
            decoded_json = decoded_bytes.decode('utf-8')
            data = json.loads(decoded_json)
            
            items = data.get('l', {}).get('a', [])
            sub_urls = []
            for item in items:
                href = item.get('url', '')
                if href:
                    sub_urls.append(urljoin(self.base_domain, href))
            return sub_urls
        except Exception as e:
            logger.error("[-] 解析列表页 JSON 数据失败: %s", e)
            return []

    def process_sub_page_if_needed(self, sub_url, idx):
        """提取详情页数据并进行解密和 PDF 渲染"""
        headers = self._build_headers(referer=self.base_domain + "/")
        
        html_text = None
        for attempt in range(3):
            # 前两次尝试用代理，第三次降级为直连（避免代理异常导致误判网站不可达）
            proxies = None
            if attempt < 2:
                from config import get_effective_proxy
                proxies = get_effective_proxy()

            try:
                r = requests.get(sub_url, headers=headers, impersonate="chrome110", timeout=20, proxies=proxies)
                r.encoding = 'utf-8'
                if r.status_code == 200:
                    html_text = self.decrypt_html(r.text)
                    if html_text:
                        break
            except Exception as e:
                pass
            if attempt < 2:
                time.sleep(random.uniform(0.5, 1.5))

        if not html_text:
            logger.error("[-] 抓取详情页并解密失败: %s", sub_url)
            return False, None

        # 匹配其中的 JSON 密文
        start_marker = "var j_b64 = '"
        start_idx = html_text.find(start_marker)
        if start_idx == -1:
            logger.error("[-] 详情页中找不到 'j_b64' 变量: %s", sub_url)
            return False, None

        val_start = start_idx + len(start_marker)
        end_idx = html_text.find("'", val_start)
        if end_idx == -1:
            logger.error("[-] 详情页中找不到 'j_b64' 变量结束符: %s", sub_url)
            return False, None

        j_b64_str = html_text[val_start:end_idx]
        try:
            decoded_bytes = base64.b64decode(j_b64_str)
            decoded_json = decoded_bytes.decode('utf-8')
            data = json.loads(decoded_json)

            title = data.get('name', '无标题').strip()
            pub_time = "Unknown_Date" # 原始数据未直接暴露发布时间，留空
            
            # 该网站的 "tm" 字段就是其解密出来的磁力链接
            resource_link = data.get('tm', '').strip()
            size = data.get('ts', '').strip()
            res_format = data.get('tr', '').strip()
            category = self.category_map.get(self.current_class, '自拍')

            logger.info("[%s] 解析详情页成功: %s | 大小: %s | 链接: %s...", idx, title, size, resource_link[:50])

            # === 提前去重：在 PDF 生成前检查磁力链接是否已存在 ===
            if self.check_resource_link and resource_link:
                existing_links = self.db_manager.filter_existing_resource_links([resource_link])
                if resource_link in existing_links:
                    logger.info("[%s] 磁力链接已存在，跳过 PDF 生成: %s...", idx, resource_link[:60])
                    processed_data = self.clean_common_metadata(
                        title=title,
                        date_str=pub_time,
                        resource_link=resource_link,
                        category=category,
                        url=sub_url,
                        pdf_path=''
                    )
                    processed_data['size'] = size
                    processed_data['resource_format'] = res_format
                    processed_data['source'] = self.source_name
                    return True, processed_data

            # 写入 PDF 文件（测试模式跳过）
            pdf_path = ''
            if not self.is_test:
                for attempt in range(1, 5):
                    no_proxy = (attempt == 4)
                    if no_proxy:
                        logger.warning("[-] [PDF-SAVE] 详情页: %s 前3次代理均失败，第4次尝试直连...", sub_url)
                        try:
                            self._destroy_thread_resources()
                        except Exception:
                            pass
                        time.sleep(random.uniform(1.0, 2.0))
                    pdf_path = self._save_pdf(sub_url, pub_time, title, no_proxy=no_proxy)
                    if pdf_path:
                        break
                    else:
                        logger.warning("[-] [PDF-SAVE] 详情页: %s 生成 PDF 失败，第 %s/4 次重试...", sub_url, attempt)
                        if attempt < 4:
                            try:
                                self._destroy_thread_resources()
                            except Exception as rec_err:
                                logger.warning("[!] 重构 Playwright 资源失败: %s", rec_err)
                            time.sleep(random.uniform(1.5, 3.0))

            # 数据清洗入库
            processed_data = self.clean_common_metadata(
                title=title,
                date_str=pub_time,
                resource_link=resource_link,
                category=category,
                url=sub_url,
                pdf_path=pdf_path
            )

            # 覆盖具体的额外数据
            processed_data['size'] = size
            processed_data['resource_format'] = res_format
            processed_data['source'] = self.source_name

            return False, processed_data

        except Exception as e:
            logger.error("[-] 解析详情页 JSON 密文发生错误: %s", e)
            return False, None

    def get_categories(self):
        """返回要爬取的分类列表"""
        return ["2935277", "2965277", "2975277"]

    def before_category_crawl(self, category):
        """爬取分类前的准备工作"""
        self.current_class = category

    def run(self, is_test=False, start_page=1, end_page=1, max_workers=None, **kwargs):
        """爬虫流程入口，使用基类的多板块爬取逻辑"""
        if max_workers is None:
            if getattr(self, 'no_pdf', False):
                max_workers = 30
            else:
                max_workers = 5  # 合理线程数，Playwright 不易卡死
        
        super().run(is_test=is_test, start_page=start_page, end_page=end_page, 
                   max_workers=max_workers, **kwargs)