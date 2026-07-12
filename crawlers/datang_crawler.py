import os
import re
import base64
import random
import time
import threading
from curl_cffi import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from config import USER_AGENTS, is_local_mode
from crawlers.base_crawler import PlaywrightBaseCrawler, DomainRotationMixin, DecryptMixin
from utils.proxy_manager import get_proxy_string, get_proxy_dict
from utils.metadata_parser import sanitize_filename


class DatangCrawler(PlaywrightBaseCrawler, DomainRotationMixin, DecryptMixin):
    def __init__(self, db_manager):
        # source_name 设为 datang
        super().__init__(db_manager, "datang")
        self.check_resource_link = True  # 启用磁力链接二次去重
        self.main_domain = "https://dtbt7.com"
        self.domain_pattern = r'([a-z]{2,5}\.\d{5,7}\.xyz)'
        self.domains = []
        self.current_domain_idx = 0
        self.base_domain = self.main_domain
        self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
        self.current_class = "guochan"
        self.max_consecutive_existing = 15  # 连续抓到历史数据时早停
        # 域名冷却机制：记录每个域名最后失败时间
        self._domain_cooldown = {}
        self._cooldown_seconds = 60  # 域名冷却 60 秒
        # 域名轮换线程安全锁
        self._domain_lock = threading.Lock()

        # 尝试从本地缓存加载之前发现的最新域名
        self._load_domains_from_cache()

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



    def on_start(self):
        """初始化 R2 上传器和代理管理器"""
        from utils.r2_uploader import get_r2_uploader
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
                print(f"[DEBUG] get_proxy_manager() 实例: {manager}", flush=True)
                if manager:
                    print("[DEBUG] 开始 fetch_proxies...", flush=True)
                    manager.fetch_proxies(force=False)
                    print("[DEBUG] 开始 verify_proxies...", flush=True)
                    manager.verify_proxies(force=False, max_workers=PROXY_VERIFY_WORKERS, test_url=self.base_domain)
                    stats = manager.get_stats()
                    print(f"[*] 代理管理器就绪: 总计 {stats['total']} 个，可用 {stats['working']} 个", flush=True)
                else:
                    print("[DEBUG] manager 为 None，未初始化", flush=True)
            except Exception as e:
                print(f"[DEBUG] 初始化代理管理器时发生异常: {e}", flush=True)
                import traceback
                traceback.print_exc()



    def _save_pdf(self, target_url, publish_date, title):
        """直接用 Playwright 打开详情页并保存为 PDF"""
        if getattr(self, 'no_pdf', False):
            return ""
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
        except Exception as e:
            print(f"[-] PDF 生成失败: {e}")
            if page:
                try:
                    page.close()
                except:
                    pass
            return None


        return self._upload_or_return_pdf_path(local_path, publish_date)



    def fetch_list_page(self, page_num, retry_with_main=True):
        """请求列表页并解密 HTML，支持域名轮换重试和自动域名发现"""
        if not self.domains:
            if not self._fetch_domains_from_main_station():
                print("[!] 域名列表为空且从主站获取失败，无法继续抓取", flush=True)
                return None

        for _ in range(len(self.domains)):
            url = self.base_list_url.format(self.current_class, page_num)
            headers = self._build_headers()
            redirect_content = None  # 追踪跳转页面内容，用于提取最新域名

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
                    response = requests.get(url, headers=headers, timeout=15, proxies=proxies, impersonate="chrome120")
                    if response.status_code == 200:
                        decrypted = self.decrypt_html(response.text)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            return decrypted
                        # 记录跳转页面内容，后续用于提取新域名
                        if decrypted and "正在检测最新可用线路" in decrypted:
                            redirect_content = decrypted
                        elif "正在检测最新可用线路" in response.text:
                            redirect_content = response.text
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
                # 记录跳转页面内容
                if decrypted and "正在检测最新可用线路" in decrypted:
                    redirect_content = decrypted
                elif "正在检测最新可用线路" in html:
                    redirect_content = html
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
                
            # 尝试从跳转页面提取最新域名
            if redirect_content and self._update_domains_from_redirect(redirect_content):
                print(f"[+] 域名列表已更新，使用新域名重试...")
                continue  # 直接用新域名重试，跳过冷却等待
            
            # 当前域名请求失败或解密结果为虚假发布页，冷却等待后轮换域名重试
            print(f"[!] 当前域名疑似被封，冷却等待后切换...")
            time.sleep(random.uniform(8.0, 15.0))
            self._rotate_domain()
            
        # 如果走到这说明现有的所有镜像都尝试失败了
        if retry_with_main:
            print(f"[!] {self.source_name.upper()} 所有现有镜像域名均尝试失败，尝试从主站更新域名列表...")
            if self._fetch_domains_from_main_station():
                print(f"[+] 成功从主站拉取到新域名，开始重新尝试请求列表页...")
                return self.fetch_list_page(page_num, retry_with_main=False)

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
        is_existing = False  # 外部 BaseCrawler 已进行过批量去重过滤

        # 每个详情页请求前随机延迟，模拟人类浏览行为
        if getattr(self, 'no_pdf', False):
            time.sleep(random.uniform(0.3, 0.8))
        else:
            time.sleep(random.uniform(2.0, 5.0))
        
        detail_html = None
        url = original_url  # Bug 6 修复：提前初始化 url，避免循环外 NameError
        
        # 最多尝试轮换所有域名的次数
        for _ in range(len(self.domains)):
            # 动态替换域名为当前的最优域名（线程安全读取）
            from urllib.parse import urlparse, urlunparse
            parsed_url = urlparse(original_url)
            with self._domain_lock:
                current_base = self.base_domain
            parsed_base = urlparse(current_base)
            if any(d in parsed_url.netloc for d in self.domains) or "685835.xyz" in parsed_url.netloc:
                parsed_url = parsed_url._replace(netloc=parsed_base.netloc, scheme=parsed_base.scheme)
                url = urlunparse(parsed_url)
            else:
                url = original_url

            # 使用完整浏览器请求头
            list_url = self.base_list_url.format(self.current_class, 1)
            headers = self._build_headers(referer=list_url)
            redirect_content = None  # 追踪跳转页面内容

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
                    response = requests.get(url, headers=headers, timeout=15, proxies=proxies, impersonate="chrome120")
                    if response.status_code == 200:
                        decrypted = self.decrypt_html(response.text)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            detail_html = decrypted
                            break
                        # 记录跳转页面内容
                        if decrypted and "正在检测最新可用线路" in decrypted:
                            redirect_content = decrypted
                        elif "正在检测最新可用线路" in response.text:
                            redirect_content = response.text
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
                        # 记录跳转页面内容
                        if decrypted and "正在检测最新可用线路" in decrypted:
                            redirect_content = decrypted
                        elif "正在检测最新可用线路" in html:
                            redirect_content = html
                except Exception as e:
                    print(f"[-] Playwright 兜底抓取详情页异常 ({url}): {e}")

            if detail_html:
                break
            
            # 尝试从跳转页面提取最新域名
            if redirect_content and self._update_domains_from_redirect(redirect_content):
                continue  # 直接用新域名重试，跳过冷却等待
                
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
            # 最多重试 3 次，失败时重建 Playwright 资源
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
        """大唐BT爬虫入口，对三个板块依次进行爬取，支持断点续爬"""
        self.is_test = is_test
        self.quiet = kwargs.get('quiet', False)
        resume = kwargs.get('resume', False)
        self.resume = resume
        self.no_pdf = kwargs.get('no_pdf', False)
        
        classes = ["guochan", "wuma", "oumei"]

        # 只调用一次 on_start（初始化 R2、代理管理器），避免重复创建/释放资源
        print(f"[*] 启动 {self.source_name} 爬虫流程...")
        self.on_start()

        no_early_stop = kwargs.get('no_early_stop', False)
        if no_early_stop:
            print("[*] 禁用早停机制，将强制爬取指定范围内所有页面。")
            self.max_consecutive_existing = None
            self.max_consecutive_duplicate_pages = None

        if max_workers is None:
            if getattr(self, 'no_pdf', False):
                max_workers = 30
            else:
                max_workers = 10

        # ========== 断点续爬逻辑：确定各板块的起始状态 ==========
        resume_class = None          # 需要从中断处恢复的板块
        resume_page = start_page     # 该板块的起始页码

        if resume:
            all_states = self.db_manager.load_crawl_state(self.source_name)
            if all_states:
                # 检查是否有 '__all__' 标记表示全部完成
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
                
                # 找到第一个未完成的板块（page_num <= end_page 表示未完成）
                for cls in classes:
                    state = all_states.get(cls)
                    if state:
                        saved_page = state["page_num"]
                        if saved_page <= end_page:
                            # 该板块未完成，从中断处继续
                            resume_class = cls
                            resume_page = saved_page
                            print(f"[*] 检测到板块 {cls} 爬取断点，从第 {resume_page} 页继续")
                            break
                        # saved_page > end_page，该板块已完成，继续检查下一个
                        print(f"[断点续爬] 板块 {cls} 已完成，跳过")
                    else:
                        # 没有记录说明还没开始，从这里开始
                        resume_class = cls
                        resume_page = start_page
                        print(f"[*] 板块 {cls} 无历史记录，从头开始爬取")
                        break
                else:
                    # 所有板块都已完成
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
                # 断点续爬：跳过 resume_class 之前的板块
                if resume and resume_class is not None:
                    cls_index = classes.index(cls)
                    resume_index = classes.index(resume_class)
                    if cls_index < resume_index:
                        print(f"\n[断点续爬] 板块 {cls} 已完成，跳过")
                        continue
                    # resume_class 及之后的板块正常爬取
                    if cls == resume_class:
                        actual_start = resume_page
                    else:
                        actual_start = start_page
                else:
                    actual_start = start_page

                self.current_class = cls
                print(f"\n[*] ================= 开始爬取大唐BT板块: {cls} (起始页码: {actual_start}) =================")
                self._crawl_pages(actual_start, end_page, max_workers, class_name=cls)
                
                # 当前板块正常结束后，保存状态为完成，以便后续板块依次执行
                if resume and cls == resume_class:
                    # 如果该板块正常爬完（未提前退出），把 resume_class 置空，后续板块从 start_page 开始
                    resume_class = None
            
            # 所有板块全部爬取完毕，标记为完全完成
            if not is_test:
                self.db_manager.mark_source_completed(self.source_name)
                print(f"[+] {self.source_name} 所有板块爬取完成，已标记完成状态")

        except KeyboardInterrupt:
            print("\n[中断] 检测到用户手动停止运行 (Ctrl+C)")
        except Exception as e:
            print(f"\n[致命错误] 运行中发生未捕获的异常: {e}")
        finally:
            self.on_finish()
