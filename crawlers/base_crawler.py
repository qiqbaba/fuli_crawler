import os
import time
import random
from utils.date_parser import parse_date
from utils.metadata_parser import parse_title, parse_link_metadata, parse_pikpak_link

class BaseCrawler:
    def __init__(self, db_manager, source_name):
        self.db_manager = db_manager
        self.source_name = source_name
        self.max_consecutive_existing = 20
        self.max_consecutive_duplicate_pages = None
        self.check_resource_link = False  # 是否额外检查 resource_link（磁力链接）去重，子类按需开启

    def on_start(self):
        """生命周期钩子：爬网开始前（子类可选覆盖）"""
        pass

    def on_finish(self):
        """生命周期钩子：爬网结束后（子类可选覆盖）"""
        pass

    def cleanup_thread_resources(self):
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

                if not items_to_process:
                    print(f"[+] 页面 {page_num} 所有项均已被跳过。")
                    # 保存断点（每页完成时记录）
                    if class_name is not None:
                        self._save_page_state(class_name, page_num + 1)
                    if early_stop_triggered or (self.max_consecutive_existing is not None and consecutive_count >= self.max_consecutive_existing):
                        print(f"\n[任务结束] 爬虫已追溯到历史抓取位置，安全退出翻页循环。")
                        early_break = True
                        break
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
                                _, data = res
                                if data:
                                    results_dict[idx] = data
                        except Exception as e:
                            print(f"[-] 处理索引为 [{idx}] 的项目时发生异常: {e}")
                else:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        future_to_idx = {
                            executor.submit(self.process_sub_page_if_needed, raw_item, idx): idx
                                for idx, raw_item in items_to_process
                        }

                        for future in as_completed(future_to_idx):
                            idx = future_to_idx[future]
                            try:
                                res = future.result()
                                if res:
                                    _, data = res
                                    if data:
                                        results_dict[idx] = data
                            except Exception as e:
                                print(f"[-] 线程处理索引为 [{idx}] 的项目时发生异常: {e}")

                        print("[*] 正在向并发工作线程发送资源清理指令...")
                        cleanup_futures = [
                            executor.submit(self.cleanup_thread_resources)
                            for _ in range(max_workers)
                        ]
                        for f in as_completed(cleanup_futures):
                            try:
                                f.result()
                            except Exception as e:
                                print(f"[-] 清理工作线程资源时发生异常: {e}")

                results = [results_dict[idx] for idx in sorted(results_dict.keys())]

                if results:
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

                # 保存断点状态（每页完成时记录）
                if class_name is not None:
                    self._save_page_state(class_name, page_num + 1)

                if early_stop_triggered or (self.max_consecutive_existing is not None and consecutive_count >= self.max_consecutive_existing):
                    print(f"\n[任务结束] 爬虫已追溯到历史抓取位置，安全退出翻页循环。")
                    early_break = True
                    break

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
        
        if resume:
            print(f"[*] 启用断点续爬模式，将自动跳过已完成的板块/页面")
        
        print(f"[*] 启动 {self.source_name} 爬虫流程...")
        self.on_start()

        no_early_stop = kwargs.get('no_early_stop', False)
        if no_early_stop:
            print("[*] 禁用早停机制，将强制爬取指定范围内所有页面。")
            self.max_consecutive_existing = None
            self.max_consecutive_duplicate_pages = None

        if max_workers is None:
            max_workers = 10 if self.source_name in ("seju", "datang") else 50

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
