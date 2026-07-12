import os
import re
import threading
import time
import random
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from curl_cffi import requests
from config import USER_AGENTS, is_local_mode
from crawlers.base_crawler import PlaywrightBaseCrawler
from utils.date_parser import parse_date
from utils.proxy_manager import get_proxy_string, get_proxy_dict, get_proxy_manager
from utils.metadata_parser import sanitize_filename


class GcbtCrawler(PlaywrightBaseCrawler):
    def __init__(self, db_manager):
        super().__init__(db_manager, "gcbt")
        self.base_url = "https://gcbt.net/"
        self.target_domain = "gcbt.net"
        self.use_persistent_context = True
        self.proxy_test_url = "https://gcbt.net/download/8067.html"
        self.proxy_expected_content = "90231538e5368bb8422500604f01cb25edfeedb4"
        # 默认早停阈值为连续 20 条已存在记录
        self.max_consecutive_existing = 20
        self.max_consecutive_duplicate_pages = 3


    def get_list_url(self, page_num):
        """获取指定页码的列表页 URL"""
        if page_num == 1:
            return self.base_url
        return urljoin(self.base_url, f"page/{page_num}")

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



    def _save_pdf(self, target_url, publish_date, title):
        """直接用 Playwright 打开详情页并保存为 PDF"""
        if getattr(self, 'no_pdf', False):
            return ""
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

        return self._upload_or_return_pdf_path(local_path, publish_date)

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
