import os
import re
import threading
import time
import random
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from config import USER_AGENTS
from crawlers.base_crawler import PlaywrightBaseCrawler
from utils.date_parser import parse_date
from utils.proxy_manager import get_proxy_manager
from utils.logger import get_logger

logger = get_logger(__name__)


class SejuCrawler(PlaywrightBaseCrawler):
    default_end_page = 4
    default_workers = 8

    def __init__(self, db_manager):
        super().__init__(db_manager, "seju")
        self.base_url = "https://seju.life/page/{}/"
        self.target_domain = "seju.life"
        self.use_persistent_context = True
        self.proxy_test_url = "https://seju.life/"
        self.thread_local = threading.local()
        
        from utils.pdf_generator import PDFRenderConfig
        self.pdf_config = PDFRenderConfig(
            margin={"top": "20mm", "bottom": "20mm", "left": "20mm", "right": "20mm"}
        )

    def get_list_url(self, page_num):
        """获取指定页码的列表页 URL，针对第一页避免 301 重定向"""
        if page_num == 1:
            return "https://seju.life/"
        return self.base_url.format(page_num)

    def _wait_for_cloudflare_bypass(self, page, timeout_sec=60):
        """检测并等待 Cloudflare Challenge (Just a moment...) 页面自动重定向通过。
        
        优先使用 page.wait_for_url() 等待非 Cloudflare URL，减少轮询次数。
        若 wait_for_url 超时，则回退到轻量轮询（仅检查 title）。
        """
        from config import is_local_mode
        start_time = time.time()
        
        # 第一步：尝试用 wait_for_url 等待跳转到非 Cloudflare URL
        try:
            page.wait_for_url(
                lambda url: "cloudflare" not in url.lower(),
                timeout=timeout_sec * 1000
            )
            logger.info("[+] 已通过 wait_for_url 绕过 Cloudflare。当前 URL: %s", page.url)
            return True
        except Exception:
            # wait_for_url 超时，回退到轻量轮询
            pass
        
        # 第二步：回退轮询（仅检查 title，减少 page.title() 调用频率）
        while time.time() - start_time < timeout_sec:
            try:
                title = page.title()
                if "Just a moment..." in title:
                    if is_local_mode():
                        logger.warning("[!] 【有头辅助提示】检测到验证码，若卡在此处，请在弹出的浏览器窗口中手动完成验证。")
                    time.sleep(2.0)  # 增加间隔，减少轮询次数
                else:
                    logger.info("[+] 疑似已绕过 Cloudflare。当前 Title: '%s', URL: %s", title, page.url)
                    return True
            except Exception as e:
                logger.error("[-] 检查 Cloudflare 状态时异常: %s", e)
                time.sleep(2.0)
        
        # 最终检查
        try:
            final_title = page.title()
            if "Just a moment..." not in final_title and "cloudflare" not in page.url:
                return True
        except Exception:
            pass
        return False

    def _destroy_thread_resources(self):
        super()._destroy_thread_resources()
        if hasattr(self.thread_local, "list_page"):
            del self.thread_local.list_page

    def fetch_list_page(self, page_num):
        """使用 Playwright 加载列表页并返回当前 HTML 文本"""
        list_url = self.get_list_url(page_num)
        logger.info("[*] 正在访问列表页: %s", list_url)
        try:
            _, _, context = self._get_thread_resources()
            if not hasattr(self.thread_local, "list_page") or self.thread_local.list_page.is_closed():
                self.thread_local.list_page = context.new_page()
            
            page = self.thread_local.list_page
            page.goto(list_url, timeout=60000, wait_until="domcontentloaded")
            
            # 检测并等待 Cloudflare 盾通过
            bypassed = self._wait_for_cloudflare_bypass(page)
            if not bypassed:
                logger.warning("[!] Cloudflare 解盾超时，列表页 %s 内容可能不完整", list_url)
            
            time.sleep(random.uniform(2, 4))
            return page.content()
        except Exception as e:
            logger.error("[-] Playwright 列表页 %s 抓取异常: %s", list_url, e)
            from config import is_proxy_manager_enabled
            if is_proxy_manager_enabled():
                manager = get_proxy_manager()
                if manager:
                    proxy_url = manager._thread_proxy_map.get(threading.get_ident())
                    if proxy_url:
                        manager.report_failure(proxy_url)
            self._destroy_thread_resources()
            return None

    def parse_list_page(self, list_page_html, page_num):
        """解析列表页卡片，提取出所有子页面的完整 URL 列表"""
        if not list_page_html:
            return []
        
        soup = BeautifulSoup(list_page_html, 'html.parser')
        articles = soup.select('div.content article')
        card_count = len(articles)
        
        if card_count == 0:
            logger.warning("[!] 警告：页面 %s 未找到任何卡片。", page_num)
            preview = list_page_html[:300].replace('\n', ' ')
            logger.warning("[!] 页面 HTML 预览: %s", preview)

        sub_urls = []
        list_url = self.get_list_url(page_num)
        for i, card in enumerate(articles):
            try:
                card_title_node = card.select_one('header h2 a')
                if not card_title_node:
                    continue
                sub_url_path = card_title_node.get('href')
                if not sub_url_path:
                    continue
                sub_urls.append(urljoin(list_url, sub_url_path))
            except Exception as e:
                logger.error("[-] 解析第 %s 个卡片链接时出错: %s", i + 1, e)
        return sub_urls

    # _save_pdf 逻辑已抽象到 base_crawler.py 和 utils/pdf_generator.py 中

    def _fetch_sub_page(self, sub_url):
        """获取子页面内容，支持自定义重试次数"""
        html_text = None
        current_url = sub_url
        sub_page = None
        last_err = None
        max_retries = getattr(self, 'max_retries', 3)
        
        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                logger.info("[*] 第 %s/%s 次重试抓取子页面: %s", attempt, max_retries, sub_url)
                if sub_page:
                    try:
                        sub_page.close()
                    except Exception:
                        pass
                    sub_page = None
                self._destroy_thread_resources()
                time.sleep(random.uniform(2.0, 4.0))
            try:
                no_proxy = (attempt == max_retries)
                _, _, context = self._get_thread_resources(no_proxy=no_proxy)
                sub_page = context.new_page()
                sub_page.goto(sub_url, timeout=60000, wait_until="domcontentloaded")
                
                # 检测并等待 Cloudflare 盾通过
                bypassed = self._wait_for_cloudflare_bypass(sub_page)
                if not bypassed:
                    logger.warning("[!] Cloudflare 解盾超时，子页面 %s 内容可能不完整", sub_url)
                
                try:
                    sub_page.wait_for_load_state("load", timeout=15000)
                except Exception as wait_err:
                    logger.warning("[!] 等待 load 状态超时，继续处理: %s", wait_err)
                
                # 等待网络静止让图片完全加载，对直接生成 PDF 至关重要
                try:
                    sub_page.wait_for_load_state("networkidle", timeout=15000)
                except Exception as wait_idle:
                    logger.info("[*] 等待 networkidle 状态超时，继续处理: %s", wait_idle)
                    
                time.sleep(random.uniform(2.0, 4.0))
                
                current_url = sub_page.url
                html_text = sub_page.content()
                return html_text, current_url, sub_page
            except Exception as err:
                last_err = err
                logger.error("[-] 使用 Playwright 抓取子页面 %s 异常 (第 %s/3 次): %s", sub_url, attempt, err)
                from config import is_proxy_manager_enabled
                if is_proxy_manager_enabled():
                    manager = get_proxy_manager()
                    if manager:
                        proxy_url = manager._thread_proxy_map.get(threading.get_ident())
                        if proxy_url:
                            manager.report_failure(proxy_url)
        
        # 所有重试均失败
        logger.error("[-] 子页面 %s 抓取失败（3 次重试均已耗尽）: %s", sub_url, last_err)
        self._destroy_thread_resources()
        return None, None, None

    def _parse_sub_page(self, html_text, current_url):
        """解析子页面元数据"""
        parsed_url = urlparse(current_url)
        is_external = self.target_domain not in parsed_url.netloc

        if is_external:
            logger.info("检测到跳转至外部网站: %s", current_url)
            soup = BeautifulSoup(html_text, 'html.parser')
            title = soup.title.string.strip() if soup.title and soup.title.string else "外部链接"
            pub_time = ""
            category = "外部跳转"
            res_link = current_url
            link_type = "外链"
        else:
            soup = BeautifulSoup(html_text, 'html.parser')
            title_node = soup.select_one('h1.article-title a') or soup.select_one('h1.article-title')
            title = title_node.get_text().strip() if title_node else "无标题"

            time_node = soup.select_one('header.article-header div.meta time')
            pub_time_raw = time_node.get_text().strip() if time_node else ""
            if not pub_time_raw and time_node and time_node.get('datetime'):
                pub_time_raw = time_node.get('datetime').strip()
            
            pub_time_cleaned = pub_time_raw.replace('С', '小').replace('ʱ', '时').replace('ǰ', '前')
            _, pub_time = parse_date(pub_time_cleaned)

            category = "Video"
            meta_spans = soup.select('header.article-header div.meta span')
            if meta_spans:
                for span in meta_spans:
                    text = span.get_text().strip()
                    if text and not text.isdigit() and "小时" not in text and "天" not in text and "Сʱ" not in text:
                        category = text
                        break

            content_div = soup.select_one('article.article-content')
            p_texts = []
            if content_div:
                for p in content_div.find_all('p'):
                    p_t = p.get_text().strip()
                    if p_t:
                        p_texts.append(p_t)
            
            cleaned_p_texts = [t for t in p_texts if t]
            if len(cleaned_p_texts) > 1:
                resource_patterns = [
                    r'^magnet:\?',
                    r'^ed2k://',
                    r'^thunder://',
                    r'^https?://',
                    r'提取码',
                    r'解压密码',
                    r'天翼'
                ]
                last_line = cleaned_p_texts[-1].lower()
                is_res = any(re.search(pat, last_line) for pat in resource_patterns)
                if not is_res:
                    cleaned_p_texts = cleaned_p_texts[:-1]

            res_link = "\n".join(cleaned_p_texts)
            link_type = ""

        data = self.clean_common_metadata(
            title=title,
            date_str=pub_time,
            resource_link=res_link,
            category=category,
            url=current_url,
            pdf_path=''
        )
        data['link_type'] = link_type
        
        return data

    def _generate_pdf_for_sub_page(self, current_url, pub_time, title):
        """为子页面生成 PDF"""
        if getattr(self, 'no_pdf', False):
            logger.info("-> 启用了 no_pdf 模式，跳过 PDF 渲染和保存")
            return ""
        
        pdf_date = pub_time if pub_time and pub_time != "Unknown_Date" else "Unknown_Date"
        saved_path = self.retry_generate_pdf(current_url, pdf_date, title)
        return saved_path

    def process_sub_page_if_needed(self, sub_url, idx):
        """处理单个子网页，提取信息并保存 PDF (纯 Playwright 实现)"""
        is_existing = False
        html_text, current_url, sub_page = self._fetch_sub_page(sub_url)

        if not html_text:
            if sub_page:
                try:
                    sub_page.close()
                except Exception:
                    pass
            logger.error("[-] 子页面 %s 抓取失败", sub_url)
            return False, None

        parsed_url = urlparse(current_url)
        is_external = self.target_domain not in parsed_url.netloc

        if current_url != sub_url:
            if self.db_manager.check_url_exists(current_url) and not self.is_test:
                logger.info("[%s] 重定向后的真实网址已存在，跳过抓取: %s", idx, current_url)
                if sub_page:
                    try:
                        sub_page.close()
                    except Exception:
                        pass
                return True, None

        try:
            data = self._parse_sub_page(html_text, current_url)
            logger.info("[%s] 页面抓取成功: %s | 分类: %s", idx, data['title'], data['category'])

            # 针对内部网页，使用当前的 Playwright 页面生成 PDF
            if not is_external:
                saved_path = self._generate_pdf_for_sub_page(current_url, data['publish_time'], data['title'])
                data['pdf_path'] = saved_path
            else:
                logger.info("-> 外部网站，已跳过 PDF 保存")

            return is_existing, data

        except Exception as e:
            logger.error("[-] 抓取子页面 %s 时发生错误: %s", sub_url, e)
            import traceback
            traceback.print_exc()
            return False, None
        finally:
            if sub_page:
                try:
                    sub_page.close()
                except Exception as close_err:
                    logger.error("[-] 关闭子页面失败: %s", close_err)
            time.sleep(random.uniform(1, 2))