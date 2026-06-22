import os
import re
import threading
import time
import random
from urllib.parse import urlparse, urljoin
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from config import USER_AGENTS, is_local_mode
from crawlers.base_crawler import BaseCrawler
from utils.r2_uploader import get_r2_uploader


def sanitize_filename(filename):
    """清理文件名中的非法字符"""
    return re.sub(r'[\\/:*?"<>|]', '_', filename).strip()


class SejuCrawler(BaseCrawler):
    def __init__(self, db_manager):
        super().__init__(db_manager, "seju")
        self.base_url = "https://seju.life/page/{}/"
        self.target_domain = "seju.life"
        self.r2_uploader = None
        self.thread_local = threading.local()
        self._active_resources = []
        self._resources_lock = threading.Lock()

    def get_list_url(self, page_num):
        """获取指定页码的列表页 URL，针对第一页避免 301 重定向"""
        if page_num == 1:
            return "https://seju.life/"
        return self.base_url.format(page_num)

    def _get_thread_resources(self):
        """获取当前线程特有的 Playwright, Browser 和 Context"""
        if not hasattr(self.thread_local, "playwright"):
            p = sync_playwright().start()
            browser = p.chromium.launch(headless=True)
            # 在 GitHub Actions (Linux 运行环境) 下，优先使用 Linux Chrome UA 以防止操作系统特征检测冲突
            if os.environ.get("GITHUB_ACTIONS") == "true":
                ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            else:
                ua = random.choice(USER_AGENTS)
            context = browser.new_context(
                user_agent=ua,
                viewport={'width': 1920, 'height': 1080}
            )
            # 强化无头浏览器反爬特征隐藏，使用 Stealth
            try:
                stealth_config = Stealth()
                stealth_config.apply_stealth_sync(context)
            except Exception as stealth_err:
                print(f"[-] 应用 stealth 失败: {stealth_err}")
            
            self.thread_local.playwright = p
            self.thread_local.browser = browser
            self.thread_local.context = context
            
            with self._resources_lock:
                self._active_resources.append((p, browser, context))
                
        return self.thread_local.playwright, self.thread_local.browser, self.thread_local.context

    def _wait_for_cloudflare_bypass(self, page, timeout_sec=15):
        """
        检测并等待 Cloudflare Challenge (Just a moment...) 页面自动重定向通过。
        """
        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            try:
                title = page.title()
                url = page.url
                # 判断是否仍在 CF 挑战页
                if "Just a moment..." in title or "cloudflare" in url or "cloudflare" in title.lower():
                    print(f"[*] 检测到 Cloudflare 盾页面，正在等待自动解盾... (当前 Title: '{title}')")
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
        except:
            pass
        print(f"[-] 智能等待 Cloudflare 结束，但当前页面 Title 依然为: '{page.title()}'")
        return False

    def on_start(self):
        """初始化 R2 上传器"""
        self.r2_uploader = get_r2_uploader()
        if self.r2_uploader:
            print("[*] Cloudflare R2 上传器已启用")
        else:
            if is_local_mode():
                print("[*] 本地模式已激活，PDF 将保存到本地目录")
            else:
                print("[*] 未配置 R2 环境变量，PDF 将保存到本地目录")

    def on_finish(self):
        """释放所有线程的 Playwright 资源"""
        print("[*] 正在释放所有线程的 Playwright 资源...")
        with self._resources_lock:
            for p, browser, context in self._active_resources:
                try:
                    browser.close()
                except Exception as e:
                    print(f"[-] 关闭浏览器失败: {e}")
                try:
                    p.stop()
                except Exception as e:
                    print(f"[-] 停止 Playwright 失败: {e}")
            self._active_resources.clear()
        self.db_manager.commit()

    def fetch_list_page(self, page_num):
        """加载列表页并返回当前 page 对象"""
        list_url = self.get_list_url(page_num)
        print(f"[*] 正在访问列表页: {list_url}")
        try:
            _, _, context = self._get_thread_resources()
            if not hasattr(self.thread_local, "list_page") or self.thread_local.list_page.is_closed():
                self.thread_local.list_page = context.new_page()
            self.thread_local.list_page.goto(list_url, timeout=60000, wait_until="domcontentloaded")
            
            # 检测并等待 Cloudflare 盾通过
            self._wait_for_cloudflare_bypass(self.thread_local.list_page)
            
            time.sleep(random.uniform(2, 4))
            return self.thread_local.list_page
        except Exception as e:
            print(f"[-] 列表页 {list_url} 加载失败: {e}")
            return None

    def parse_list_page(self, list_page, page_num):
        """解析列表页卡片，提取出所有子页面的完整 URL 列表"""
        articles_locator = list_page.locator('//div[@class="content"]/article')
        card_count = articles_locator.count()
        if card_count == 0:
            try:
                title = list_page.title()
                print(f"[!] 警告：页面 {page_num} 未找到任何卡片。当前页面 Title 为: '{title}'")
                preview = list_page.content()[:300].replace('\n', ' ')
                print(f"[!] 页面 HTML 预览: {preview}")
            except Exception as e:
                print(f"[-] 获取页面调试信息失败: {e}")

        sub_urls = []
        list_url = self.get_list_url(page_num)
        for i in range(card_count):
            try:
                card = articles_locator.nth(i)
                card_title_node = card.locator('xpath=./header/h2/a')
                sub_url_path = card_title_node.get_attribute('href')
                if not sub_url_path:
                    continue
                sub_urls.append(urljoin(list_url, sub_url_path))
            except Exception as e:
                print(f"[-] 解析第 {i+1} 个卡片链接时出错: {e}")
        return sub_urls

    def _get_pdf_local_tmp_path(self, publish_date, title):
        """
        获取 PDF 本地临时保存路径。
        - 云端（配置了 R2）：使用 /tmp/seju_pdfs/ 临时目录
        - 本地（未配置 R2）：使用 config.PDF_BASE_DIR 持久目录
        """
        if self.r2_uploader:
            # 云端临时目录
            base = "/tmp/seju_pdfs"
        else:
            from config import PDF_BASE_DIR
            base = PDF_BASE_DIR

        year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
        save_dir = os.path.join(base, year)
        os.makedirs(save_dir, exist_ok=True)

        safe_title = sanitize_filename(title)
        base_filename = f"{publish_date}_{safe_title}"
        pdf_path = os.path.join(save_dir, f"{base_filename}.pdf")

        counter = 1
        while os.path.exists(pdf_path):
            pdf_path = os.path.join(save_dir, f"{base_filename}_{counter}.pdf")
            counter += 1

        return pdf_path

    def _save_pdf(self, sub_page, publish_date, title):
        """
        保存 PDF：先写本地临时文件，若配置了 R2 则上传并返回 R2 Key，
        否则返回本地路径。失败时返回空字符串。
        """
        local_path = self._get_pdf_local_tmp_path(publish_date, title)
        try:
            sub_page.pdf(path=local_path, format="A4", print_background=True)
            print(f"[+] PDF 已保存至临时路径: {local_path}")
        except Exception as e:
            print(f"[-] PDF 生成失败: {e}")
            return ""

        if self.r2_uploader:
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            remote_key = f"pdfs/{year}/{os.path.basename(local_path)}"
            result = self.r2_uploader.upload_pdf(local_path, remote_key)
            return result  # R2 Key 或空字符串（失败时本地文件保留）
        else:
            # 返回相对路径，统一格式为 pdf/year/filename.pdf，并使用正斜杠
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            rel_path = f"pdf/{year}/{os.path.basename(local_path)}"
            return rel_path.replace('\\', '/')

    def process_sub_page_if_needed(self, sub_url, idx):
        """
        处理单个子页面的抓取、信息提取、PDF 保存/上传。
        raw_item 在这里即为 sub_url。
        """
        # 1. 检查子页面链接是否已存在
        is_existing = self.db_manager.check_url_exists(sub_url)
        if is_existing and not self.is_test:
            print(f"[{idx}] 网址已存在数据库中，跳过抓取: {sub_url}")
            return True, None

        sub_page = None
        try:
            _, _, context = self._get_thread_resources()
            sub_page = context.new_page()
            sub_page.goto(sub_url, timeout=60000, wait_until="domcontentloaded")
            
            # 检测并等待 Cloudflare 盾通过
            self._wait_for_cloudflare_bypass(sub_page)
            
            sub_page.wait_for_load_state("load", timeout=10000)
            time.sleep(random.uniform(1.5, 3.5))

            current_url = sub_page.url
            parsed_url = urlparse(current_url)
            is_external = self.target_domain not in parsed_url.netloc

            # 2. 检查重定向后的真实链接是否已存在
            if current_url != sub_url:
                if self.db_manager.check_url_exists(current_url) and not self.is_test:
                    print(f"[{idx}] 重定向后的真实网址已存在，跳过抓取: {current_url}")
                    return True, None

            if is_external:
                print(f"检测到跳转至外部网站: {current_url}")
                title = sub_page.title()
                pub_time = ""
                category = "外部跳转"
                res_link = current_url
                link_type = "外链"
                pdf_path = ""
            else:
                title_loc = sub_page.locator('//h1[@class="article-title"]/a')
                title = title_loc.text_content().strip() if title_loc.count() > 0 else "无标题"

                time_loc = sub_page.locator('//header[@class="article-header"]/div[@class="meta"]/time')
                pub_time = time_loc.text_content().strip() if time_loc.count() > 0 else ""

                cat_loc = sub_page.locator('//header[@class="article-header"]/div[@class="meta"]/span[1]')
                category = cat_loc.text_content().strip() if cat_loc.count() > 0 else ""

                p_texts = sub_page.locator('//article[@class="article-content"]//p').all_text_contents()
                cleaned_p_texts = [t.strip() for t in p_texts if t.strip()]

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

            # 使用基类中的通用元数据清洗逻辑
            data = self.clean_common_metadata(
                title=title,
                date_str=pub_time,
                resource_link=res_link,
                category=category,
                url=current_url,
                pdf_path=''
            )
            data['link_type'] = link_type

            # 针对内部网页，生成 PDF 并上传 R2（或保存本地）
            if not is_external:
                if self.is_test:
                    print("-> 测试模式下跳过保存 PDF 以节省时间")
                else:
                    publish_date = data['publish_time']
                    saved_path = self._save_pdf(sub_page, publish_date, title)
                    data['pdf_path'] = saved_path
            else:
                print("-> 外部网站，已跳过 PDF 保存")

            return is_existing, data

        except Exception as e:
            print(f"[-] 抓取子页面 {sub_url} 时发生错误: {e}")
            return False, None
        finally:
            if sub_page:
                try:
                    sub_page.close()
                except Exception as e:
                    print(f"[-] 关闭子页面失败: {e}")
            time.sleep(random.uniform(1, 2))
