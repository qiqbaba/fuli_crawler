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


class SejuCrawler(PlaywrightBaseCrawler):
    def __init__(self, db_manager):
        super().__init__(db_manager, "seju")
        self.base_url = "https://seju.life/page/{}/"
        self.target_domain = "seju.life"
        self.use_persistent_context = True
        self.proxy_test_url = "https://seju.life/"
        
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
        """检测并等待 Cloudflare Challenge (Just a moment...) 页面自动重定向通过。"""
        from config import is_local_mode
        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            try:
                title = page.title()
                url = page.url
                if "Just a moment..." in title or "cloudflare" in url or "cloudflare" in title.lower():
                    print(f"[*] 检测到 Cloudflare 盾页面，正在等待自动解盾... (当前 Title: '{title}')")
                    if is_local_mode():
                        print(f"[!] 【有头辅助提示】检测到验证码，若卡在此处，请在弹出的浏览器窗口中手动完成验证。")
                    time.sleep(1.5)
                else:
                    print(f"[+] 疑似已绕过 Cloudflare。当前 Title: '{title}', URL: {url}")
                    return True
            except Exception as e:
                print(f"[-] 检查 Cloudflare 状态时异常: {e}")
                time.sleep(1.5)
        
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
        print(f"[*] 正在访问列表页: {list_url}")
        try:
            _, _, context = self._get_thread_resources()
            if not hasattr(self.thread_local, "list_page") or self.thread_local.list_page.is_closed():
                self.thread_local.list_page = context.new_page()
            
            page = self.thread_local.list_page
            page.goto(list_url, timeout=60000, wait_until="domcontentloaded")
            
            # 检测并等待 Cloudflare 盾通过
            bypassed = self._wait_for_cloudflare_bypass(page)
            if not bypassed:
                print(f"[!] Cloudflare 解盾超时，列表页 {list_url} 内容可能不完整")
            
            time.sleep(random.uniform(2, 4))
            return page.content()
        except Exception as e:
            print(f"[-] Playwright 列表页 {list_url} 抓取异常: {e}")
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
            print(f"[!] 警告：页面 {page_num} 未找到任何卡片。")
            preview = list_page_html[:300].replace('\n', ' ')
            print(f"[!] 页面 HTML 预览: {preview}")

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
                print(f"[-] 解析第 {i+1} 个卡片链接时出错: {e}")
        return sub_urls

    # _save_pdf 逻辑已抽象到 base_crawler.py 和 utils/pdf_generator.py 中

    def process_sub_page_if_needed(self, sub_url, idx):
        """处理单个子网页，提取信息并保存 PDF (纯 Playwright 实现)"""
        is_existing = False
        html_text = None
        current_url = sub_url
        sub_page = None
        
        try:
            _, _, context = self._get_thread_resources()
            sub_page = context.new_page()
            sub_page.goto(sub_url, timeout=60000, wait_until="domcontentloaded")
            
            # 检测并等待 Cloudflare 盾通过
            bypassed = self._wait_for_cloudflare_bypass(sub_page)
            if not bypassed:
                print(f"[!] Cloudflare 解盾超时，子页面 {sub_url} 内容可能不完整")
            
            try:
                sub_page.wait_for_load_state("load", timeout=15000)
            except Exception as wait_err:
                print(f"[!] 等待 load 状态超时，继续处理: {wait_err}")
            
            # 等待网络静止让图片完全加载，对直接生成 PDF 至关重要
            try:
                sub_page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as wait_idle:
                print(f"[*] 等待 networkidle 状态超时，继续处理: {wait_idle}")
                
            time.sleep(random.uniform(2.0, 4.0))
            
            current_url = sub_page.url
            html_text = sub_page.content()
        except Exception as err:
            print(f"[-] 使用 Playwright 抓取子页面 {sub_url} 异常: {err}")
            from config import is_proxy_manager_enabled
            if is_proxy_manager_enabled():
                manager = get_proxy_manager()
                if manager:
                    proxy_url = manager._thread_proxy_map.get(threading.get_ident())
                    if proxy_url:
                        manager.report_failure(proxy_url)
            self._destroy_thread_resources()

        if not html_text:
            if sub_page:
                try:
                    sub_page.close()
                except Exception:
                    pass
            print(f"[-] 子页面 {sub_url} 抓取失败")
            return False, None

        parsed_url = urlparse(current_url)
        is_external = self.target_domain not in parsed_url.netloc

        if current_url != sub_url:
            if self.db_manager.check_url_exists(current_url) and not self.is_test:
                print(f"[{idx}] 重定向后的真实网址已存在，跳过抓取: {current_url}")
                if sub_page:
                    try:
                        sub_page.close()
                    except Exception:
                        pass
                return True, None

        try:
            if is_external:
                print(f"检测到跳转至外部网站: {current_url}")
                soup = BeautifulSoup(html_text, 'html.parser')
                title = soup.title.string.strip() if soup.title else "外部链接"
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

            print(f"[{idx}] 页面抓取成功: {title} | 分类: {category}")

            data = self.clean_common_metadata(
                title=title,
                date_str=pub_time,
                resource_link=res_link,
                category=category,
                url=current_url,
                pdf_path=''
            )
            data['link_type'] = link_type

            # 针对内部网页，使用当前的 Playwright 页面生成 PDF
            if not is_external:
                if getattr(self, 'no_pdf', False):
                    print("-> 启用了 no_pdf 模式，跳过 PDF 渲染和保存")
                else:
                    pdf_date = pub_time if pub_time and pub_time != "Unknown_Date" else "Unknown_Date"
                    saved_path = self.retry_generate_pdf(current_url, pdf_date, title, max_retries=3)
                    data['pdf_path'] = saved_path
            else:
                print("-> 外部网站，已跳过 PDF 保存")

            return is_existing, data

        except Exception as e:
            print(f"[-] 抓取子页面 {sub_url} 时发生错误: {e}")
            import traceback
            traceback.print_exc()
            return False, None
        finally:
            if sub_page:
                try:
                    sub_page.close()
                except Exception as close_err:
                    print(f"[-] 关闭子页面失败: {close_err}")
            time.sleep(random.uniform(1, 2))
