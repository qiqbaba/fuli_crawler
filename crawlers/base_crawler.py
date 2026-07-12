import os
import json
import time
import random
import threading
from utils.date_parser import parse_date
from utils.metadata_parser import parse_title, parse_link_metadata, parse_pikpak_link
from utils.lang_filter import is_japanese

class BaseCrawler:
    def __init__(self, db_manager, source_name):
        self.db_manager = db_manager
        self.source_name = source_name
        self.max_consecutive_existing = 20
        self.max_consecutive_duplicate_pages = None
        self.check_resource_link = False  # 是否额外检查 resource_link（磁力链接）去重，子类按需开启
        self.skip_japanese = True  # 是否跳过日语标题

    def on_start(self):
        """生命周期钩子：爬网开始前（子类可选覆盖）"""
        pass

    def on_finish(self):
        """生命周期钩子：爬网结束后（子类可选覆盖）"""
        pass

    def release_thread_resources(self):
        """生命周期钩子：释放线程局部资源（子类可选覆盖）"""
        pass

    def fetch_list_page(self, page_num):
        """抓取列表页内容，返回原始页面内容或对象"""
        raise NotImplementedError("子类必须实现 fetch_list_page 方法")

    def parse_list_page(self, list_page_content, page_num):
        """解析列表页内容，返回原始 item 字典列表"""
        raise NotImplementedError("子类必须实现 parse_list_page 方法")

    def process_sub_page_if_needed(self, raw_item, idx):
        """
        若需要访问子页面（如 seju），可在这里具体实现。
        若不需要（如 u3c3），直接返回 raw_item 或处理好的数据。
        返回值格式要求:
            (is_existing_in_db, processed_item_data)
            - is_existing_in_db: bool，指示此记录在数据库中是否已存在
            - processed_item_data: dict，最终结构化完毕的数据。如果发生错误或被过滤，可返回 None
        """
        raise NotImplementedError("子类必须实现 process_sub_page_if_needed 方法")

    def clean_common_metadata(self, title, date_str, resource_link, category, url, pikpak_link=None, pdf_path=''):
        """通用的数据清洗与结构化逻辑"""
        # 解析发布日期
        _, publish_date = parse_date(date_str)
        
        # 提取标题元数据
        size_val, res_format = parse_title(title)
        
        # 提取链接元数据
        link_size, link_format = parse_link_metadata(resource_link)
        
        if not size_val:
            size_val = link_size
        if not res_format:
            res_format = link_format
            
        # 提取 PikPak 链接
        real_pikpak = pikpak_link
        if not real_pikpak and resource_link:
            real_pikpak = parse_pikpak_link(resource_link)
            
        return {
            'title': title,
            'publish_time': publish_date,
            'category': category,
            'resource_link': resource_link,
            'pikpak_link': real_pikpak,
            'size': size_val,
            'resource_format': res_format,
            'link_type': '',  # 默认留空
            'url': url,
            'pdf_path': pdf_path,
            'source': self.source_name
        }

    def _is_japanese_title(self, raw_item):
        """
        检查 raw_item 中的标题是否为日语。
        如果 skip_japanese 为 False 或 raw_item 中没有 title 字段，返回 False。
        """
        if not self.skip_japanese:
            return False
        title = None
        if isinstance(raw_item, dict):
            title = raw_item.get('title', '')
        elif isinstance(raw_item, str):
            return False  # 纯字符串(如 URL)无法检测
        if not title:
            return False
        return is_japanese(title)

    def _run_test_mode(self, start_page):
        """测试模式：抓取第一页前 5 条并输出，不入库"""
        print(f"【测试模式】正在抓取第一页以提供测试数据...")
        list_content = self.fetch_list_page(start_page)
        if not list_content:
            print("[-] 抓取列表页测试数据失败。")
            return

        raw_items = self.parse_list_page(list_content, start_page)
        print(f"[+] 列表页解析完成，共找到 {len(raw_items)} 条记录。")

        test_items = raw_items[:5]
        print(f"\n================ 进行前 5 条数据测试 ================")
        for idx, raw_item in enumerate(test_items, 1):
            res = self.process_sub_page_if_needed(raw_item, idx)
            if not res:
                print(f"[{idx}] 提取测试失败")
                continue
            is_existing, data = res
            if data:
                print(f"[{idx}] 状态 (已存在数据库: {is_existing})")
                print(f"    - title (标题): {data['title']}")
                print(f"    - publish_time (发布时间): {data['publish_time']}")
                print(f"    - category (分类): {data['category']}")
                print(f"    - resource_link (资源链接): {data['resource_link'][:80]}...")
                print(f"    - pikpak_link (PikPak): {data['pikpak_link']}")
                print(f"    - size (大小): {data['size']}")
                print(f"    - resource_format (格式): {data['resource_format']}")
                print(f"    - url (链接): {data['url']}")
                print(f"    - pdf_path (PDF): {data['pdf_path']}")
                print("-" * 80)
        print("\n[+] 测试完毕。")

    def _save_page_state(self, class_name, page_num, completed=False):
        """保存当前爬取进度到数据库（断点续爬）"""
        try:
            self.db_manager.save_crawl_state(self.source_name, class_name, page_num, completed=completed)
        except Exception as e:
            print(f"[!] 保存爬虫断点状态失败: {e}")

    def _crawl_pages(self, start_page, end_page, max_workers, class_name=None):
        """
        爬虫页面循环核心逻辑（不含 on_start/on_finish），
        供 DatangCrawler 多板块循环复用。
        
        class_name: 可选，用于断点续爬时记录当前板块名称
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import time
        import random
        import os

        consecutive_count = 0
        consecutive_duplicate_pages = 0
        is_gha = os.environ.get('GITHUB_ACTIONS') == 'true'
        early_break = False  # 跟踪是否因早停而跳出循环

        for page_num in range(start_page, end_page + 1):
            if is_gha:
                print(f"::group::正在抓取第 {page_num}/{end_page} 页", flush=True)
            else:
                print(f"\n================ 正在抓取第 {page_num}/{end_page} 页 ================")

            try:
                list_content = self.fetch_list_page(page_num)
                if not list_content:
                    print(f"[-] 页面 {page_num} 抓取失败或无内容，跳过。")
                    continue

                raw_items = self.parse_list_page(list_content, page_num)
                if not raw_items:
                    print(f"[-] 页面 {page_num} 未提取到有效项。")
                    time.sleep(random.uniform(1.0, 2.0))
                    continue

                print(f"[+] 本页共解析到 {len(raw_items)} 条记录。")

                items_to_process = []
                skipped_count = 0
                early_stop_triggered = False

                urls_to_check = [raw_item if isinstance(raw_item, str) else raw_item.get('url') for raw_item in raw_items]
                existing_urls = self.db_manager.filter_existing_urls(urls_to_check)

                for idx, raw_item in enumerate(raw_items, 1):
                    url = raw_item if isinstance(raw_item, str) else raw_item.get('url')
                    is_existing = url in existing_urls

                    # 日语标题预过滤（仅对标题已在 raw_item 中的爬虫有效）
                    if not is_existing and self._is_japanese_title(raw_item):
                        if not self.quiet:
                            title_preview = raw_item.get('title', '')[:40]
                            print(f"[{idx}] 标题检测为日语，跳过: {title_preview}")
                        continue

                    if is_existing:
                        skipped_count += 1
                        if self.max_consecutive_existing is not None:
                            consecutive_count += 1
                            if not self.quiet:
                                print(f"[{idx}] 网址已存在数据库中，跳过抓取: {url}")
                                print(f"[*] 连续发现已存在数据: {consecutive_count}/{self.max_consecutive_existing}")
                            if consecutive_count >= self.max_consecutive_existing:
                                print(f"\n[触发停止条件] 连续 {self.max_consecutive_existing} 条数据已存在，停止处理当前页！")
                                early_stop_triggered = True
                                break
                        else:
                            if not self.quiet:
                                print(f"[{idx}] 网址已存在数据库中，跳过抓取: {url}")
                    else:
                        items_to_process.append((idx, raw_item))
                        consecutive_count = 0

                if not early_stop_triggered:
                    is_page_duplicate = (skipped_count == len(raw_items))
                    if is_page_duplicate:
                        if self.max_consecutive_duplicate_pages is not None:
                            consecutive_duplicate_pages += 1
                            if not self.quiet:
                                print(f"[*] 当前页所有数据均已重复。连续重复页数: {consecutive_duplicate_pages}/{self.max_consecutive_duplicate_pages}")
                            if consecutive_duplicate_pages >= self.max_consecutive_duplicate_pages:
                                early_stop_triggered = True
                    else:
                        consecutive_duplicate_pages = 0

                if not items_to_process or early_stop_triggered:
                    if not items_to_process:
                        print(f"[+] 页面 {page_num} 所有项均已被跳过。")
                    else:
                        print(f"[+] 页面 {page_num} 触发早停，跳过 {len(items_to_process)} 条待处理项。")
                    # 保存断点（每页完成时记录）
                    if class_name is not None:
                        self._save_page_state(class_name, page_num + 1)
                    if early_stop_triggered or (self.max_consecutive_existing is not None and consecutive_count >= self.max_consecutive_existing):
                        print(f"\n[任务结束] 爬虫已追溯到历史抓取位置，安全退出翻页循环。")
                        early_break = True
                        break
                    if self.no_pdf:
                        time.sleep(random.uniform(0.5, 1.5))
                    else:
                        time.sleep(random.uniform(3.0, 6.0))
                    continue

                print(f"[*] 开始并发处理 {len(items_to_process)} 条新纪录 (并发线程数: {max_workers})...")

                inserted_count = 0
                results_dict = {}

                if max_workers == 1:
                    for idx, raw_item in items_to_process:
                        try:
                            res = self.process_sub_page_if_needed(raw_item, idx)
                            if res:
                                is_existing, data = res
                                if is_existing:
                                    skipped_count += 1
                                    if self.max_consecutive_existing is not None:
                                        consecutive_count += 1
                                        if not self.quiet:
                                            print(f"[{idx}] 子页面级去重跳过: 连续已存在计数: {consecutive_count}/{self.max_consecutive_existing}")
                                        if consecutive_count >= self.max_consecutive_existing:
                                            early_stop_triggered = True
                                            break
                                if data:
                                    results_dict[idx] = data
                        except Exception as e:
                            print(f"[-] 处理索引为 [{idx}] 的项目时发生异常: {e}")
                    # 单线程模式清理资源
                    self.release_thread_resources()
                else:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        future_to_idx = {
                            executor.submit(self.process_sub_page_if_needed, raw_item, idx): idx
                                for idx, raw_item in items_to_process
                        }

                        # 先收集所有结果（as_completed 不保证顺序）
                        collected_results = {}  # idx -> (is_existing, data)
                        for future in as_completed(future_to_idx):
                            idx = future_to_idx[future]
                            try:
                                res = future.result()
                                if res:
                                    is_existing, data = res
                                    collected_results[idx] = (is_existing, data)
                            except Exception as e:
                                print(f"[-] 线程处理索引为 [{idx}] 的项目时发生异常: {e}")

                        # Bug 3 fix: 按原始顺序重新处理，确保连续计数正确
                        for idx in sorted(collected_results.keys()):
                            is_existing, data = collected_results[idx]
                            if is_existing:
                                skipped_count += 1
                                if self.max_consecutive_existing is not None:
                                    consecutive_count += 1
                                    if not self.quiet:
                                        print(f"[{idx}] 子页面级去重跳过: 连续已存在计数: {consecutive_count}/{self.max_consecutive_existing}")
                                    if consecutive_count >= self.max_consecutive_existing:
                                        early_stop_triggered = True
                            if data:
                                results_dict[idx] = data

                        print("[*] 正在向并发工作线程发送资源清理指令...")
                        cleanup_futures = [
                            executor.submit(self.release_thread_resources)
                            for _ in range(max_workers)
                        ]
                        for f in as_completed(cleanup_futures):
                            try:
                                f.result()
                            except Exception as e:
                                print(f"[-] 清理工作线程资源时发生异常: {e}")

                # 子页面级去重早停标记（已处理的结果仍会入库，稍后翻页循环会退出）
                if early_stop_triggered:
                    print(f"[!] 子页面级去重触发早停标记，已完成的结果将继续入库。")

                results = [results_dict[idx] for idx in sorted(results_dict.keys())]

                if results:
                    # 日语标题后置过滤（针对预过滤无法获取标题的爬虫）
                    if self.skip_japanese:
                        filtered_jp = []
                        jp_skipped = 0
                        for d in results:
                            title = d.get('title', '')
                            if title and is_japanese(title):
                                jp_skipped += 1
                                if not self.quiet:
                                    print(f"[*] 结果标题检测为日语，过滤: {title[:40]}")
                            else:
                                filtered_jp.append(d)
                        if jp_skipped > 0:
                            print(f"[*] 日语标题后置过滤掉 {jp_skipped} 条")
                        results = filtered_jp

                    # 可选：对已获取的 resource_link（磁力链接）进行二次去重
                    if self.check_resource_link:
                        resource_links = [d.get('resource_link') for d in results]
                        existing_links = self.db_manager.filter_existing_resource_links(resource_links)
                        filtered_results = []
                        resource_link_skipped = 0
                        for d in results:
                            link = d.get('resource_link', '')
                            if link and link in existing_links:
                                resource_link_skipped += 1
                                if not self.quiet:
                                    print(f"[*] 磁力链接已存在数据库中，跳过: {link[:60]}...")
                            else:
                                filtered_results.append(d)
                        if resource_link_skipped > 0:
                            print(f"[*] 磁力链接去重过滤掉 {resource_link_skipped} 条")
                        results = filtered_results

                    print(f"[*] 正在写入 {len(results)} 条新纪录到数据库...")
                    for data in results:
                        success = self.db_manager.insert_resource(data)
                        if success:
                            inserted_count += 1
                            consecutive_count = 0
                        elif success is None:
                            # 网络/API 错误（如 Supabase 不可达），不触发早停
                            skipped_count += 1
                            if not self.quiet:
                                print(f"[*] 写入失败 (网络/API 错误)，不计入连续已存在计数")
                        else:
                            skipped_count += 1
                            if self.max_consecutive_existing is not None:
                                consecutive_count += 1
                                if not self.quiet:
                                    print(f"[*] 写入失败或重复 (DB IGNORE)，连续已存在计数: {consecutive_count}/{self.max_consecutive_existing}")
                                if consecutive_count >= self.max_consecutive_existing:
                                    early_stop_triggered = True
                                    break
                            else:
                                if not self.quiet:
                                    print(f"[*] 写入失败或重复 (DB IGNORE)")

                print(f"[+] 页面 {page_num} 处理完成：写入 {inserted_count} 条，跳过 {skipped_count} 条。")
                self.db_manager.commit()

                # 保存断点状态（每页完成时记录）
                if class_name is not None:
                    self._save_page_state(class_name, page_num + 1)

                if early_stop_triggered or (self.max_consecutive_existing is not None and consecutive_count >= self.max_consecutive_existing):
                    print(f"\n[任务结束] 爬虫已追溯到历史抓取位置，安全退出翻页循环。")
                    early_break = True
                    break

                if self.no_pdf:
                    time.sleep(random.uniform(0.3, 0.8))
                else:
                    time.sleep(random.uniform(1.5, 3.0))
            finally:
                if is_gha:
                    print("::endgroup::", flush=True)
        
        # 所有页正常爬完（for 循环自然结束且未早停），保存当前板块已完成
        if class_name is not None and not early_break:
            self._save_page_state(class_name, end_page + 1)
            print(f"[+] 板块 {class_name} 所有页面爬取完成")

    def run(self, is_test=False, start_page=1, end_page=1, max_workers=None, **kwargs):
        """
        统一的爬虫执行骨架
        """
        self.is_test = is_test
        self.quiet = kwargs.get('quiet', False)
        resume = kwargs.get('resume', False)
        self.resume = resume
        self.no_pdf = kwargs.get('no_pdf', False)
        
        if resume:
            print(f"[*] 启用断点续爬模式，将自动跳过已完成的板块/页面")
        if self.no_pdf:
            print("[*] 启用无 PDF 渲染模式，将跳过 PDF 生成与图片下载")
        
        print(f"[*] 启动 {self.source_name} 爬虫流程...")
        self.on_start()

        no_early_stop = kwargs.get('no_early_stop', False)
        if no_early_stop:
            print("[*] 禁用早停机制，将强制爬取指定范围内所有页面。")
            self.max_consecutive_existing = None
            self.max_consecutive_duplicate_pages = None

        if max_workers is None:
            # Playwright 爬虫（datang, madou, gcbt, seju）每个线程启动一个浏览器，限制并发避免 OOM
            # 纯 curl_cffi 爬虫（u3c3）可开更高并发
            if self.no_pdf:
                # 无 PDF 模式无需浏览器进程，可大幅提升并发
                max_workers = 30
            elif self.source_name in ("seju", "datang", "madou", "gcbt"):
                max_workers = 3
            else:
                max_workers = 50

        try:
            if is_test:
                self._run_test_mode(start_page)
                return

            self._crawl_pages(start_page, end_page, max_workers)

        except KeyboardInterrupt:
            print("\n[中断] 检测到用户手动停止运行 (Ctrl+C)")
        except Exception as e:
            print(f"\n[致命错误] 运行中发生未捕获的异常: {e}")
        finally:
            self.on_finish()


class PlaywrightBaseCrawler(BaseCrawler):
    def __init__(self, db_manager, source_name):
        super().__init__(db_manager, source_name)
        self.thread_local = threading.local()
        self._active_resources = []
        self._resources_lock = threading.Lock()
        self.r2_uploader = None
        self.use_persistent_context = False
        # Perf 1: 是否跨页面复用浏览器，避免每页销毁重建
        self._reuse_browser = True
        # ========== Anti-detection 配置 ==========
        from config import get_effective_browser_type, is_headless_forced, HEADFUL_LOCAL, HEADFUL_CLOUD, is_stealth_enabled, is_local_mode
        # 浏览器类型: 'chromium' | 'firefox' | 'webkit'
        self.browser_type = get_effective_browser_type()
        # 本地模式是否使用 headful（有头）模式，默认 True 便于调试且更难被检测
        self.headful_local = HEADFUL_LOCAL
        # 云端模式是否使用 headful（默认 false 因为无图形界面）
        self.headful_cloud = HEADFUL_CLOUD
        # 是否启用 stealth 高级注入（默认启用）
        self.enable_stealth = is_stealth_enabled()
        # 是否强制 headless（通过 CLI --headless 覆盖）
        self._force_headless = is_headless_forced()

    def on_start(self):
        """初始化 R2 上传器和代理管理器"""
        from utils.r2_uploader import get_r2_uploader
        from config import is_local_mode
        self.r2_uploader = get_r2_uploader()
        if self.r2_uploader:
            print(f"[*] Cloudflare R2 上传器已启用 ({self.source_name})", flush=True)
        else:
            if is_local_mode():
                print(f"[*] 本地模式已激活，PDF 将保存到本地目录 ({self.source_name})", flush=True)
            else:
                print(f"[*] 未配置 R2 环境变量，PDF 将保存到本地目录 ({self.source_name})", flush=True)
        
        # 初始化代理管理器
        from config import is_proxy_manager_enabled
        if is_proxy_manager_enabled():
            print(f"[*] 代理管理器已启用，正在获取和验证代理IP...", flush=True)
            from utils.proxy_manager import get_proxy_manager
            from config import PROXY_VERIFY_WORKERS
            try:
                manager = get_proxy_manager()
                if manager:
                    manager.fetch_proxies(force=False)
                    test_url = getattr(self, 'proxy_test_url', None) or getattr(self, 'base_domain', None) or getattr(self, 'base_url', None)
                    expected_content = getattr(self, 'proxy_expected_content', None)
                    manager.verify_proxies(
                        force=False, 
                        max_workers=PROXY_VERIFY_WORKERS, 
                        test_url=test_url, 
                        expected_content=expected_content
                    )
                    stats = manager.get_stats()
                    print(f"[*] 代理管理器就绪: 总计 {stats['total']} 个，可用 {stats['working']} 个", flush=True)
            except Exception as e:
                print(f"[-] 初始化代理管理器时发生异常: {e}", flush=True)

    def release_thread_resources(self):
        """
        生命周期钩子：释放当前工作线程持有的 Playwright 资源
        
        当 _reuse_browser=True 时（默认），页面间只清理页面级资源，不销毁浏览器，
        由 on_finish 统一清理，避免每页销毁重建的开销。
        """
        if not self._reuse_browser:
            # 旧行为：每页销毁并重建浏览器
            if hasattr(self.thread_local, "playwright"):
                self._destroy_thread_resources()
        else:
            # Perf 1: 仅清理页面级资源，保留浏览器供下一页复用
            try:
                page = getattr(self.thread_local, "page", None)
                if page:
                    page.close()
                    del self.thread_local.page
            except Exception:
                pass

    def on_finish(self):
        """释放 Playwright 渲染资源"""
        print(f"[*] 正在释放主线程 Playwright 资源 ({self.source_name})...")
        # 1. 优先清理主线程自身的资源
        self.release_thread_resources()
        
        # 2. 残留资源兜底关闭
        with self._resources_lock:
            if self._active_resources:
                print(f"[!] 发现 {len(self._active_resources)} 个未被工作线程自主清理的残留资源，执行主线程兜底关闭...")
                for item in self._active_resources:
                    p, browser, context, profile_dir = item
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
                        if p:
                            p.stop()
                    except Exception:
                        pass
                    if profile_dir and os.path.exists(profile_dir):
                        try:
                            import shutil
                            shutil.rmtree(profile_dir)
                        except Exception:
                            pass
                self._active_resources.clear()
            
        self.db_manager.commit()

    def _get_thread_resources(self):
        """获取当前线程特有的 Playwright 实例"""
        if not hasattr(self.thread_local, "playwright"):
            from playwright.sync_api import sync_playwright
            p = sync_playwright().start()
            
            from config import USER_AGENTS, is_local_mode, get_crawler_proxy, is_proxy_manager_enabled
            from utils.proxy_manager import get_proxy_string
            from utils.stealth import get_browser_launch_args, apply_stealth
            
            ua = random.choice(USER_AGENTS)
            local_mode = is_local_mode()
            
            # 根据配置决定 headful/headless
            if self._force_headless:
                headless = True
            elif local_mode and self.headful_local:
                headless = False
            elif not local_mode and self.headful_cloud:
                # 云端模式启用 headful（需要图形界面支持如 xvfb）
                headless = False
            else:
                headless = True
                
            launch_args = get_browser_launch_args(
                browser_type=self.browser_type,
                headless=headless,
            )
            
            playwright_proxy = None
            crawler_proxy = get_crawler_proxy()
            if crawler_proxy:
                playwright_proxy = {"server": crawler_proxy}
            elif is_proxy_manager_enabled():
                proxy_url = get_proxy_string()
                if proxy_url:
                    playwright_proxy = {"server": proxy_url}
                
            if getattr(self, "use_persistent_context", False):
                profile_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "temp_profiles",
                    f"profile_{self.source_name}_{threading.get_ident()}_{random.randint(1000, 9999)}_{int(time.time())}"
                )
                os.makedirs(profile_dir, exist_ok=True)
                
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
                
                # 注入高级 stealth 脚本（替换旧 playwright_stealth + 内联 JS）
                if self.enable_stealth:
                    apply_stealth(context, browser_type=self.browser_type)
                    
                self.thread_local.profile_dir = profile_dir
            else:
                browser = p.chromium.launch(headless=headless, args=launch_args, proxy=playwright_proxy)
                context = browser.new_context(
                    viewport={'width': 1280, 'height': 900},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    user_agent=ua,
                )
                profile_dir = None
            
            # 注入高级 stealth 脚本（无论是否 persistent context 都执行）
            if self.enable_stealth:
                apply_stealth(context, browser_type=self.browser_type)
                
            self.thread_local.playwright = p
            self.thread_local.browser = browser
            self.thread_local.context = context
            
            with self._resources_lock:
                self._active_resources.append((p, browser, context, profile_dir))
                
        return self.thread_local.playwright, self.thread_local.browser, self.thread_local.context

    def _destroy_thread_resources(self):
        """清理当前线程的 Playwright 资源，以便下一次重新创建"""
        p = getattr(self.thread_local, "playwright", None)
        browser = getattr(self.thread_local, "browser", None)
        context = getattr(self.thread_local, "context", None)
        profile_dir = getattr(self.thread_local, "profile_dir", None)
        
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
            if p:
                p.stop()
        except Exception:
            pass
            
        with self._resources_lock:
            self._active_resources = [
                item for item in self._active_resources
                if item[0] != p
            ]
            
        if profile_dir and os.path.exists(profile_dir):
            try:
                import shutil
                shutil.rmtree(profile_dir)
            except Exception:
                pass
                
        if hasattr(self.thread_local, "playwright"):
            del self.thread_local.playwright
        if hasattr(self.thread_local, "browser"):
            del self.thread_local.browser
        if hasattr(self.thread_local, "context"):
            del self.thread_local.context
        if hasattr(self.thread_local, "profile_dir"):
            del self.thread_local.profile_dir

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
        """获取 PDF 本地临时/持久化保存路径"""
        if self.r2_uploader:
            base = f"/tmp/{self.source_name}_pdfs"
        else:
            from config import PDF_BASE_DIR
            base = PDF_BASE_DIR

        year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
        save_dir = os.path.join(base, year)
        os.makedirs(save_dir, exist_ok=True)

        from utils.metadata_parser import sanitize_filename
        safe_title = sanitize_filename(title)
        base_filename = f"{publish_date}_{safe_title}_{self.source_name}"
        pdf_path = os.path.join(save_dir, f"{base_filename}.pdf")

        counter = 1
        while os.path.exists(pdf_path):
            pdf_path = os.path.join(save_dir, f"{base_filename}_{counter}.pdf")
            counter += 1

        return pdf_path

    def _upload_or_return_pdf_path(self, local_path, publish_date):
        """统一的 PDF R2 上传与相对路径返回逻辑"""
        if not local_path or not os.path.exists(local_path):
            return None
        if self.r2_uploader:
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            remote_key = f"pdfs/{year}/{os.path.basename(local_path)}"
            result = self.r2_uploader.upload_pdf(local_path, remote_key)
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            return result
        else:
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            rel_path = f"pdf/{year}/{os.path.basename(local_path)}"
            return rel_path.replace('\\', '/')


class DomainRotationMixin:
    def _get_domain_cache_path(self):
        """获取域名缓存文件路径（按 source_name 区分）"""
        cache_dir = os.path.join(os.path.dirname(__file__), "..", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, f"{self.source_name}_domains.json")

    def _load_domains_from_cache(self):
        """从本地缓存加载之前发现的最新域名，如果有则替换 self.domains"""
        cache_path = self._get_domain_cache_path()
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, list) and len(cached) > 0:
                    # 验证缓存中的域名格式
                    import re
                    pattern = getattr(self, "domain_pattern", r'([a-z]{2,5}\.\d{5,7}\.xyz)')
                    valid = [d for d in cached if re.search(pattern, d)]
                    if valid:
                        with self._domain_lock:
                            # 去重并保留顺序
                            seen = set()
                            unique_domains = []
                            for d in valid:
                                if d not in seen:
                                    seen.add(d)
                                    unique_domains.append(d)
                            self.domains = unique_domains
                            self.current_domain_idx = 0
                            old_base = self.base_domain
                            self.base_domain = f"https://{self.domains[0]}"
                            if old_base and getattr(self, "base_list_url", None):
                                self.base_list_url = self.base_list_url.replace(old_base, self.base_domain)
                            else:
                                self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
                            self._domain_cooldown.clear()
                        print(f"[+] {self.source_name.upper()} 从缓存加载 {len(unique_domains)} 个域名:")
                        for d in unique_domains:
                            print(f"    - {d}")
                        return True
        except Exception as e:
            print(f"[!] {self.source_name.upper()} 读取域名缓存失败: {e}")
        return False

    def _save_domains_to_cache(self):
        """将当前域名列表保存到本地缓存"""
        cache_path = self._get_domain_cache_path()
        try:
            with self._domain_lock:
                to_save = list(dict.fromkeys(self.domains))  # 去重保序
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=2)
            print(f"[+] {self.source_name.upper()} 域名已缓存至: {cache_path}")
        except Exception as e:
            print(f"[!] {self.source_name.upper()} 保存域名缓存失败: {e}")

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
                    old_base = self.base_domain
                    self.base_domain = f"https://{self.domains[self.current_domain_idx]}"
                    if old_base and getattr(self, "base_list_url", None):
                        self.base_list_url = self.base_list_url.replace(old_base, self.base_domain)
                    else:
                        self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
                    print(f"[!] {self.source_name.upper()} 域名切换至: {self.base_domain}")
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
            else:
                wait_time = random.uniform(2, 5)
                time.sleep(wait_time)
                
            old_base = self.base_domain
            self.current_domain_idx = (self.current_domain_idx + 1) % len(self.domains)
            self.base_domain = f"https://{self.domains[self.current_domain_idx]}"
            if old_base and getattr(self, "base_list_url", None):
                self.base_list_url = self.base_list_url.replace(old_base, self.base_domain)
            else:
                self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
            self._domain_cooldown.pop(self.domains[self.current_domain_idx], None)
            print(f"[!] 冷却结束，{self.source_name.upper()} 域名切换至: {self.base_domain}")

    def _update_domains_from_redirect(self, content):
        """从'正在检测最新可用线路'跳转页面中提取最新域名并更新域名列表
        
        当旧域名失效时，服务器会返回一个包含新域名列表的跳转页面。
        此方法解析该页面，提取新域名并动态更新 self.domains。
        
        Args:
            content: 跳转页面的 HTML 内容（可以是原始或解密后的）
            
        Returns:
            True 如果域名列表已更新，False 如果无变化或未检测到跳转页
        """
        if not content or "正在检测最新可用线路" not in content:
            return False
        
        import re
        # 匹配域名格式
        pattern = getattr(self, "domain_pattern", r'([a-z]{2,5}\.\d{5,7}\.xyz)')
        new_domains = re.findall(pattern, content)
        if not new_domains:
            print(f"[!] 检测到跳转页面但未能提取到域名")
            return False
        
        # 去重并保留顺序
        seen = set()
        unique_domains = []
        for d in new_domains:
            if d not in seen:
                seen.add(d)
                unique_domains.append(d)
        
        with self._domain_lock:
            old_set = set(self.domains)
            new_set = set(unique_domains)
            
            if new_set == old_set:
                return False  # 域名没有变化，无需更新
            
            old_domains = self.domains[:]
            self.domains = unique_domains
            self.current_domain_idx = 0
            old_base = self.base_domain
            self.base_domain = f"https://{self.domains[0]}"
            if old_base and getattr(self, "base_list_url", None):
                self.base_list_url = self.base_list_url.replace(old_base, self.base_domain)
            else:
                self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
            self._domain_cooldown.clear()
            
            added = new_set - old_set
            removed = old_set - new_set
            print(f"[+] {self.source_name.upper()} 检测到域名变更！提取到 {len(unique_domains)} 个最新域名:")
            for i, d in enumerate(unique_domains, 1):
                tag = " [新]" if d in added else ""
                print(f"    域名{i}: {d}{tag}")
            if removed:
                print(f"    已失效: {', '.join(removed)}")
        
        # 自动持久化到本地缓存
        self._save_domains_to_cache()
        
        return True

    def _fetch_domains_from_main_station(self):
        """从主站域名动态获取最新可用镜像域名列表"""
        if not getattr(self, "main_domain", None):
            return False
        
        print(f"[*] {self.source_name.upper()} 开始从主站 {self.main_domain} 动态获取最新域名列表...", flush=True)
        headers = self._build_headers(referer=self.main_domain)
        
        # 1. 优先获取代理
        proxies = None
        from config import get_crawler_proxy, is_proxy_manager_enabled
        crawler_proxy = get_crawler_proxy()
        if crawler_proxy:
            proxies = {"http": crawler_proxy, "https": crawler_proxy}
        elif is_proxy_manager_enabled():
            from utils.proxy_manager import get_proxy_dict
            proxies = get_proxy_dict()
            
        html = None
        # Perf 4: curl_cffi 与 Playwright 并发请求，谁先完成用谁
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading
        
        html_lock = threading.Lock()
        
        def _curl_fetch():
            try:
                from curl_cffi import requests as curl_requests
                resp = curl_requests.get(self.main_domain, headers=headers, timeout=15, proxies=proxies, impersonate="chrome120")
                if resp.status_code == 200:
                    return resp.text
            except Exception as e:
                print(f"[-] curl_cffi 请求主站 {self.main_domain} 失败: {e}", flush=True)
                if proxies and is_proxy_manager_enabled():
                    from utils.proxy_manager import get_proxy_manager
                    manager = get_proxy_manager()
                    if manager and "http" in proxies:
                        manager.report_failure(proxies["http"])
            return None
        
        def _pw_fetch():
            nonlocal html
            if not hasattr(self, "_get_thread_resources"):
                return None
            print(f"[*] Playwright 并发访问主站: {self.main_domain}", flush=True)
            page = None
            try:
                _, _, context = self._get_thread_resources()
                page = context.new_page()
                page.goto(self.main_domain, timeout=30000, wait_until="domcontentloaded")
                import time
                time.sleep(3.0)
                return page.content()
            except Exception as e:
                print(f"[-] Playwright 访问主站 {self.main_domain} 异常: {e}", flush=True)
                self._destroy_thread_resources()
                return None
            finally:
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_curl = executor.submit(_curl_fetch)
            fut_pw = executor.submit(_pw_fetch)
            for fut in as_completed([fut_curl, fut_pw]):
                result = fut.result()
                if result:
                    with html_lock:
                        if html is None:
                            html = result
                            # 取消另一个还在跑的请求（如果有必要）
                            # 注意：as_completed 不会取消其它 future，我们只是优先使用先到的结果
                            break
        
        if not html:
            print(f"[!] 无法从主站 {self.main_domain} 获取到页面内容", flush=True)
            return False
            
        # 4. 尝试解密 HTML
        decrypted = None
        if hasattr(self, "decrypt_html"):
            decrypted = self.decrypt_html(html)
            
        content_to_parse = decrypted if decrypted else html
        
        # 5. 提取域名
        import re
        pattern = getattr(self, "domain_pattern", r'([a-z]{2,5}\.\d{5,7}\.xyz)')
        new_domains = re.findall(pattern, content_to_parse)
        if not new_domains:
            print(f"[!] 未能在主站内容中匹配提取到镜像域名 (正则: {pattern})", flush=True)
            return False
            
        # 去重并保留顺序，排除主站本身
        seen = set()
        unique_domains = []
        for d in new_domains:
            if d in self.main_domain:
                continue
            if d not in seen:
                seen.add(d)
                unique_domains.append(d)
                
        if not unique_domains:
            print(f"[!] 解析出的镜像域名列表为空", flush=True)
            return False
            
        with self._domain_lock:
            self.domains = unique_domains
            self.current_domain_idx = 0
            old_base = self.base_domain
            self.base_domain = f"https://{self.domains[0]}"
            if old_base and getattr(self, "base_list_url", None):
                self.base_list_url = self.base_list_url.replace(old_base, self.base_domain)
            else:
                self.base_list_url = f"{self.base_domain}/list.php?class={{}}&page={{}}"
            self._domain_cooldown.clear()
            
        print(f"[+] 成功从主站拉取并更新了 {len(unique_domains)} 个最新域名:")
        for i, d in enumerate(unique_domains, 1):
            print(f"    域名{i}: {d}")
            
        # 持久化到缓存
        self._save_domains_to_cache()
        return True


class DecryptMixin:
    def decrypt_html(self, raw_html):
        """解密目标网站动态混淆的 HTML"""
        import base64
        import re
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
        """解密详情页或列表页的加密标题"""
        import base64
        try:
            return base64.b64decode(encrypted_title_b64).decode('utf-8')
        except Exception as e:
            print(f"[-] 标题解密失败: {e}")
            return ""

