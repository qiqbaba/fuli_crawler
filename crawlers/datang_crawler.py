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


def sanitize_filename(filename):
    """清理文件名中的非法字符，移除表情符号及特殊变体字符防止编码问题"""
    # 替换 Windows 文件名非法字符
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
    # 移除非 BMP 字符（如 Emoji 等 Unicode 码点大于 0xFFFF 的字符）
    filename = re.sub(r'[^\u0000-\uFFFF]', '', filename)
    # 移除特殊的不可见控制字符和变体选择器
    filename = re.sub(r'[\u200b-\u200d\ufe00-\ufe0f\ufeff]', '', filename)
    return filename.strip()



class DatangCrawler(BaseCrawler):
    def __init__(self, db_manager):
        # source_name 设为 datang
        super().__init__(db_manager, "datang")
        self.base_list_url = "https://urn.685835.xyz/list.php?class={}&page={}"
        self.base_domain = "https://urn.685835.xyz"
        self.current_class = "guochan"
        self.max_consecutive_existing = 15  # 连续抓到历史数据时早停
        self.r2_uploader = None
        self.thread_local = threading.local()
        self._active_resources = []
        self._resources_lock = threading.Lock()

    def decrypt_html(self, raw_html):
        """解密目标网站动态混淆的 HTML"""
        # 寻找最长的 Base64-like 字符候选（仅含 Base64 字符且长度大于 1000）
        candidates = re.findall(r"['\"]([A-Za-z0-9+/=]{1000,})['\"]", raw_html)
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
            
        # 清除 thread_local 上的属性以防止多板块循环时重用已停止的 Playwright 实例
        if hasattr(self.thread_local, "playwright"):
            delattr(self.thread_local, "playwright")
        if hasattr(self.thread_local, "browser"):
            delattr(self.thread_local, "browser")
        if hasattr(self.thread_local, "context"):
            delattr(self.thread_local, "context")
            
        self.db_manager.commit()

    def _get_thread_resources(self):
        """获取当前线程特有的 Playwright 实例"""
        if not hasattr(self.thread_local, "playwright"):
            p = sync_playwright().start()
            
            from config import CRAWLER_PROXY
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security",
                "--ignore-certificate-errors",
            ]
            
            playwright_proxy = None
            if CRAWLER_PROXY:
                playwright_proxy = {"server": CRAWLER_PROXY}
                
            browser = p.chromium.launch(headless=True, args=launch_args, proxy=playwright_proxy)
            context = browser.new_context(
                viewport={'width': 1280, 'height': 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai"
            )
            
            self.thread_local.playwright = p
            self.thread_local.browser = browser
            self.thread_local.context = context
            
            with self._resources_lock:
                self._active_resources.append((p, browser, context, None))
                
        return self.thread_local.playwright, self.thread_local.browser, self.thread_local.context

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
        local_path = self._get_pdf_local_tmp_path(publish_date, title)
        try:
            _, _, context = self._get_thread_resources()
            page = context.new_page()
            page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(3.0)
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
            return result
        else:
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            rel_path = f"pdf/{year}/{os.path.basename(local_path)}"
            return rel_path.replace('\\', '/')

    def fetch_list_page(self, page_num):
        """请求列表页并解密 HTML"""
        url = self.base_list_url.format(self.current_class, page_num)
        headers = {
            "User-Agent": random.choice(USER_AGENTS)
        }
        # 1. 优先使用 requests
        for attempt in range(2):
            try:
                response = requests.get(url, headers=headers, timeout=15)
                if response.status_code == 200:
                    decrypted = self.decrypt_html(response.text)
                    if decrypted:
                        return decrypted
            except Exception:
                pass
            time.sleep(random.uniform(1.0, 2.5))
            
        # 2. 兜底使用 Playwright
        print(f"[*] 使用 Playwright 兜底访问列表页: {url}")
        try:
            _, _, context = self._get_thread_resources()
            page = context.new_page()
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(1.0)
            html = page.content()
            page.close()
            if "class=\"bt_ul\"" in html or "class='bt_ul'" in html or "bt_ul" in html:
                return html
            decrypted = self.decrypt_html(html)
            if decrypted:
                return decrypted
        except Exception as e:
            print(f"[-] Playwright 兜底抓取列表页异常: {e}")
            
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
        """请求详情页，解析资源元数据并生成 PDF"""
        url = raw_item['url']
        is_existing = self.db_manager.check_url_exists(url)
        if is_existing and not self.is_test:
            return True, None

        headers = {
            "User-Agent": random.choice(USER_AGENTS)
        }
        detail_html = None
        
        # 1. 优先使用 requests
        for attempt in range(2):
            try:
                response = requests.get(url, headers=headers, timeout=15)
                if response.status_code == 200:
                    decrypted = self.decrypt_html(response.text)
                    if decrypted:
                        detail_html = decrypted
                        break
            except Exception:
                pass
            time.sleep(random.uniform(1.0, 2.0))

        # 2. 兜底使用 Playwright
        if not detail_html:
            try:
                _, _, context = self._get_thread_resources()
                page = context.new_page()
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                time.sleep(1.0)
                html = page.content()
                page.close()
                if "video-description" in html or "class=\"video-description\"" in html:
                    detail_html = html
                else:
                    detail_html = self.decrypt_html(html)
            except Exception as e:
                print(f"[-] Playwright 兜底抓取详情页异常 ({url}): {e}")

        if not detail_html:
            print(f"[-] 详情页 {url} 抓取失败")
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
            print(f"[-] 在详情页中未找到磁力链接: {url}")
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
            data['pdf_path'] = saved_pdf

        return is_existing, data

    def run(self, is_test=False, start_page=1, end_page=1, max_workers=None, **kwargs):
        """大唐BT爬虫入口，对三个板块依次进行爬取"""
        self.is_test = is_test
        classes = ["guochan", "wuma", "oumei"]
        
        for cls in classes:
            self.current_class = cls
            print(f"\n[*] ================= 开始爬取大唐BT板块: {cls} =================")
            super().run(is_test=is_test, start_page=start_page, end_page=end_page, max_workers=max_workers, **kwargs)
