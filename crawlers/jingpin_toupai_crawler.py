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
from crawlers.base_crawler import PlaywrightBaseCrawler, DomainRotationMixin, DecryptMixin
from utils.proxy_manager import get_proxy_string, get_proxy_dict, get_proxy_manager


class JingpinToupaiCrawler(PlaywrightBaseCrawler, DomainRotationMixin, DecryptMixin):
    def __init__(self, db_manager):
        super().__init__(db_manager, "jingpin_toupai")
        self.check_resource_link = True  # 启用磁力链接二次去重
        self.domains = [
            "pms.532862.xyz"
        ]
        self.current_domain_idx = 0
        self.base_domain = f"https://{self.domains[self.current_domain_idx]}"
        self.base_list_url = f"{self.base_domain}/list/{{}}-{{}}.html"
        self.current_class = "2935277"
        self.max_consecutive_existing = 15  # 连续抓到历史数据时早停
        
        # 域名冷却机制
        self._domain_cooldown = {}
        self._cooldown_seconds = 60
        self._domain_lock = threading.Lock()

    def _build_headers(self, referer=None):
        """构造浏览器请求头，绕过 TLS 指纹检测"""
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

    def _save_pdf(self, target_url, publish_date, title):
        """Playwright 打开详情页并渲染 PDF（去广告）"""
        if getattr(self, 'no_pdf', False):
            return ""
        if not publish_date or publish_date == "Unknown_Date":
            publish_date = "Unknown_Date"

        local_path = self._get_pdf_local_tmp_path(publish_date, title)
        page = None
        try:
            _, _, context = self._get_thread_resources()
            page = context.new_page()

            page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(5.0)
            
            # 清理详情页广告、漂浮弹窗、统计 iframe 及无关的 DOM 节点
            try:
                page.evaluate("""
                    () => {
                        // 移除所有的 iframe 广告
                        document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
                        
                        // 常见广告/统计节点清除
                        const selectors = [
                            'div[style*="height:60px"]', 
                            'div[style*="height:55px"]', 
                            'div[style*="height:70px"]',
                            '#bottom_float',
                            '.layui-layer',
                            '.layui-layer-shade',
                            '[id*="layui-layer"]',
                            '.modal',
                            '.modal-backdrop'
                        ];
                        selectors.forEach(sel => {
                            document.querySelectorAll(sel).forEach(el => el.remove());
                        });

                        // 强行把页面溢出滚动条打开
                        if (document.body) document.body.style.overflow = 'auto';
                        if (document.documentElement) document.documentElement.style.overflow = 'auto';
                    }
                """)
            except Exception as ad_err:
                print(f"[-] 屏蔽广告脚本执行失败: {ad_err}")

            # 模拟 screen 排版，以保证 PDF 完全展现网页原本样貌
            try:
                page.emulate_media(media="screen")
            except:
                pass

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

    def fetch_list_page(self, page_num):
        """拉取列表页 HTML 并解码（解密全页倒序 Base64）"""
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
                    print(f"[*] 正在拉取列表页 (尝试 {attempt+1}/2): {url}")
                    r = requests.get(url, headers=headers, impersonate="chrome110", timeout=20, proxies=proxies)
                    r.encoding = 'utf-8'
                    if r.status_code == 200:
                        decrypted = self.decrypt_html(r.text)
                        if decrypted:
                            return decrypted
                        else:
                            print("[-] 列表页解密失败")
                    else:
                        print(f"[-] 列表页请求失败，状态码: {r.status_code}")
                except Exception as e:
                    print(f"[-] 请求列表页发生异常: {e}")

                if attempt == 0:
                    time.sleep(random.uniform(1.0, 2.0))

            # 如果失败，轮换域名再试
            self._rotate_domain()

        return None

    def parse_list_page(self, list_page_html, page_num):
        """解析列表页解密后的 HTML，从 JSON 中提取详情页 URL 列表"""
        if not list_page_html:
            return []

        # 匹配其中的 JSON 密文
        start_marker = "var j_b64 = '"
        start_idx = list_page_html.find(start_marker)
        if start_idx == -1:
            print("[-] 未在解密的列表页中匹配到 'j_b64' 变量")
            return []

        val_start = start_idx + len(start_marker)
        end_idx = list_page_html.find("'", val_start)
        if end_idx == -1:
            print("[-] 未在解密的列表页中匹配到 'j_b64' 变量结束符")
            return []

        j_b64_str = list_page_html[val_start:end_idx]
        try:
            decoded_bytes = base64.b64decode(j_b64_str)
            decoded_json = decoded_bytes.decode('utf-8')
            data = json.loads(decoded_json) if 'json' in globals() else __import__('json').loads(decoded_json)
            
            items = data.get('l', {}).get('a', [])
            sub_urls = []
            for item in items:
                href = item.get('url', '')
                if href:
                    sub_urls.append(urljoin(self.base_domain, href))
            return sub_urls
        except Exception as e:
            print(f"[-] 解析列表页 JSON 数据失败: {e}")
            return []

    def process_sub_page_if_needed(self, sub_url, idx):
        """提取详情页数据并进行解密和 PDF 渲染"""
        headers = self._build_headers(referer=self.base_domain + "/")
        
        html_text = None
        for attempt in range(2):
            proxies = None
            from config import get_crawler_proxy, is_proxy_manager_enabled
            crawler_proxy = get_crawler_proxy()
            if crawler_proxy:
                proxies = {"http": crawler_proxy, "https": crawler_proxy}
            elif is_proxy_manager_enabled():
                proxies = get_proxy_dict()

            try:
                r = requests.get(sub_url, headers=headers, impersonate="chrome110", timeout=20, proxies=proxies)
                r.encoding = 'utf-8'
                if r.status_code == 200:
                    html_text = self.decrypt_html(r.text)
                    if html_text:
                        break
            except Exception as e:
                pass
            if attempt == 0:
                time.sleep(random.uniform(0.5, 1.5))

        if not html_text:
            print(f"[-] 抓取详情页并解密失败: {sub_url}")
            return False, None

        # 匹配其中的 JSON 密文
        start_marker = "var j_b64 = '"
        start_idx = html_text.find(start_marker)
        if start_idx == -1:
            print(f"[-] 详情页中找不到 'j_b64' 变量: {sub_url}")
            return False, None

        val_start = start_idx + len(start_marker)
        end_idx = html_text.find("'", val_start)
        if end_idx == -1:
            print(f"[-] 详情页中找不到 'j_b64' 变量结束符: {sub_url}")
            return False, None

        j_b64_str = html_text[val_start:end_idx]
        try:
            decoded_bytes = base64.b64decode(j_b64_str)
            decoded_json = decoded_bytes.decode('utf-8')
            data = json.loads(decoded_json) if 'json' in globals() else __import__('json').loads(decoded_json)

            title = data.get('name', '无标题').strip()
            pub_time = "Unknown_Date" # 原始数据未直接暴露发布时间，留空
            
            # 该网站的 "tm" 字段就是其解密出来的磁力链接
            resource_link = data.get('tm', 'None').strip()
            size = data.get('ts', 'None').strip()
            res_format = data.get('tr', 'None').strip()
            category = data.get('cn', '自拍').strip()

            print(f"[{idx}] 解析详情页成功: {title} | 大小: {size} | 链接: {resource_link[:50]}...")

            # 写入 PDF 文件（测试模式跳过）
            pdf_path = ''
            if not self.is_test:
                for attempt in range(1, 4):
                    pdf_path = self._save_pdf(sub_url, pub_time, title)
                    if pdf_path:
                        break
                    else:
                        print(f"[-] [PDF-SAVE] 详情页: {sub_url} 生成 PDF 失败，第 {attempt}/3 次重试...")
                        if attempt < 3:
                            try:
                                self._recreate_thread_resources()
                            except Exception as rec_err:
                                print(f"[!] 重构 Playwright 资源失败: {rec_err}")
                            time.sleep(random.uniform(1.5, 3.0))

            # 数据清洗入库
            processed_data = self.clean_common_metadata(
                title=title,
                date_str=pub_time,
                resource_link=resource_link,
                category=category,
                url=sub_url,
                pdf_path=pdf_path
            )

            # 覆盖具体的额外数据
            processed_data['size'] = size
            processed_data['resource_format'] = res_format
            processed_data['source'] = self.source_name

            return False, processed_data

        except Exception as e:
            print(f"[-] 解析详情页 JSON 密文发生错误: {e}")
            return False, None

    def run(self, is_test=False, start_page=1, end_page=1, max_workers=None, **kwargs):
        """爬虫流程入口，对多个精品自拍板块依次爬取并支持断点续爬"""
        self.is_test = is_test
        self.quiet = kwargs.get('quiet', False)
        resume = kwargs.get('resume', False)
        self.resume = resume
        self.no_pdf = kwargs.get('no_pdf', False)
        
        # 爬取的三个分类/板块板块
        classes = ["2935277", "2965277", "2975277"]

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
                max_workers = 5  # 合理线程数，Playwright 不易卡死

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
                # 在测试模式下，强制对第一个分类的第一页运行测试即可
                self.current_class = classes[0]
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
                print(f"\n[*] ================= 开始爬取自拍板块: {cls} (起始页码: {actual_start}) =================")
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
