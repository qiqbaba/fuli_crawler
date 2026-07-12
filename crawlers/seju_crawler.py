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


class SejuCrawler(PlaywrightBaseCrawler):
    def __init__(self, db_manager):
        super().__init__(db_manager, "seju")
        self.base_url = "https://seju.life/page/{}/"
        self.target_domain = "seju.life"
        self.use_persistent_context = True
        self.proxy_test_url = "https://seju.life/"


    _PDF_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{safe_title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Microsoft YaHei", sans-serif;
            line-height: 1.8;
            color: #333;
            max-width: 850px;
            margin: 0 auto;
            padding: 40px 30px;
            background-color: #fff;
        }}
        h1 {{
            font-size: 26px;
            color: #111;
            margin-bottom: 12px;
            font-weight: 600;
            line-height: 1.4;
        }}
        .meta {{
            font-size: 14px;
            color: #777;
            border-bottom: 1px dashed #ddd;
            padding-bottom: 15px;
            margin-bottom: 30px;
        }}
        .meta span {{
            margin-right: 15px;
        }}
        .content {{
            font-size: 16px;
            color: #222;
        }}
        .content p {{
            margin-bottom: 20px;
            word-wrap: break-word;
            word-break: break-all;
            white-space: pre-wrap;
        }}
        .content img {{
            max-width: 100%;
            height: auto;
            display: block;
            margin: 25px auto;
            border-radius: 4px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        }}
        .footer {{
            margin-top: 50px;
            font-size: 12px;
            color: #999;
            text-align: center;
            border-top: 1px dashed #ddd;
            padding-top: 20px;
        }}
        a {{
            color: #0066cc;
            text-decoration: none;
        }}
        a:hover {{
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    <h1>{safe_title}</h1>
    <div class="meta">
        <span><strong>发布时间:</strong> {safe_pub_time}</span>
        <span><strong>分类:</strong> {safe_category}</span>
        <span><strong>来源:</strong> <a href="{safe_current_url}">{safe_current_url}</a></span>
    </div>
    <div class="content">
        {content_html}
    </div>
    <div class="footer">
        PDF 由 Fuli Crawler 离线格式化生成
    </div>
</body>
</html>"""

    def get_list_url(self, page_num):
        """获取指定页码的列表页 URL，针对第一页避免 301 重定向"""
        if page_num == 1:
            return "https://seju.life/"
        return self.base_url.format(page_num)

 
    def _wait_for_cloudflare_bypass(self, page, timeout_sec=60):
        """
        检测并等待 Cloudflare Challenge (Just a moment...) 页面自动重定向通过。
        """
        from config import is_local_mode
        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            try:
                title = page.title()
                url = page.url
                # 判断是否仍在 CF 挑战页
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
        except:
            pass
        try:
            current_title = page.title()
            print(f"[-] 智能等待 Cloudflare 结束，但当前页面 Title 依然为: '{current_title}'")
        except Exception:
            print(f"[-] 智能等待 Cloudflare 结束，但无法获取页面标题（页面可能已关闭）")
        return False
 
    def _recreate_thread_resources(self):
        super()._recreate_thread_resources()
        if hasattr(self.thread_local, "list_page"):
            del self.thread_local.list_page

    def _http_get(self, url, timeout=20):
        """使用 curl_cffi 模拟浏览器获取 URL，处理反爬和编码。返回 (final_url, html_text)"""
        proxies = None
        crawler_proxy = None
        try:
            ua = random.choice(USER_AGENTS)
            headers = {"User-Agent": ua}
            
            # 优先使用运行时配置的代理
            from config import get_crawler_proxy, is_proxy_manager_enabled
            crawler_proxy = get_crawler_proxy()
            if crawler_proxy:
                proxies = {"http": crawler_proxy, "https": crawler_proxy}
            elif is_proxy_manager_enabled():
                # 使用代理管理器获取随机代理
                proxies = get_proxy_dict()
            
            r = requests.get(url, headers=headers, impersonate="chrome120", timeout=timeout, proxies=proxies)
            r.encoding = 'utf-8'
            if r.status_code == 200:
                return r.url, r.text
            else:
                print(f"[-] HTTP 请求失败 ({url}): 状态码 {r.status_code}")
                if r.status_code in (403, 407, 502, 503, 504) and proxies and is_proxy_manager_enabled():
                    manager = get_proxy_manager()
                    if manager and "http" in proxies:
                        manager.report_failure(proxies["http"])
                return url, None
        except Exception as e:
            print(f"[-] HTTP 请求异常 ({url}): {e}")
            if proxies and is_proxy_manager_enabled():
                manager = get_proxy_manager()
                if manager and "http" in proxies:
                    manager.report_failure(proxies["http"])
            elif crawler_proxy:
                print(f"[!] 固定代理请求异常，请检查代理是否有效: {crawler_proxy}")
            return url, None
 
    def _http_get_binary(self, url, timeout=25):
        """使用 curl_cffi 下载二进制文件（如图片）"""
        proxies = None
        crawler_proxy = None
        try:
            ua = random.choice(USER_AGENTS)
            headers = {"User-Agent": ua}
            
            # 优先使用运行时配置的代理
            from config import get_crawler_proxy, is_proxy_manager_enabled
            crawler_proxy = get_crawler_proxy()
            if crawler_proxy:
                proxies = {"http": crawler_proxy, "https": crawler_proxy}
            elif is_proxy_manager_enabled():
                # 使用代理管理器获取随机代理
                proxies = get_proxy_dict()
            
            r = requests.get(url, headers=headers, impersonate="chrome120", timeout=timeout, proxies=proxies)
            if r.status_code == 200:
                return r.content
            else:
                print(f"[-] 二进制下载失败 ({url}): 状态码 {r.status_code}")
                if r.status_code in (403, 407, 502, 503, 504) and proxies and is_proxy_manager_enabled():
                    manager = get_proxy_manager()
                    if manager and "http" in proxies:
                        manager.report_failure(proxies["http"])
                return None
        except Exception as e:
            print(f"[-] 二进制下载异常 ({url}): {e}")
            if proxies and is_proxy_manager_enabled():
                manager = get_proxy_manager()
                if manager and "http" in proxies:
                    manager.report_failure(proxies["http"])
            elif crawler_proxy:
                print(f"[!] 固定代理二进制请求异常，请检查代理是否有效: {crawler_proxy}")
            return None


    def fetch_list_page(self, page_num):
        """加载列表页并返回当前 HTML 文本 (curl_cffi 优先，Playwright 兜底)"""
        list_url = self.get_list_url(page_num)
        print(f"[*] 正在访问列表页: {list_url}")
        
        # 1. 优先使用 curl_cffi 尝试直接拉取（高概率直接穿盾，速度快）
        try:
            _, html_text = self._http_get(list_url, timeout=25)
            if html_text and "Just a moment..." not in html_text and "cloudflare" not in html_text.lower():
                print(f"[+] 使用 curl_cffi 成功直接抓取列表页 (无 Cloudflare 拦截): {list_url}")
                time.sleep(random.uniform(2, 4))
                return html_text
            else:
                print(f"[*] curl_cffi 尝试抓取被拦截或失效，将回退至 Playwright 备用通道...")
        except Exception as curl_err:
            print(f"[-] curl_err 抓取失败: {curl_err}，转向 Playwright...")
            
        # 2. Playwright 兜底方案
        try:
            _, _, context = self._get_thread_resources()
            if not hasattr(self.thread_local, "list_page") or self.thread_local.list_page.is_closed():
                self.thread_local.list_page = context.new_page()
            
            page = self.thread_local.list_page
            page.goto(list_url, timeout=60000, wait_until="domcontentloaded")
            
            # 检测并等待 Cloudflare 盾通过
            self._wait_for_cloudflare_bypass(page)
            
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
            self._recreate_thread_resources()
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

    def _save_pdf(self, html_content, publish_date, title):
        """
        保存 PDF：使用 Playwright 将 HTML 内容离线渲染并保存为 PDF。
        若配置了 R2 则上传并返回 R2 Key，否则返回本地路径。
        """
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
            page.set_content(html_content, wait_until="networkidle")
            page.pdf(
                path=local_path,
                format="A4",
                print_background=True,
                margin={"top": "20mm", "bottom": "20mm", "left": "20mm", "right": "20mm"}
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
        """
        处理单个子页面的抓取、信息提取、PDF 保存/上传。
        """
        # 1. 检查子页面链接是否已存在 (外部 BaseCrawler 已进行过批量去重过滤)
        is_existing = False

        html_text = None
        current_url = sub_url
        
        # 2. 优先使用 curl_cffi 尝试直接拉取（高概率直接穿盾，防无头检测）
        try:
            final_url, curl_html = self._http_get(sub_url, timeout=25)
            if curl_html and "Just a moment..." not in curl_html and "cloudflare" not in curl_html.lower():
                print(f"[{idx}] 使用 curl_cffi 成功直接抓取子页面: {sub_url}")
                html_text = curl_html
                current_url = final_url
            else:
                print(f"[{idx}] curl_cffi 抓取子页面被拦截或失败，转向 Playwright 备用通道...")
        except Exception as curl_err:
            print(f"[{idx}] curl_cffi 抓取异常: {curl_err}，转向 Playwright...")
            
        # 3. Playwright 兜底方案
        if not html_text:
            sub_page = None
            try:
                _, _, context = self._get_thread_resources()
                sub_page = context.new_page()
                sub_page.goto(sub_url, timeout=60000, wait_until="domcontentloaded")
                
                # 检测并等待 Cloudflare 盾通过
                self._wait_for_cloudflare_bypass(sub_page)
                
                try:
                    sub_page.wait_for_load_state("load", timeout=10000)
                except Exception as wait_err:
                    print(f"[!] 等待 load 状态超时，继续处理: {wait_err}")
                    
                time.sleep(random.uniform(1.5, 3.5))
                
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
                self._recreate_thread_resources()
            finally:
                if sub_page:
                    try:
                        sub_page.close()
                    except Exception as close_err:
                        print(f"[-] 关闭临时子页面失败: {close_err}")

        if not html_text:
            print(f"[-] 子页面 {sub_url} 抓取失败")
            return False, None

        parsed_url = urlparse(current_url)
        is_external = self.target_domain not in parsed_url.netloc

        # 4. 检查重定向后的真实链接是否已存在
        if current_url != sub_url:
            if self.db_manager.check_url_exists(current_url) and not self.is_test:
                print(f"[{idx}] 重定向后的真实网址已存在，跳过抓取: {current_url}")
                return True, None

        temp_image_dir = ""
        try:
            if is_external:
                print(f"检测到跳转至外部网站: {current_url}")
                soup = BeautifulSoup(html_text, 'html.parser')
                title = soup.title.string.strip() if soup.title else "外部链接"
                pub_time = ""      # Bug 1 修复：is_external 分支中初始化 pub_time，防止 NameError
                content_html = ""  # Bug 1 修复：is_external 分支中初始化 content_html，防止 NameError
                category = "外部跳转"
                res_link = current_url
                link_type = "外链"
                pdf_path = ""
            else:
                soup = BeautifulSoup(html_text, 'html.parser')
                title_node = soup.select_one('h1.article-title a') or soup.select_one('h1.article-title')
                title = title_node.get_text().strip() if title_node else "无标题"

                time_node = soup.select_one('header.article-header div.meta time')
                pub_time_raw = time_node.get_text().strip() if time_node else ""
                if not pub_time_raw and time_node and time_node.get('datetime'):
                    pub_time_raw = time_node.get('datetime').strip()
                
                # 反混淆混淆的时间字符：С -> 小, ʱ -> 时, ǰ -> 前
                pub_time_cleaned = pub_time_raw.replace('С', '小').replace('ʱ', '时').replace('ǰ', '前')
                _, pub_time = parse_date(pub_time_cleaned)

                # 获取分类
                category = "Video"
                meta_spans = soup.select('header.article-header div.meta span')
                if meta_spans:
                    for span in meta_spans:
                        text = span.get_text().strip()
                        if text and not text.isdigit() and "小时" not in text and "天" not in text and "Сʱ" not in text:
                            category = text
                            break

                # 提取正文文本与资源链接
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

                # 处理图片下载与本地化
                if content_div and not getattr(self, 'no_pdf', False):
                    img_tags = content_div.find_all('img')
                    if img_tags:
                        local_tmp_base = self._get_pdf_local_tmp_path(pub_time, title)
                        temp_image_dir = os.path.join(os.path.dirname(local_tmp_base), f"temp_imgs_{idx}_{int(time.time())}")
                        os.makedirs(temp_image_dir, exist_ok=True)
                        
                        for img_idx, img in enumerate(img_tags):
                            img_src = img.get('src')
                            if not img_src:
                                continue
                            
                            abs_img_url = urljoin(current_url, img_src)
                            img_ext = os.path.splitext(abs_img_url.split('?')[0])[1]
                            if not img_ext or len(img_ext) > 5:
                                img_ext = ".jpg"
                            local_img_name = f"img_{img_idx}_{os.path.basename(abs_img_url.split('?')[0])}"
                            if not local_img_name.endswith(img_ext):
                                local_img_name += img_ext
                            
                            local_img_path = os.path.join(temp_image_dir, local_img_name)
                            
                            # 下载图片二进制并保存
                            img_data = self._http_get_binary(abs_img_url)
                            if img_data:
                                try:
                                    with open(local_img_path, 'wb') as img_f:
                                        img_f.write(img_data)
                                    file_url = f"file:///{local_img_path.replace(os.sep, '/')}"
                                    img['src'] = file_url
                                    if img.get('data-src'):
                                        del img['data-src']
                                    if img.get('data-original-src'):
                                        del img['data-original-src']
                                except Exception as write_err:
                                    print(f"[-] 保存本地图片失败: {write_err}")

                # 获得修改后的正文 HTML 内容
                content_html = str(content_div) if content_div else ""

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

            # 针对内部网页，生成 PDF 并上传
            if not is_external:
                if self.is_test:
                    print("-> 测试模式下跳过保存 PDF 以节省时间")
                else:
                    import html as html_escape
                    safe_title = html_escape.escape(title)
                    safe_pub_time = html_escape.escape(pub_time)
                    safe_category = html_escape.escape(category)
                    safe_current_url = html_escape.escape(current_url)
                    html_template = self._PDF_HTML_TEMPLATE.format(
                        safe_title=safe_title,
                        safe_pub_time=safe_pub_time,
                        safe_category=safe_category,
                        safe_current_url=safe_current_url,
                        content_html=content_html
                    )
                    # 使用安全的日期字符串作为 PDF 文件名，避免 Unknown_Date
                    pdf_date = pub_time if pub_time and pub_time != "Unknown_Date" else "Unknown_Date"
                    # 最多重试 3 次，失败时重建 Playwright 资源
                    saved_path = ""
                    for attempt in range(1, 4):
                        saved_path = self._save_pdf(html_template, pdf_date, title)
                        if saved_path:
                            print(f"[PDF-SAVE] 标题: {title} -> PDF 路径: {saved_path}")
                            break
                        else:
                            print(f"[-] [PDF-SAVE] 标题: {title} 生成 PDF 失败，进行第 {attempt}/3 次尝试")
                            if attempt < 3:
                                try:
                                    self._recreate_thread_resources()
                                except Exception as recreate_err:
                                    print(f"[!] 重构 Playwright 资源失败: {recreate_err}")
                                time.sleep(random.uniform(1.5, 3.0))
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
            if temp_image_dir and os.path.exists(temp_image_dir):
                try:
                    shutil.rmtree(temp_image_dir)
                except Exception as clean_err:
                    print(f"[-] 清理临时图片目录 {temp_image_dir} 失败: {clean_err}")
            time.sleep(random.uniform(1, 2))
