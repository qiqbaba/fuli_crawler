import os
import re
import base64
import random
import time
import threading
import shutil
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

from config import USER_AGENTS, is_local_mode
from crawlers.base_crawler import BaseCrawler
from utils.r2_uploader import get_r2_uploader
from utils.proxy_manager import get_proxy_string, get_proxy_dict


def sanitize_filename(filename):
    """清理文件名中的非法字符，移除表情符号及特殊变体字符防止编码问题"""
    # 替换 Windows 文件名非法字符
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
    # 移除非 BMP 字符（如 Emoji 等 Unicode 码点大于 0xFFFF 的字符）
    filename = re.sub(r'[^\u0000-\uFFFF]', '', filename)
    # 移除特殊的不可见控制字符和变体选择器
    filename = re.sub(r'[\u200b-\u200d\ufe00-\ufe0f\ufeff]', '', filename)
    return filename.strip()



# Playwright 反 webdriver 检测脚本
_STEALTH_JS = """
() => {
    // 隐藏 webdriver 标志
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    // 伪造 plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });
    // 伪造 languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['zh-CN', 'zh', 'en']
    });
    // 删除 Chrome 自动化相关属性
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
}
"""


class DatangCrawler(BaseCrawler):
    def __init__(self, db_manager):
        # source_name 设为 datang
        super().__init__(db_manager, "datang")
        self.domains = [
            "tms.883835.xyz",
            "iut.983292.xyz",
            "nrt.322953.xyz",
            "eta.389838.xyz",
            "fip.553892.xyz"
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
        # 全局限速器：确保所有线程的请求间隔不低于 1 秒
        self._rate_lock = threading.Lock()
        self._last_request_time = 0
        # 域名冷却机制：记录每个域名最后失败时间
        self._domain_cooldown = {}
        self._cooldown_seconds = 60  # 域名冷却 60 秒

    def _rate_limit(self):
        """线程安全的全局限速，确保请求间隔不低于 1 秒"""
        with self._rate_lock:
            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < 1.0:
                wait = 1.0 - elapsed + random.uniform(0.1, 0.5)
                time.sleep(wait)
            self._last_request_time = time.time()

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
        # 寻找最长的 Base64-like 字符候选（仅含 Base64 字符且长度大于 1000）
        candidates = re.findall(r'''['""]([A-Za-z0-9+/=]{1000,})['"]''', raw_html)
        if not candidates:
            return None
        
        longest_b64 = max(candidates, key=len)
        # 反转 Base64 字符串
        normal_b64 = longest_b64[::-1]
        
        try:
            return base64.b64decode(normal_b64).decode('utf-8')
        except Exception as e:
            print(f"[-] HTML 解密失败: {e}")
            return None

    def decrypt_title(self, encrypted_title_b64):
        """解密详情页或列表页的加密标题"""
        try:
            return base64.b64decode(encrypted_title_b64).decode('utf-8')
        except Exception as e:
            print(f"[-] 标题解密失败: {e}")
            return ""

    def on_start(self):
        """初始化 R2 上传器和代理管理器"""
        self.r2_uploader = get_r2_uploader()
        if self.r2_uploader:
            print("[*] Cloudflare R2 上传器已启用")
        else:
            if is_local_mode():
                print("[*] 本地模式已激活，PDF 将保存到本地目录")
            else:
                print("[*] 未配置 R2 环境变量，PDF 将保存到本地目录")
        
        # 初始化代理管理器
        from config import is_proxy_manager_enabled
        if is_proxy_manager_enabled():
            print("[*] 代理管理器已启用，正在获取和验证代理IP...")
            from utils.proxy_manager import get_proxy_manager
            from config import PROXY_VERIFY_WORKERS
            manager = get_proxy_manager()
            if manager:
                manager.fetch_proxies(force=True)
                manager.verify_proxies(force=True, max_workers=PROXY_VERIFY_WORKERS)
                stats = manager.get_stats()
                print(f"[*] 代理管理器就绪: 总计 {stats['total']} 个，可用 {stats['working']} 个")

    def on_finish(self):
        """释放 Playwright 渲染资源"""
        print("[*] 正在释放 datang 爬虫 Playwright 资源...")
        with self._resources_lock:
            for item in self._active_resources:
                p, browser, context, _ = item
                try:
                    if context:
                        context.close()
                except Exception:
                    pass
                try:
                    if browser:
                        browser.close()
                except Exception:
                    pass
                try:
                    p.stop()
                except Exception:
                    pass
            self._active_resources.clear()

        # Bug 2 修复：thread_local 属性是线程私有的，在主线程中 delattr 工作线程的
        # thread_local 完全无效（每个线程只能看到自己的 thread_local 属性）。
        # 工作线程自己的 thread_local 清理已由 _recreate_thread_resources 负责。
        # 这里正确做法是什么都不做，资源已通过 _active_resources 循环关闭。
            
        self.db_manager.commit()

    def _get_thread_resources(self):
        """获取当前线程特有的 Playwright 实例（带反检测增强）"""
        if not hasattr(self.thread_local, "playwright"):
            p = sync_playwright().start()
            
            from config import get_crawler_proxy, is_proxy_manager_enabled
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security",
                "--ignore-certificate-errors",
                "--disable-blink-features=AutomationControlled",
            ]
            
            playwright_proxy = None
            # 优先使用运行时配置的代理
            crawler_proxy = get_crawler_proxy()
            if crawler_proxy:
                playwright_proxy = {"server": crawler_proxy}
            elif is_proxy_manager_enabled():
                # 使用代理管理器获取随机代理
                proxy_url = get_proxy_string()
                if proxy_url:
                    playwright_proxy = {"server": proxy_url}
                    print(f"[+] 线程 {threading.get_ident()} 配置 Playwright 代理 (代理管理器): {proxy_url}")
                
            browser = p.chromium.launch(headless=True, args=launch_args, proxy=playwright_proxy)
            
            # 使用随机 UA 并设置完整的浏览器上下文参数
            ua = random.choice(USER_AGENTS)
            context = browser.new_context(
                viewport={'width': 1280, 'height': 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent=ua,
            )
            
            # 注入反 webdriver 检测脚本（对该 context 的所有页面生效）
            context.add_init_script(_STEALTH_JS)
            
            self.thread_local.playwright = p
            self.thread_local.browser = browser
            self.thread_local.context = context
            
            with self._resources_lock:
                self._active_resources.append((p, browser, context, None))
                
        return self.thread_local.playwright, self.thread_local.browser, self.thread_local.context

    def _recreate_thread_resources(self):
        """清理当前线程的 Playwright 资源，以便下一次重新创建"""
        print(f"[*] 线程 {threading.get_ident()} 检测到代理失效，正在重构 Playwright 资源...")
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

    def _get_pdf_local_tmp_path(self, publish_date, title):
        """获取 PDF 本地路径 (带 source_name 尾缀)"""
        if self.r2_uploader:
            base = "/tmp/datang_pdfs"
        else:
            from config import PDF_BASE_DIR
            base = PDF_BASE_DIR

        year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
        save_dir = os.path.join(base, year)
        os.makedirs(save_dir, exist_ok=True)

        safe_title = sanitize_filename(title)
        # 在文件名后加入 datang
        base_filename = f"{publish_date}_{safe_title}_{self.source_name}"
        pdf_path = os.path.join(save_dir, f"{base_filename}.pdf")

        counter = 1
        while os.path.exists(pdf_path):
            pdf_path = os.path.join(save_dir, f"{base_filename}_{counter}.pdf")
            counter += 1

        return pdf_path

    def _save_pdf(self, target_url, publish_date, title):
        """直接用 Playwright 打开详情页并保存为 PDF"""
        # 确保日期有效，避免生成 Unknown_Date 文件名
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
                        // 1. 移除面包屑导航上方的广告容器
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
                        
                        // 2. 移除所有固定高度的广告容器
                        const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"]');
                        adDivs.forEach(div => div.remove());
                        
                        // 3. 移除底部悬浮广告
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
            print(f"[+] PDF 已保存至临时路径: {local_path}")
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
        """轮换至下一个可用域名（带冷却机制）"""
        # 记录当前域名的冷却时间
        failed_domain = self.domains[self.current_domain_idx]
        self._domain_cooldown[failed_domain] = time.time()
        
        # 寻找不在冷却中的域名
        now = time.time()
        for i in range(1, len(self.domains) + 1):
            candidate_idx = (self.current_domain_idx + i) % len(self.domains)
            candidate = self.domains[candidate_idx]
            last_fail = self._domain_cooldown.get(candidate, 0)
            if now - last_fail >= self._cooldown_seconds:
                self.current_domain_idx = candidate_idx
                self.base_domain = f"https://{self.domains[self.current_domain_idx]}"
                self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
                print(f"[!] 大唐BT域名切换至: {self.base_domain}")
                return
        
        # 所有域名都在冷却中，等待最短冷却时间后使用
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
        # 清除该域名的冷却记录
        self._domain_cooldown.pop(self.domains[self.current_domain_idx], None)
        print(f"[!] 冷却结束，大唐BT域名切换至: {self.base_domain}")

    def fetch_list_page(self, page_num):
        """请求列表页并解密 HTML，支持域名轮换重试"""
        for _ in range(len(self.domains)):
            url = self.base_list_url.format(self.current_class, page_num)
            headers = self._build_headers()
            
            # 全局限速
            self._rate_limit()
            
            # Bug 10 修复：删除外层冗余死代码，代理配置统一在内层循环中按需获取
            # 1. 优先使用 requests
            for attempt in range(2):
                # 每次重试重新获取代理配置（确保拿到最新可用代理）
                proxies = None
                from config import get_crawler_proxy, is_proxy_manager_enabled
                crawler_proxy = get_crawler_proxy()
                if crawler_proxy:
                    proxies = {"http": crawler_proxy, "https": crawler_proxy}
                elif is_proxy_manager_enabled():
                    proxies = get_proxy_dict()
                
                try:
                    response = requests.get(url, headers=headers, timeout=15, proxies=proxies)
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
                        break  # 跳出重试，直接换域名
                except Exception as e:
                    if proxies and is_proxy_manager_enabled():
                        from utils.proxy_manager import get_proxy_manager
                        manager = get_proxy_manager()
                        if manager and "http" in proxies:
                            manager.report_failure(proxies["http"])
                time.sleep(random.uniform(2.0, 4.0))
                
            # 2. 兜底使用 Playwright
            print(f"[*] 使用 Playwright 兜底访问列表页: {url}")
            try:
                _, _, context = self._get_thread_resources()
                page = context.new_page()
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                time.sleep(random.uniform(2.0, 4.0))
                html = page.content()
                page.close()
                if "class=\"bt_ul\"" in html or "class='bt_ul'" in html or "bt_ul" in html:
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
                
            # 当前域名请求失败或解密结果为虚假发布页，冷却等待后轮换域名重试
            print(f"[!] 当前域名疑似被封，冷却等待后切换...")
            time.sleep(random.uniform(8.0, 15.0))
            self._rotate_domain()
            
        return None

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

    def process_sub_page_if_needed(self, raw_item, idx):
        """请求详情页，解析资源元数据并生成 PDF，支持域名轮换重试"""
        original_url = raw_item['url']
        is_existing = self.db_manager.check_url_exists(original_url)
        if is_existing and not self.is_test:
            return True, None

        # 每个详情页请求前随机延迟，模拟人类浏览行为
        time.sleep(random.uniform(2.0, 5.0))
        
        detail_html = None
        url = original_url  # Bug 6 修复：提前初始化 url，避免循环外 NameError
        
        # 最多尝试轮换所有域名的次数
        for _ in range(len(self.domains)):
            # 动态替换域名为当前的最优域名
            from urllib.parse import urlparse, urlunparse
            parsed_url = urlparse(original_url)
            parsed_base = urlparse(self.base_domain)
            if any(d in parsed_url.netloc for d in self.domains) or "685835.xyz" in parsed_url.netloc:
                parsed_url = parsed_url._replace(netloc=parsed_base.netloc, scheme=parsed_base.scheme)
                url = urlunparse(parsed_url)
            else:
                url = original_url

            # 使用完整浏览器请求头
            list_url = self.base_list_url.format(self.current_class, 1)
            headers = self._build_headers(referer=list_url)
            
            # 全局限速
            self._rate_limit()
            
            # Bug 10 修复：删除外层冗余死代码，代理配置统一在内层循环中按需获取
            # 1. 优先使用 requests
            for attempt in range(2):
                # 每次重试重新获取代理配置（确保拿到最新可用代理）
                proxies = None
                from config import get_crawler_proxy, is_proxy_manager_enabled
                crawler_proxy = get_crawler_proxy()
                if crawler_proxy:
                    proxies = {"http": crawler_proxy, "https": crawler_proxy}
                elif is_proxy_manager_enabled():
                    proxies = get_proxy_dict()
                
                try:
                    response = requests.get(url, headers=headers, timeout=15, proxies=proxies)
                    if response.status_code == 200:
                        decrypted = self.decrypt_html(response.text)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            detail_html = decrypted
                            break
                    elif response.status_code == 403:
                        print(f"[!] 详情页返回 403，疑似触发反爬: {url}")
                        break  # 跳出重试，直接换域名
                except Exception:
                    pass
                time.sleep(random.uniform(2.0, 4.0))

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
                    if "video-description" in html or "class=\"video-description\"" in html:
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
                
            # 当前域名请求失败或解密结果为虚假发布页，冷却等待后轮换域名重试
            time.sleep(random.uniform(5.0, 10.0))
            self._rotate_domain()

        if not detail_html:
            # Bug 6 修复：使用 original_url 而非循环内局部 url，确保错误日志准确
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

        # 匹配详细发布时间
        date_match = re.search(r"【发布时间】：\s*(\d{4}-\d{2}-\d{2})", detail_html)
        if date_match:
            date_str = date_match.group(1)

        # 匹配影片大小
        size_match = re.search(r"【影片大小】：\s*([^<]+)", detail_html)
        if size_match:
            size_val = size_match.group(1).strip()

        # 匹配影片格式
        format_match = re.search(r"【影片格式】：\s*([^<]+)", detail_html)
        if format_match:
            res_format = format_match.group(1).strip()

        category_map = {
            "guochan": "国产",
            "wuma": "无码",
            "oumei": "欧美"
        }
        category = category_map.get(raw_item['class_name'], raw_item['class_name'])

        # 数据结构清洗
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
            saved_pdf = self._save_pdf(url, date_str, raw_item['title'])
            data['pdf_path'] = saved_pdf if saved_pdf else ''

        return is_existing, data

    def run(self, is_test=False, start_page=1, end_page=1, max_workers=None, **kwargs):
        """大唐BT爬虫入口，对三个板块依次进行爬取"""
        self.is_test = is_test
        classes = ["guochan", "wuma", "oumei"]
        
        for cls in classes:
            self.current_class = cls
            print(f"\n[*] ================= 开始爬取大唐BT板块: {cls} =================")
            super().run(is_test=is_test, start_page=start_page, end_page=end_page, max_workers=max_workers, **kwargs)
