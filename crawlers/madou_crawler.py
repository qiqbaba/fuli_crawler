import os
import re
import base64
import random
import time
import threading
from curl_cffi import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

from config import USER_AGENTS, is_local_mode
from crawlers.base_crawler import BaseCrawler
from utils.r2_uploader import get_r2_uploader
from utils.proxy_manager import get_proxy_string, get_proxy_dict


def sanitize_filename(filename):
    """清理文件名中的非法字符，移除表情符号及特殊变体字符防止编码问题"""
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
    filename = re.sub(r'[^\u0000-\uFFFF]', '', filename)
    filename = re.sub(r'[\u200b-\u200d\ufe00-\ufe0f\ufeff]', '', filename)
    return filename.strip()


# Playwright 反 webdriver 检测脚本
_STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['zh-CN', 'zh', 'en']
    });
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
}
"""


class MadouCrawler(BaseCrawler):
    def __init__(self, db_manager):
        super().__init__(db_manager, "madou")
        self.check_resource_link = True  # 启用磁力链接二次去重
        self.domains = [
            "hfc.232668.xyz"
        ]
        self.current_domain_idx = 0
        self.base_domain = f"https://{self.domains[self.current_domain_idx]}"
        self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
        self.current_class = "guochan"
        self.max_consecutive_existing = 15  # 连续抓到历史数据时早停
        self.r2_uploader = None
        self.thread_local = threading.local()
        self._active_resources = []
        self._resources_lock = threading.Lock()
        # 域名冷却机制
        self._domain_cooldown = {}
        self._cooldown_seconds = 60
        self._domain_lock = threading.Lock()

    def _build_headers(self, referer=None):
        """构造完整的浏览器请求头，模拟真实浏览器行为"""
        ua = random.choice(USER_AGENTS)
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        if referer:
            headers["Referer"] = referer
        else:
            headers["Referer"] = self.base_domain + "/"
        return headers

    def decrypt_html(self, raw_html):
        """解密目标网站动态混淆的 HTML"""
        candidates = re.findall(r'''['""]([A-Za-z0-9+/=]{1000,})['"]''', raw_html)
        if not candidates:
            return None
        
        longest_b64 = max(candidates, key=len)
        normal_b64 = longest_b64[::-1]
        
        try:
            return base64.b64decode(normal_b64).decode('utf-8')
        except Exception as e:
            print(f"[-] HTML 解密失败: {e}")
            return None

    def decrypt_title(self, encrypted_title_b64):
        """解密标题"""
        try:
            return base64.b64decode(encrypted_title_b64).decode('utf-8')
        except Exception as e:
            print(f"[-] 标题解密失败: {e}")
            return ""

    def on_start(self):
        """初始化 R2 上传器和代理管理器"""
        self.r2_uploader = get_r2_uploader()
        if self.r2_uploader:
            print("[*] Cloudflare R2 上传器已启用", flush=True)
        else:
            if is_local_mode():
                print("[*] 本地模式已激活，PDF 将保存到本地目录", flush=True)
            else:
                print("[*] 未配置 R2 环境变量，PDF 将保存到本地目录", flush=True)
        
        # 初始化代理管理器
        from config import is_proxy_manager_enabled
        print(f"[DEBUG] is_proxy_manager_enabled() = {is_proxy_manager_enabled()}", flush=True)
        if is_proxy_manager_enabled():
            print("[*] 代理管理器已启用，正在获取和验证代理IP...", flush=True)
            from utils.proxy_manager import get_proxy_manager
            from config import PROXY_VERIFY_WORKERS
            try:
                manager = get_proxy_manager()
                if manager:
                    manager.fetch_proxies(force=False)
                    manager.verify_proxies(force=False, max_workers=PROXY_VERIFY_WORKERS, test_url=self.base_domain)
                    stats = manager.get_stats()
                    print(f"[*] 代理管理器就绪: 总计 {stats['total']} 个，可用 {stats['working']} 个", flush=True)
            except Exception as e:
                print(f"[DEBUG] 初始化代理管理器时发生异常: {e}", flush=True)

    def cleanup_thread_resources(self):
        """释放当前工作线程持有的 Playwright 资源"""
        if hasattr(self.thread_local, "playwright"):
            self._recreate_thread_resources()

    def on_finish(self):
        """释放 Playwright 渲染资源"""
        print("[*] 正在释放主线程 Playwright 资源...")
        self.cleanup_thread_resources()
        
        with self._resources_lock:
            if self._active_resources:
                print(f"[!] 发现 {len(self._active_resources)} 个未被工作线程自主清理的残留资源，执行主线程兜底关闭...")
                for item in self._active_resources:
                    p, browser, context, _ = item
                    try:
                        if context:
                            context.close()
                    except:
                        pass
                    try:
                        if browser:
                            browser.close()
                    except:
                        pass
                    try:
                        p.stop()
                    except:
                        pass
                self._active_resources.clear()
            
        self.db_manager.commit()

    def _get_thread_resources(self):
        """获取当前线程特有的 Playwright 实例"""
        if not hasattr(self.thread_local, "playwright"):
            p = sync_playwright().start()
            
            from config import get_crawler_proxy, is_proxy_manager_enabled
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security",
                "--ignore-certificate-errors",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
            
            playwright_proxy = None
            crawler_proxy = get_crawler_proxy()
            if crawler_proxy:
                playwright_proxy = {"server": crawler_proxy}
            elif is_proxy_manager_enabled():
                proxy_url = get_proxy_string()
                if proxy_url:
                    playwright_proxy = {"server": proxy_url}
                
            browser = p.chromium.launch(headless=True, args=launch_args, proxy=playwright_proxy)
            
            ua = random.choice(USER_AGENTS)
            context = browser.new_context(
                viewport={'width': 1280, 'height': 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent=ua,
            )
            
            context.add_init_script(_STEALTH_JS)
            
            self.thread_local.playwright = p
            self.thread_local.browser = browser
            self.thread_local.context = context
            
            with self._resources_lock:
                self._active_resources.append((p, browser, context, None))
                
        return self.thread_local.playwright, self.thread_local.browser, self.thread_local.context

    def _recreate_thread_resources(self):
        """清理当前线程的 Playwright 资源，以便下一次重新创建"""
        p = getattr(self.thread_local, "playwright", None)
        browser = getattr(self.thread_local, "browser", None)
        context = getattr(self.thread_local, "context", None)
        
        try:
            if context:
                context.close()
        except:
            pass
        try:
            if browser:
                browser.close()
        except:
            pass
        try:
            if p:
                p.stop()
        except:
            pass
            
        with self._resources_lock:
            self._active_resources = [
                item for item in self._active_resources
                if item[0] != p
            ]
                
        if hasattr(self.thread_local, "playwright"):
            del self.thread_local.playwright
        if hasattr(self.thread_local, "browser"):
            del self.thread_local.browser
        if hasattr(self.thread_local, "context"):
            del self.thread_local.context

        # 清除当前线程的代理绑定，使下次创建时分配到新代理
        try:
            from utils.proxy_manager import get_proxy_manager
            from config import is_proxy_manager_enabled
            if is_proxy_manager_enabled():
                mgr = get_proxy_manager()
                if mgr:
                    tid = threading.get_ident()
                    with mgr._lock:
                        if tid in mgr._thread_proxy_map:
                            del mgr._thread_proxy_map[tid]
        except Exception:
            pass

    def _get_pdf_local_tmp_path(self, publish_date, title):
        """获取 PDF 本地路径 (带 source_name 尾缀)"""
        if self.r2_uploader:
            base = "/tmp/madou_pdfs"
        else:
            from config import PDF_BASE_DIR
            base = PDF_BASE_DIR

        year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
        save_dir = os.path.join(base, year)
        os.makedirs(save_dir, exist_ok=True)

        safe_title = sanitize_filename(title)
        base_filename = f"{publish_date}_{safe_title}_{self.source_name}"
        pdf_path = os.path.join(save_dir, f"{base_filename}.pdf")

        counter = 1
        while os.path.exists(pdf_path):
            pdf_path = os.path.join(save_dir, f"{base_filename}_{counter}.pdf")
            counter += 1

        return pdf_path

    def _save_pdf(self, target_url, publish_date, title):
        """直接用 Playwright 打开详情页并保存为 PDF"""
        if not publish_date or publish_date == "Unknown_Date":
            from datetime import datetime
            publish_date = datetime.now().strftime("%Y-%m-%d")
            
        local_path = self._get_pdf_local_tmp_path(publish_date, title)
        page = None
        try:
            _, _, context = self._get_thread_resources()
            page = context.new_page()
            page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(3.0)
            
            # 屏蔽广告
            try:
                page.evaluate("""
                    () => {
                        const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
                        adDivs.forEach(div => div.remove());
                        const bottomFloat = document.getElementById('bottom_float');
                        if (bottomFloat) {
                            bottomFloat.remove();
                        }
                    }
                """)
            except Exception as ad_err:
                print(f"[-] 屏蔽广告脚本执行失败: {ad_err}")

            page.pdf(
                path=local_path,
                format="A4",
                print_background=True,
                margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"}
            )
            page.close()
        except Exception as e:
            print(f"[-] PDF 生成失败: {e}")
            if page:
                try:
                    page.close()
                except:
                    pass
            return None

        if self.r2_uploader:
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            remote_key = f"pdfs/{year}/{os.path.basename(local_path)}"
            result = self.r2_uploader.upload_pdf(local_path, remote_key)
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except:
                    pass
            return result
        else:
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            rel_path = f"pdf/{year}/{os.path.basename(local_path)}"
            return rel_path.replace('\\', '/')

    def _rotate_domain(self):
        """轮换至下一个可用域名（带冷却机制），线程安全"""
        with self._domain_lock:
            failed_domain = self.domains[self.current_domain_idx]
            self._domain_cooldown[failed_domain] = time.time()
            
            now = time.time()
            for i in range(1, len(self.domains) + 1):
                candidate_idx = (self.current_domain_idx + i) % len(self.domains)
                candidate = self.domains[candidate_idx]
                last_fail = self._domain_cooldown.get(candidate, 0)
                if now - last_fail >= self._cooldown_seconds:
                    self.current_domain_idx = candidate_idx
                    self.base_domain = f"https://{self.domains[self.current_domain_idx]}"
                    self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
                    print(f"[!] 麻豆域名切换至: {self.base_domain}")
                    return
            
            # 所有域名都在冷却中
            if len(self.domains) > 1:
                min_wait = min(
                    self._cooldown_seconds - (now - self._domain_cooldown.get(d, 0))
                    for d in self.domains
                )
                wait_time = max(min_wait, 10) + random.uniform(2, 5)
                print(f"[!] 所有域名均在冷却中，等待 {wait_time:.1f} 秒...")
                time.sleep(wait_time)
            
            self.current_domain_idx = (self.current_domain_idx + 1) % len(self.domains)
            self.base_domain = f"https://{self.domains[self.current_domain_idx]}"
            self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
            self._domain_cooldown.pop(self.domains[self.current_domain_idx], None)
            print(f"[!] 冷却结束或直接重试，麻豆域名切换至: {self.base_domain}")

    def fetch_list_page(self, page_num):
        """请求列表页并解密 HTML，支持域名轮换重试"""
        for _ in range(len(self.domains)):
            url = self.base_list_url.format(self.current_class, page_num)
            headers = self._build_headers()

            for attempt in range(2):
                proxies = None
                from config import get_crawler_proxy, is_proxy_manager_enabled
                crawler_proxy = get_crawler_proxy()
                if crawler_proxy:
                    proxies = {"http": crawler_proxy, "https": crawler_proxy}
                elif is_proxy_manager_enabled():
                    proxies = get_proxy_dict()
                
                try:
                    response = requests.get(url, headers=headers, timeout=15, proxies=proxies, impersonate="chrome120")
                    if response.status_code == 200:
                        decrypted = self.decrypt_html(response.text)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            return decrypted
                    elif response.status_code == 403:
                        print(f"[!] 列表页返回 403，疑似触发反爬: {url}")
                        if proxies and is_proxy_manager_enabled():
                            from utils.proxy_manager import get_proxy_manager
                            manager = get_proxy_manager()
                            if manager and "http" in proxies:
                                manager.report_failure(proxies["http"])
                        break
                except Exception:
                    if proxies and is_proxy_manager_enabled():
                        from utils.proxy_manager import get_proxy_manager
                        manager = get_proxy_manager()
                        if manager and "http" in proxies:
                            manager.report_failure(proxies["http"])
                time.sleep(random.uniform(2.0, 4.0))
                
            print(f"[*] 使用 Playwright 兜底访问列表页: {url}")
            try:
                _, _, context = self._get_thread_resources()
                page = context.new_page()
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                time.sleep(random.uniform(2.0, 4.0))
                html = page.content()
                page.close()
                if "torrent-list" in html or "class=\"torrent-list\"" in html:
                    return html
                decrypted = self.decrypt_html(html)
                if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                    return decrypted
            except Exception as e:
                print(f"[-] Playwright 兜底抓取列表页异常: {e}")
                if is_proxy_manager_enabled():
                    from utils.proxy_manager import get_proxy_manager
                    manager = get_proxy_manager()
                    if manager:
                        proxy_url = manager._thread_proxy_map.get(threading.get_ident())
                        if proxy_url:
                            manager.report_failure(proxy_url)
                self._recreate_thread_resources()
                
            print(f"[!] 当前域名疑似被封，冷却等待后切换...")
            time.sleep(random.uniform(5.0, 10.0))
            self._rotate_domain()
            
        return None

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

    def process_sub_page_if_needed(self, raw_item, idx):
        """请求详情页，解析资源元数据并生成 PDF，支持域名轮换重试"""
        original_url = raw_item['url']
        is_existing = False

        time.sleep(random.uniform(2.0, 5.0))
        
        detail_html = None
        url = original_url
        
        for _ in range(len(self.domains)):
            from urllib.parse import urlparse, urlunparse
            parsed_url = urlparse(original_url)
            with self._domain_lock:
                current_base = self.base_domain
            parsed_base = urlparse(current_base)
            if any(d in parsed_url.netloc for d in self.domains):
                parsed_url = parsed_url._replace(netloc=parsed_base.netloc, scheme=parsed_base.scheme)
                url = urlunparse(parsed_url)
            else:
                url = original_url

            list_url = self.base_list_url.format(self.current_class, 1)
            headers = self._build_headers(referer=list_url)

            for attempt in range(2):
                proxies = None
                from config import get_crawler_proxy, is_proxy_manager_enabled
                crawler_proxy = get_crawler_proxy()
                if crawler_proxy:
                    proxies = {"http": crawler_proxy, "https": crawler_proxy}
                elif is_proxy_manager_enabled():
                    proxies = get_proxy_dict()
                
                try:
                    response = requests.get(url, headers=headers, timeout=15, proxies=proxies, impersonate="chrome120")
                    if response.status_code == 200:
                        decrypted = self.decrypt_html(response.text)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            detail_html = decrypted
                            break
                    elif response.status_code == 403:
                        print(f"[!] 详情页返回 403，疑似触发反爬: {url}")
                        break
                except Exception:
                    pass
                time.sleep(random.uniform(2.0, 4.0))

            if detail_html:
                break

            if not detail_html:
                try:
                    _, _, context = self._get_thread_resources()
                    page = context.new_page()
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    time.sleep(random.uniform(2.0, 4.0))
                    html = page.content()
                    page.close()
                    if "panel-title" in html or "torrent-description" in html:
                        detail_html = html
                        break
                    else:
                        decrypted = self.decrypt_html(html)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            detail_html = decrypted
                            break
                except Exception as e:
                    print(f"[-] Playwright 兜底抓取详情页异常 ({url}): {e}")

            if detail_html:
                break
                
            time.sleep(random.uniform(5.0, 10.0))
            self._rotate_domain()

        if not detail_html:
            print(f"[-] 详情页 {original_url} 抓取失败（最终尝试 URL: {url}）")
            return False, None

        # 提取磁力链接
        magnet_link = ""
        magnet_match = re.search(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+", detail_html)
        if magnet_match:
            magnet_link = magnet_match.group(0)
        else:
            magnet_match = re.search(r"magnet:\?[^\s'\"<>\)]+", detail_html)
            if magnet_match:
                magnet_link = magnet_match.group(0)

        if not magnet_link:
            print(f"[-] 在详情页中未找到磁力链接: {original_url}")
            return False, None

        size_val = ""
        res_format = ""
        date_str = raw_item['date_str']

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

        category_map = {
            "guochan": "国产",
            "oumei": "欧美"
        }
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

        # 处理 PDF 文件生成
        if self.is_test:
            print("-> 测试模式下跳过保存 PDF 以节省时间")
        else:
            saved_pdf = ""
            for attempt in range(1, 4):
                saved_pdf = self._save_pdf(url, date_str, raw_item['title'])
                if saved_pdf:
                    print(f"[PDF-SAVE] 标题: {raw_item['title']} -> PDF 路径: {saved_pdf}")
                    break
                else:
                    print(f"[-] [PDF-SAVE] 标题: {raw_item['title']} 生成 PDF 失败，进行第 {attempt}/3 次尝试")
                    if attempt < 3:
                        try:
                            self._recreate_thread_resources()
                        except Exception as recreate_err:
                            print(f"[!] 重构 Playwright 资源失败: {recreate_err}")
                        time.sleep(random.uniform(1.5, 3.0))
            data['pdf_path'] = saved_pdf

        return is_existing, data

    def run(self, is_test=False, start_page=1, end_page=1, max_workers=None, **kwargs):
        """麻豆爬虫入口，对两个板块依次进行爬取，支持断点续爬"""
        self.is_test = is_test
        self.quiet = kwargs.get('quiet', False)
        resume = kwargs.get('resume', False)
        self.resume = resume
        
        classes = ["guochan", "oumei"]

        print(f"[*] 启动 {self.source_name} 爬虫流程...")
        self.on_start()

        no_early_stop = kwargs.get('no_early_stop', False)
        if no_early_stop:
            print("[*] 禁用早停机制，将强制爬取指定范围内所有页面。")
            self.max_consecutive_existing = None
            self.max_consecutive_duplicate_pages = None

        if max_workers is None:
            max_workers = 10

        resume_class = None
        resume_page = start_page

        if resume:
            all_states = self.db_manager.load_crawl_state(self.source_name)
            if all_states:
                if "__all__" in all_states and all_states["__all__"].get("completed", False):
                    print(f"[*] 检测到 {self.source_name} 已完成全部爬取，跳过所有板块")
                    self.db_manager.clear_crawl_state(self.source_name)
                    print(f"[+] 已清除完成标记，下次运行将重新爬取")
                    try:
                        if is_test:
                            self._run_test_mode(start_page)
                        return
                    finally:
                        self.on_finish()
                    return
                
                for cls in classes:
                    state = all_states.get(cls)
                    if state:
                        saved_page = state["page_num"]
                        if saved_page <= end_page:
                            resume_class = cls
                            resume_page = saved_page
                            print(f"[*] 检测到板块 {cls} 爬取断点，从第 {resume_page} 页继续")
                            break
                        print(f"[断点续爬] 板块 {cls} 已完成，跳过")
                    else:
                        resume_class = cls
                        resume_page = start_page
                        print(f"[*] 板块 {cls} 无历史记录，从头开始爬取")
                        break
                else:
                    print(f"[*] 所有板块已完成，无需爬取")
                    try:
                        if is_test:
                            self._run_test_mode(start_page)
                        return
                    finally:
                        self.on_finish()
                    return
            else:
                print(f"[*] 未检测到历史断点，从头开始爬取")

        try:
            if is_test:
                self._run_test_mode(start_page)
                return

            for cls in classes:
                if resume and resume_class is not None:
                    cls_index = classes.index(cls)
                    resume_index = classes.index(resume_class)
                    if cls_index < resume_index:
                        print(f"\n[断点续爬] 板块 {cls} 已完成，跳过")
                        continue
                    if cls == resume_class:
                        actual_start = resume_page
                    else:
                        actual_start = start_page
                else:
                    actual_start = start_page

                self.current_class = cls
                print(f"\n[*] ================= 开始爬取麻豆板块: {cls} (起始页码: {actual_start}) =================")
                self._crawl_pages(actual_start, end_page, max_workers, class_name=cls)
                
                if resume and cls == resume_class:
                    resume_class = None
            
            if not is_test:
                self.db_manager.mark_source_completed(self.source_name)
                print(f"[+] {self.source_name} 所有板块爬取完成，已标记完成状态")

        except KeyboardInterrupt:
            print("\n[中断] 检测到用户手动停止运行 (Ctrl+C)")
        except Exception as e:
            print(f"\n[致命错误] 运行中发生未捕获的异常: {e}")
        finally:
            self.on_finish()
