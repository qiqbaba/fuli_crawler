import os
import re
import shutil
import threading
import time
import random
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from curl_cffi import requests
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from config import USER_AGENTS, is_local_mode
from crawlers.base_crawler import BaseCrawler
from utils.r2_uploader import get_r2_uploader
from utils.date_parser import parse_date
from utils.proxy_manager import get_proxy_string, get_proxy_dict, get_proxy_manager


def sanitize_filename(filename):
    """清理文件名中的非法字符，移除表情符号及特殊变体字符防止编码问题"""
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
    filename = re.sub(r'[^\u0000-\uFFFF]', '', filename)
    filename = re.sub(r'[\u200b-\u200d\ufe00-\ufe0f\ufeff]', '', filename)
    return filename.strip()


class GcbtCrawler(BaseCrawler):
    def __init__(self, db_manager):
        super().__init__(db_manager, "gcbt")
        self.base_url = "https://gcbt.net/"
        self.target_domain = "gcbt.net"
        self.r2_uploader = None
        self.thread_local = threading.local()
        self._active_resources = []
        self._resources_lock = threading.Lock()
        # 默认早停阈值为连续 20 条已存在记录
        self.max_consecutive_existing = 20
        self.max_consecutive_duplicate_pages = 3

    def get_list_url(self, page_num):
        """获取指定页码的列表页 URL"""
        if page_num == 1:
            return self.base_url
        return urljoin(self.base_url, f"page/{page_num}")

    def _get_thread_resources(self):
        """获取当前线程特有的 Playwright, Browser 和 Context，用以渲染 PDF"""
        if not hasattr(self.thread_local, "playwright"):
            p = sync_playwright().start()
            
            local_mode = is_local_mode()
            headless = True
            
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security",
                "--ignore-certificate-errors",
            ]
            if headless:
                launch_args.extend([
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ])
            
            # 使用临时 Persistent Context
            profile_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "temp_profiles",
                f"profile_gcbt_{threading.get_ident()}_{random.randint(1000, 9999)}_{int(time.time())}"
            )
            os.makedirs(profile_dir, exist_ok=True)
            
            ua = random.choice(USER_AGENTS)
            playwright_proxy = None
            
            # 获取代理配置
            from config import get_crawler_proxy, is_proxy_manager_enabled
            crawler_proxy = get_crawler_proxy()
            if crawler_proxy:
                playwright_proxy = {"server": crawler_proxy}
            elif is_proxy_manager_enabled():
                proxy_url = get_proxy_string()
                if proxy_url:
                    playwright_proxy = {"server": proxy_url}
            
            context = None
            browser = None
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=headless,
                    channel="chrome",
                    args=launch_args,
                    user_agent=ua,
                    viewport={'width': 1280, 'height': 900},
                    bypass_csp=True,
                    proxy=playwright_proxy,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai"
                )
            except Exception as e:
                print(f"[*] 启动真实 Chrome 失败，回退到内置 Chromium: {e}")
                try:
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=headless,
                        args=launch_args,
                        user_agent=ua,
                        viewport={'width': 1280, 'height': 900},
                        bypass_csp=True,
                        proxy=playwright_proxy,
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai"
                    )
                    print(f"[+] 线程 {threading.get_ident()} 成功启动内置 Chromium 持久化上下文")
                except Exception as e2:
                    print(f"[-] 启动持久化上下文均失败: {e2}。尝试普通方式启动。")
            
            if context:
                browser = context.browser
            else:
                browser = p.chromium.launch(headless=headless, args=launch_args)
                context = browser.new_context(
                    user_agent=ua,
                    viewport={'width': 1280, 'height': 900},
                    proxy=playwright_proxy,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai"
                )
            
            try:
                stealth_config = Stealth()
                stealth_config.apply_stealth_sync(context)
            except Exception as stealth_err:
                print(f"[-] 应用 stealth 失败: {stealth_err}")
                
            try:
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                """)
            except Exception as script_err:
                print(f"[-] 注入伪装脚本失败: {script_err}")
            
            self.thread_local.playwright = p
            self.thread_local.browser = browser
            self.thread_local.context = context
            self.thread_local.profile_dir = profile_dir
            
            with self._resources_lock:
                self._active_resources.append((p, browser, context, profile_dir))
                
        return self.thread_local.playwright, self.thread_local.browser, self.thread_local.context

    def _recreate_thread_resources(self):
        """清理当前线程的 Playwright 资源，以便下一次重新创建"""
        print(f"[*] 线程 {threading.get_ident()} 正在释放 Playwright 资源...")
        p = getattr(self.thread_local, "playwright", None)
        browser = getattr(self.thread_local, "browser", None)
        context = getattr(self.thread_local, "context", None)
        profile_dir = getattr(self.thread_local, "profile_dir", None)
        
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
            
        if profile_dir and os.path.exists(profile_dir):
            try:
                shutil.rmtree(profile_dir)
            except:
                pass
                
        if hasattr(self.thread_local, "playwright"):
            del self.thread_local.playwright
        if hasattr(self.thread_local, "browser"):
            del self.thread_local.browser
        if hasattr(self.thread_local, "context"):
            del self.thread_local.context
        if hasattr(self.thread_local, "profile_dir"):
            del self.thread_local.profile_dir

    def _http_get(self, url, timeout=20):
        """使用 curl_cffi 模拟浏览器获取 URL，最多重试 3 次，都失败再跳过"""
        for attempt in range(1, 4):
            proxies = None
            crawler_proxy = None
            try:
                ua = random.choice(USER_AGENTS)
                headers = {"User-Agent": ua}
                
                from config import get_crawler_proxy, is_proxy_manager_enabled
                crawler_proxy = get_crawler_proxy()
                if crawler_proxy:
                    proxies = {"http": crawler_proxy, "https": crawler_proxy}
                elif is_proxy_manager_enabled():
                    proxies = get_proxy_dict()
                
                r = requests.get(url, headers=headers, impersonate="chrome120", timeout=timeout, proxies=proxies)
                r.encoding = 'utf-8'
                if r.status_code == 200:
                    return r.url, r.text
                else:
                    print(f"[-] HTTP 请求失败 ({url}) [第 {attempt}/3 次尝试]: 状态码 {r.status_code}")
                    if r.status_code in (403, 407, 502, 503, 504) and proxies and is_proxy_manager_enabled():
                        manager = get_proxy_manager()
                        if manager and "http" in proxies:
                            manager.report_failure(proxies["http"])
            except Exception as e:
                print(f"[-] HTTP 请求异常 ({url}) [第 {attempt}/3 次尝试]: {e}")
                if proxies and is_proxy_manager_enabled():
                    manager = get_proxy_manager()
                    if manager and "http" in proxies:
                        manager.report_failure(proxies["http"])
            
            if attempt < 3:
                time.sleep(random.uniform(1.0, 3.0))
                
        return url, None

    def cleanup_thread_resources(self):
        """生命周期钩子，释放当前工作线程持有的 Playwright 资源"""
        if hasattr(self.thread_local, "playwright"):
            self._recreate_thread_resources()

    def on_start(self):
        """初始化 R2 上传器 and 代理管理器"""
        self.r2_uploader = get_r2_uploader()
        if self.r2_uploader:
            print("[*] Cloudflare R2 上传器已启用")
        else:
            print("[*] PDF 将保存到本地目录")
        
        # 初始化代理管理器
        from config import is_proxy_manager_enabled
        if is_proxy_manager_enabled():
            print("[*] 代理管理器已启用，正在获取和验证代理IP...")
            manager = get_proxy_manager()
            if manager:
                from config import PROXY_VERIFY_WORKERS
                manager.fetch_proxies(force=True)
                manager.verify_proxies(
                    force=True, 
                    max_workers=PROXY_VERIFY_WORKERS, 
                    test_url="https://gcbt.net/download/8067.html", 
                    expected_content="90231538e5368bb8422500604f01cb25edfeedb4"
                )
                stats = manager.get_stats()
                print(f"[*] 代理管理器就绪: 总计 {stats['total']} 个，可用 {stats['working']} 个")

    def on_finish(self):
        """释放所有线程的 Playwright 资源并提交事务"""
        print("[*] 正在释放主线程 Playwright 资源...")
        self.cleanup_thread_resources()
        
        # 兜底清理其它残留的 Playwright 实例
        with self._resources_lock:
            if self._active_resources:
                print(f"[!] 发现 {len(self._active_resources)} 个残留资源，执行兜底关闭...")
                for item in self._active_resources:
                    p, browser, context, profile_dir = item
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
                    if profile_dir and os.path.exists(profile_dir):
                        try:
                            shutil.rmtree(profile_dir)
                        except:
                            pass
                self._active_resources.clear()
        self.db_manager.commit()

    def fetch_list_page(self, page_num):
        """抓取列表页内容"""
        list_url = self.get_list_url(page_num)
        print(f"[*] 正在访问列表页: {list_url}")
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

    def _get_pdf_local_tmp_path(self, publish_date, title):
        """获取 PDF 本地临时/持久化保存路径"""
        if self.r2_uploader:
            base = "/tmp/gcbt_pdfs"
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
            
            # 在网络层添加图片代理请求拦截器，在 Python 后台下载图片喂给浏览器以绕过防盗链和 GFW
            try:
                def img_router(route):
                    req_url = route.request.url
                    if "plugin/img_layer/data/" in req_url and "?src=" in req_url:
                        try:
                            import urllib.parse
                            real_url = urllib.parse.unquote(req_url.split("?src=")[1])
                            
                            # 获取爬虫全局配置代理
                            from config import get_crawler_proxy
                            p_url = get_crawler_proxy()
                            p_dict = {"http": p_url, "https": p_url} if p_url else None
                            
                            headers = {
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                            }
                            
                            import requests
                            r = requests.get(real_url, headers=headers, proxies=p_dict, timeout=15)
                            if r.status_code == 200:
                                route.fulfill(
                                    status=200,
                                    content_type=r.headers.get("Content-Type", "image/jpeg"),
                                    body=r.content
                                )
                                return
                        except Exception as route_err:
                            print(f"[!] 路由代理图片下载失败: {route_err}")
                    route.continue_()

                page.route("**/*", img_router)
            except Exception as route_setup_err:
                print(f"[!] 配置网络拦截路由异常: {route_setup_err}")
            
            # 1. 先访问首页以建立 Cookie/Session 并绕过重定向检测
            try:
                page.goto("https://gcbt.net/", timeout=20000, wait_until="domcontentloaded")
                time.sleep(1.5)
            except Exception as e_home:
                print(f"[!] 详情页前置访问首页异常 (不影响后续): {e_home}")
                
            # 2. 携带 Referer 访问真正的详情页
            page.goto(target_url, referer="https://gcbt.net/", timeout=30000, wait_until="domcontentloaded")
            time.sleep(3.0)

            # 2.5 强行模拟屏幕媒体排版，确保以网页真实外观进行 PDF 打印而绝不缺漏样式
            try:
                page.emulate_media(media="screen")
            except Exception as media_err:
                print(f"[!] 模拟 screen 媒体状态异常: {media_err}")

            # 自动滚动整页以触发所有图片的懒加载（Lazy Load），并替换可能存在的懒加载属性
            try:
                page.evaluate("""
                    async () => {
                        // 1. 替换页面上可能存在的懒加载图片属性
                        const replaceLazyAttrs = () => {
                            const images = document.querySelectorAll('img');
                            const lazyAttrs = ['data-src', 'data-original', 'data-lazy-src', 'data-src-webp', 'data-cfsrc', 'lazy-src'];
                            images.forEach(img => {
                                for (const attr of lazyAttrs) {
                                    const val = img.getAttribute(attr);
                                    if (val) {
                                        img.src = val;
                                        break;
                                    }
                                }
                            });
                        };
                        replaceLazyAttrs();

                        // 2. 逐步滚动到底部触发懒加载
                        await new Promise((resolve) => {
                            let totalHeight = 0;
                            const distance = 600;
                            const timer = setInterval(() => {
                                const scrollHeight = document.body.scrollHeight;
                                window.scrollBy(0, distance);
                                totalHeight += distance;
                                replaceLazyAttrs(); // 滚动时再次替换，以防动态生成

                                if (totalHeight >= scrollHeight || window.scrollY + window.innerHeight >= scrollHeight) {
                                    clearInterval(timer);
                                    resolve();
                                }
                            }, 150);
                        });

                        // 3. 滚回顶部，准备打印PDF
                        window.scrollTo(0, 0);
                        await new Promise(r => setTimeout(r, 500));

                        // 4. 等待所有图片完全加载
                        const images = Array.from(document.querySelectorAll('img'));
                        const imagePromises = images.map(img => {
                            if (img.complete) return Promise.resolve();
                            return new Promise(resolve => {
                                img.addEventListener('load', resolve);
                                img.addEventListener('error', resolve); // 即使加载失败也 resolve，避免卡死
                            });
                        });
                        await Promise.all(imagePromises);
                    }
                """)
                # print(f"[+] 线程 {threading.get_ident()} 完成页面全滚动及所有图片加载等待")
            except Exception as scroll_err:
                print(f"[!] 页面滚动或等待图片加载异常: {scroll_err}")

            # 尽量等待网络静止以防有其它动态资源在加载
            try:
                page.wait_for_load_state(state="networkidle", timeout=5000)
            except:
                pass

            # 3. 动态清除阻挡视线的“收藏发布页”弹窗及半透明黑色遮罩层
            try:
                page.evaluate("""
                    () => {
                        const selectors = [
                            '.layui-layer', '.layui-layer-shade',
                            '.modal', '.modal-backdrop',
                            '.swal-overlay', '.swal-modal', '.swal2-container',
                            '[id*="layui-layer"]'
                        ];
                        selectors.forEach(sel => {
                            const elms = document.querySelectorAll(sel);
                            elms.forEach(el => el.remove());
                        });
                        
                        // 恢复滚动条
                        if (document.body) document.body.style.overflow = 'auto';
                        if (document.documentElement) document.documentElement.style.overflow = 'auto';
                    }
                """)
            except Exception as eval_err:
                print(f"[-] 清理弹窗脚本执行异常: {eval_err}")



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
            return ""

        if self.r2_uploader:
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            remote_key = f"pdfs/{year}/{os.path.basename(local_path)}"
            result = self.r2_uploader.upload_pdf(local_path, remote_key)
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except:
                    pass
            return result  # R2 Key
        else:
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            rel_path = f"pdf/{year}/{os.path.basename(local_path)}"
            return rel_path.replace('\\', '/')

    def process_sub_page_if_needed(self, sub_url, idx):
        """处理详情页并转换数据与 PDF"""
        is_existing = False
        _, html_text = self._http_get(sub_url, timeout=25)
        
        if not html_text:
            print(f"[-] 子页面 {sub_url} 抓取失败")
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
        
        size = "None"
        size_match = re.search(r'【影片大小】\s*[:：]\s*([a-zA-Z0-9\.\s]+)', article_text)
        if size_match:
            size = size_match.group(1).strip()
            
        fmt = "None"
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

        if download_links:
            # 优先采用磁力链接
            magnet_links = [l for l in download_links if l.startswith('magnet:')]
            if magnet_links:
                resource_link = magnet_links[0]
            else:
                resource_link = download_links[0]
        else:
            resource_link = "None"
            
        print(f"[{idx}] 抓取成功: {title} | 发布时间: {pub_time} | 大小: {size} | 链接: {resource_link[:60]}...")
        
        # 6. 生成并渲染 PDF 文件（直接保存原网页，测试模式跳过）
        pdf_path = ''
        if not self.is_test and article:
            for attempt in range(1, 4):
                pdf_path = self._save_pdf(sub_url, pub_time, title)
                if pdf_path:
                    print(f"[PDF-SAVE] 网页地址: {sub_url} -> PDF 路径: {pdf_path}")
                    break
                else:
                    print(f"[-] [PDF-SAVE] 网页地址: {sub_url} 生成 PDF 失败，进行第 {attempt}/3 次尝试")
                    if attempt < 3:
                        try:
                            self._recreate_thread_resources()
                        except Exception as recreate_err:
                            print(f"[!] 重构 Playwright 资源失败: {recreate_err}")
                        time.sleep(random.uniform(1.5, 3.0))
            
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
