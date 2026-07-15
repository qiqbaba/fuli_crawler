import os
import json
import time
import random
import threading
from dataclasses import dataclass, field
from typing import List, Optional
from utils.pdf_generator import PDFGenerator, PDFRenderConfig
from utils.date_parser import parse_date
from utils.metadata_parser import parse_title, parse_link_metadata, parse_pikpak_link
from utils.lang_filter import is_japanese, batch_is_japanese
from utils.logger import get_logger
from config import USER_AGENTS
from curl_cffi import requests

logger = get_logger(__name__)


@dataclass
class CrawlConfig:
    """爬虫配置数据类，替代零散的类属性

    为 DecryptSiteBaseCrawler 及其子类提供统一配置入口。
    初始化后可通过 config.domains / config.base_domain 等访问派生属性。
    """
    source_name: str
    categories: List[str] = field(default_factory=list)
    initial_domains: List[str] = field(default_factory=list)
    main_domain: str = ""
    domain_pattern: str = r'([a-z]{2,5}\.\d{5,7}\.xyz)'
    list_url_template: str = "{base}/list.php?class={cat}&page={page}"
    check_resource_link: bool = True
    max_consecutive_existing: Optional[int] = 15

    # ---- 派生属性（由 init 后调用 build() 生成） ----
    domains: List[str] = field(init=False)
    base_domain: str = field(init=False)
    base_list_url: str = field(init=False)

    def __post_init__(self):
        self.domains = list(self.initial_domains)
        if self.main_domain:
            self.base_domain = self.main_domain
        elif self.domains:
            self.base_domain = f"https://{self.domains[0]}"
        else:
            self.base_domain = ""
        self.base_list_url = self.list_url_template.format(
            base=self.base_domain,
            cat="{cat}",
            page="{page}"
        )


class BaseCrawler:
    def __init__(self, db_manager, source_name):
        self.db_manager = db_manager
        self.source_name = source_name
        self.max_consecutive_existing = 20
        self.max_consecutive_duplicate_pages = None
        self.check_resource_link = False  # 是否额外检查 resource_link（磁力链接）去重，子类按需开启
        self.skip_japanese = True  # 是否跳过日语标题
        self.is_test = False
        self.quiet = False  # 静音模式，在 run() 中可被覆盖

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
            base = getattr(self, 'base_domain', None) or getattr(self, 'base_url', None) or ""
            headers["Referer"] = base + "/"
        return headers

    def _http_get(self, url, timeout=20, impersonate="chrome120"):
        """通用 HTTP GET 请求，最多重试 3 次，支持代理自动切换和失败上报"""
        from curl_cffi import requests
        from config import get_effective_proxy, is_proxy_manager_enabled
        from utils.proxy_manager import get_proxy_manager

        # 预先获取代理管理器实例（移出重试循环，只执行一次）
        proxy_manager = get_proxy_manager() if is_proxy_manager_enabled() else None

        for attempt in range(1, 4):
            proxies = None
            try:
                ua = random.choice(USER_AGENTS)
                headers = {"User-Agent": ua}

                proxies = get_effective_proxy()

                r = requests.get(url, headers=headers, impersonate=impersonate, timeout=timeout, proxies=proxies)
                r.encoding = 'utf-8'
                if r.status_code == 200:
                    return r.url, r.text
                else:
                    logger.error("HTTP 请求失败 (%s) [第 %s/3 次尝试]: 状态码 %s", url, attempt, r.status_code)
                    if r.status_code in (403, 407, 502, 503, 504) and proxies and proxy_manager:
                        if "http" in proxies:
                            proxy_manager.report_failure(proxies["http"])
            except Exception as e:
                logger.error("HTTP 请求异常 (%s) [第 %s/3 次尝试]: %s", url, attempt, e)
                if proxies and proxy_manager:
                    if "http" in proxies:
                        proxy_manager.report_failure(proxies["http"])

            if attempt < 3:
                time.sleep(random.uniform(1.0, 3.0))

        return url, None

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
        logger.info("【测试模式】正在抓取第一页以提供测试数据...")
        list_content = self.fetch_list_page(start_page)
        if not list_content:
            logger.error("抓取列表页测试数据失败。")
            return

        raw_items = self.parse_list_page(list_content, start_page)
        logger.info("列表页解析完成，共找到 %s 条记录。", len(raw_items))

        test_items = raw_items[:5]
        logger.info("\n================ 进行前 5 条数据测试 ================")
        for idx, raw_item in enumerate(test_items, 1):
            res = self.process_sub_page_if_needed(raw_item, idx)
            if not res:
                logger.info("[%s] 提取测试失败", idx)
                continue
            is_existing, data = res
            if data:
                logger.info("[%s] 状态 (已存在数据库: %s)", idx, is_existing)
                logger.info("    - title (标题): %s", data['title'])
                logger.info("    - publish_time (发布时间): %s", data['publish_time'])
                logger.info("    - category (分类): %s", data['category'])
                logger.info("    - resource_link (资源链接): %s...", data['resource_link'][:80])
                logger.info("    - pikpak_link (PikPak): %s", data['pikpak_link'])
                logger.info("    - size (大小): %s", data['size'])
                logger.info("    - resource_format (格式): %s", data['resource_format'])
                logger.info("    - url (链接): %s", data['url'])
                logger.info("    - pdf_path (PDF): %s", data['pdf_path'])
                print("-" * 80)
        logger.info("测试完毕。")

    def _save_page_state(self, class_name, page_num, completed=False):
        """保存当前爬取进度到数据库（断点续爬）"""
        try:
            self.db_manager.save_crawl_state(self.source_name, class_name, page_num, completed=completed)
        except Exception as e:
            logger.warning("保存爬虫断点状态失败: %s", e)

    def _filter_new_items(self, raw_items, existing_urls, consecutive_count):
        """URL级去重过滤 — 分离自 _crawl_pages 的过滤逻辑
        
        Args:
            raw_items: 解析后的原始项列表
            existing_urls: 已存在于数据库中的 URL 集合
            consecutive_count: 当前连续已存在计数
            
        Returns:
            (items_to_process, skipped_count, consecutive_count, early_stop_triggered)
        """
        items_to_process = []
        skipped_count = 0
        early_stop_triggered = False

        # 批量日语预过滤：先收集所有标题，一次性调用 batch_is_japanese
        if self.skip_japanese:
            titles = [
                raw_item.get('title', '') if isinstance(raw_item, dict) else ''
                for raw_item in raw_items
            ]
            is_jp_results = batch_is_japanese(titles)
        else:
            is_jp_results = [False] * len(raw_items)

        for idx, raw_item in enumerate(raw_items, 1):
            url = raw_item if isinstance(raw_item, str) else raw_item.get('url')
            
            # 过滤空 URL，避免 None in existing_urls 永远返回 False 导致重复数据
            if not url:
                continue
                
            is_existing = url in existing_urls

            # 日语标题预过滤（使用批量检测结果）
            if not is_existing and is_jp_results[idx - 1]:
                if not self.quiet:
                    title_preview = raw_item.get('title', '')[:40]
                    logger.info("[%s] 标题检测为日语，跳过: %s", idx, title_preview)
                continue

            if is_existing:
                skipped_count += 1
                if self.max_consecutive_existing is not None:
                    consecutive_count += 1
                    if not self.quiet:
                        logger.info("[%s] 网址已存在数据库中，跳过抓取: %s", idx, url)
                        logger.info("[*] 连续发现已存在数据: %s/%s", consecutive_count, self.max_consecutive_existing)
                        if consecutive_count >= self.max_consecutive_existing:
                            logger.info("\n[触发停止条件] 连续 %s 条数据已存在，停止处理当前页！", self.max_consecutive_existing)
                            early_stop_triggered = True
                            break
                else:
                    if not self.quiet:
                        logger.info("[%s] 网址已存在数据库中，跳过抓取: %s", idx, url)
            else:
                items_to_process.append((idx, raw_item))
                consecutive_count = 0

        return items_to_process, skipped_count, consecutive_count, early_stop_triggered

    def _check_consecutive_subpage_stop(self, idx, is_existing, consecutive_subpage_count):
        """检查子页面级连续已存在计数，返回 (new_count, should_stop)"""
        if not is_existing or self.max_consecutive_existing is None:
            return consecutive_subpage_count, False
        consecutive_subpage_count += 1
        if not self.quiet:
            logger.info("[%s] 子页面级去重跳过: 连续已存在计数: %s/%s",
                        idx, consecutive_subpage_count, self.max_consecutive_existing)
        should_stop = consecutive_subpage_count >= self.max_consecutive_existing
        return consecutive_subpage_count, should_stop

    def _process_items_concurrently(self, items_to_process, max_workers, consecutive_subpage_count):
        """并发处理子页面 — 分离自 _crawl_pages 的并发处理逻辑
        
        使用预分配 dict 按 idx 索引，避免排序开销。
        
        Args:
            items_to_process: 待处理项列表 [(idx, raw_item), ...]
            max_workers: 最大并发线程数
            consecutive_subpage_count: 当前子页面级连续已存在计数
            
        Returns:
            (results_dict, consecutive_subpage_count, early_stop_triggered)
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results_dict = {}
        early_stop_triggered = False

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(self.process_sub_page_if_needed, raw_item, idx): idx
                for idx, raw_item in items_to_process
            }

            # 预分配 dict，按 idx 索引，避免后续排序
            collected_results = {idx: None for idx, _ in items_to_process}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    res = future.result()
                    if res:
                        collected_results[idx] = res
                except Exception as e:
                    logger.error("线程处理索引为 [%s] 的项目时发生异常: %s", idx, e)

            # 按预分配顺序遍历（Python 3.7+ 保持插入顺序），无需排序
            for idx in collected_results:
                res = collected_results[idx]
                if res is None:
                    continue
                is_existing, data = res
                consecutive_subpage_count, should_stop = self._check_consecutive_subpage_stop(
                    idx, is_existing, consecutive_subpage_count
                )
                if should_stop:
                    early_stop_triggered = True
                if data:
                    results_dict[idx] = data

            logger.info("正在关闭线程池并自动清理资源...")
            executor.shutdown(wait=True)

        if early_stop_triggered:
            logger.warning("子页面级去重触发早停标记，已完成的结果将继续入库。")

        return results_dict, consecutive_subpage_count, early_stop_triggered

    def _write_results_to_db(self, results, consecutive_count):
        """写入结果到数据库并更新计数 — 分离自 _crawl_pages 的写入逻辑
        
        Args:
            results: 待写入的结构化数据列表
            consecutive_count: 当前连续已存在计数
            
        Returns:
            (inserted_count, skipped_count, consecutive_count, early_stop_triggered)
        """
        inserted_count = 0
        skipped_count = 0
        early_stop_triggered = False

        # 日语标题后置过滤（批量检测）
        if self.skip_japanese:
            titles = [d.get('title', '') for d in results]
            is_jp_results = batch_is_japanese(titles)
            filtered_jp = []
            jp_skipped = 0
            for d, is_jp in zip(results, is_jp_results):
                if is_jp:
                    jp_skipped += 1
                    if not self.quiet:
                        logger.info("[*] 结果标题检测为日语，过滤: %s", d.get('title', '')[:40])
                else:
                    filtered_jp.append(d)
            if jp_skipped > 0:
                logger.info("[*] 日语标题后置过滤掉 %s 条", jp_skipped)
            results = filtered_jp

        # 磁力链接二次去重
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
                        logger.info("[*] 磁力链接已存在数据库中，跳过: %s...", link[:60])
                else:
                    filtered_results.append(d)
            if resource_link_skipped > 0:
                logger.info("[*] 磁力链接去重过滤掉 %s 条", resource_link_skipped)
            results = filtered_results

        logger.info("[*] 正在写入 %s 条新纪录到数据库...", len(results))
        for data in results:
            success = self.db_manager.insert_resource(data)
            if success:
                inserted_count += 1
                consecutive_count = 0
            else:
                skipped_count += 1
                if self.max_consecutive_existing is not None:
                    consecutive_count += 1
                    if not self.quiet:
                        logger.info("[*] 写入失败或重复 (DB IGNORE)，连续已存在计数: %s/%s", consecutive_count, self.max_consecutive_existing)
                    if consecutive_count >= self.max_consecutive_existing:
                        early_stop_triggered = True
                        break
                else:
                    if not self.quiet:
                        logger.info("[*] 写入失败或重复 (DB IGNORE)")

        return inserted_count, skipped_count, consecutive_count, early_stop_triggered

    def _check_early_stop_condition(self, consecutive_count, consecutive_subpage_count):
        """检查是否触发早停条件（连续已存在数据超过阈值）
        
        Args:
            consecutive_count: URL 级连续已存在计数
            consecutive_subpage_count: 子页面级连续已存在计数
            
        Returns:
            是否应早停
        """
        if self.max_consecutive_existing is None:
            return False
        return max(consecutive_count, consecutive_subpage_count) >= self.max_consecutive_existing

    def _crawl_pages(self, start_page, end_page, max_workers, class_name=None):
        """
        爬虫页面循环核心逻辑（不含 on_start/on_finish），
        供 DatangCrawler 多板块循环复用。
        
        class_name: 可选，用于断点续爬时记录当前板块名称
        """
        consecutive_count = 0
        consecutive_subpage_count = 0
        consecutive_duplicate_pages = 0
        from config import is_local_mode
        is_gha = not is_local_mode()
        early_break = False

        for page_num in range(start_page, end_page + 1):
            if is_gha:
                print(f"::group::正在抓取第 {page_num}/{end_page} 页", flush=True)
            else:
                logger.info("\n================ 正在抓取第 %s/%s 页 ================", page_num, end_page)

            try:
                list_content = self.fetch_list_page(page_num)
                if not list_content:
                    logger.error("页面 %s 抓取失败或无内容，跳过。", page_num)
                    continue

                raw_items = self.parse_list_page(list_content, page_num)
                if not raw_items:
                    logger.error("页面 %s 未提取到有效项。", page_num)
                    time.sleep(random.uniform(1.0, 2.0))
                    continue

                logger.info("本页共解析到 %s 条记录。", len(raw_items))

                # === 步骤 1: URL 级去重过滤 ===
                urls_to_check = [raw_item if isinstance(raw_item, str) else raw_item.get('url') for raw_item in raw_items]
                existing_urls = self.db_manager.filter_existing_urls(urls_to_check)
                items_to_process, skipped_count, consecutive_count, early_stop_triggered = self._filter_new_items(
                    raw_items, existing_urls, consecutive_count
                )

                # === 步骤 2: 整页重复检测 ===
                if not early_stop_triggered:
                    is_page_duplicate = (skipped_count == len(raw_items))
                    if is_page_duplicate:
                        if self.max_consecutive_duplicate_pages is not None:
                            consecutive_duplicate_pages += 1
                            if not self.quiet:
                                logger.info("[*] 当前页所有数据均已重复。连续重复页数: %s/%s", consecutive_duplicate_pages, self.max_consecutive_duplicate_pages)
                            if consecutive_duplicate_pages >= self.max_consecutive_duplicate_pages:
                                early_stop_triggered = True
                    else:
                        consecutive_duplicate_pages = 0

                # === 步骤 3: 早停或无待处理项 ===
                if not items_to_process or early_stop_triggered:
                    if not items_to_process:
                        logger.info("页面 %s 所有项均已被跳过。", page_num)
                    else:
                        logger.info("页面 %s 触发早停，跳过 %s 条待处理项。", page_num, len(items_to_process))
                    if class_name is not None:
                        self._save_page_state(class_name, page_num + 1)
                    if early_stop_triggered or self._check_early_stop_condition(consecutive_count, consecutive_subpage_count):
                        logger.info("\n[任务结束] 爬虫已追溯到历史抓取位置，安全退出翻页循环。")
                        early_break = True
                        break
                    self._sleep_between_pages(self.no_pdf)
                    continue

                # === 步骤 4: 并发处理子页面 ===
                logger.info("[*] 开始并发处理 %s 条新纪录 (并发线程数: %s)...", len(items_to_process), max_workers)
                results_dict, consecutive_subpage_count, subpage_early_stop = self._process_items_concurrently(
                    items_to_process, max_workers, consecutive_subpage_count
                )
                if subpage_early_stop:
                    early_stop_triggered = True

                # === 步骤 5: 写入数据库 ===
                results = [results_dict[idx] for idx in sorted(results_dict.keys())]
                if results:
                    inserted_count, db_skipped, consecutive_count, db_early_stop = self._write_results_to_db(results, consecutive_count)
                    if db_early_stop:
                        early_stop_triggered = True
                else:
                    inserted_count = 0
                    db_skipped = 0

                logger.info("页面 %s 处理完成：写入 %s 条，跳过 %s 条。", page_num, inserted_count, skipped_count + db_skipped)
                self.db_manager.commit()

                if class_name is not None:
                    self._save_page_state(class_name, page_num + 1)

                if early_stop_triggered or self._check_early_stop_condition(consecutive_count, consecutive_subpage_count):
                    logger.info("\n[任务结束] 爬虫已追溯到历史抓取位置，安全退出翻页循环。")
                    early_break = True
                    break

                self._sleep_between_pages(self.no_pdf)
            finally:
                if is_gha:
                    print("::endgroup::", flush=True)
        
        if class_name is not None and not early_break:
            self._save_page_state(class_name, end_page + 1)

    def _sleep_between_pages(self, no_pdf):
        """页面间延迟等待，降低被反爬检测的风险"""
        if no_pdf:
            time.sleep(random.uniform(0.3, 0.8))
        else:
            time.sleep(random.uniform(1.5, 3.0))

    def get_categories(self):
        """钩子方法：返回要爬取的分类列表，子类可覆盖"""
        return []

    def before_category_crawl(self, category):
        """钩子方法：爬取分类前的准备工作，子类可覆盖"""
        pass

    def run(self, is_test=False, start_page=1, end_page=1, max_workers=None, **kwargs):
        """
        统一的爬虫执行骨架，支持多板块爬取
        """
        self.is_test = is_test
        self.quiet = kwargs.get('quiet', False)
        resume = kwargs.get('resume', False)
        self.resume = resume
        self.no_pdf = kwargs.get('no_pdf', False)
        
        if resume:
            logger.info("[*] 启用断点续爬模式，将自动跳过已完成的板块/页面")
        if self.no_pdf:
            logger.info("[*] 启用无 PDF 渲染模式，将跳过 PDF 生成与图片下载")
        
        logger.info("[*] 启动 %s 爬虫流程...", self.source_name)
        self.on_start()

        no_early_stop = kwargs.get('no_early_stop', False)
        if no_early_stop:
            logger.info("[*] 禁用早停机制，将强制爬取指定范围内所有页面。")
            self.max_consecutive_existing = None
            self.max_consecutive_duplicate_pages = None
        elif not resume:
            # 非断点续爬模式时，禁用早停（避免数据库已有历史数据时误触发提前退出）
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

            categories = self.get_categories()
            if categories:
                # 多板块爬取模式
                resume_category = None
                resume_page = start_page

                if resume:
                    all_states = self.db_manager.load_crawl_state(self.source_name)
                    if all_states:
                        if "__all__" in all_states and all_states["__all__"].get("completed", False):
                            logger.info("[*] 检测到 %s 已完成全部爬取，跳过所有板块", self.source_name)
                            self.db_manager.clear_crawl_state(self.source_name)
                            logger.info("[+] 已清除完成标记，下次运行将重新爬取")
                            return
                        
                        for category in categories:
                            state = all_states.get(category)
                            if state:
                                saved_page = state["page_num"]
                                if saved_page <= end_page:
                                    resume_category = category
                                    resume_page = saved_page
                                    logger.info("[*] 检测到板块 %s 爬取断点，从第 %s 页继续", category, resume_page)
                                    break
                                logger.info("[断点续爬] 板块 %s 已完成，跳过", category)
                            else:
                                resume_category = category
                                resume_page = start_page
                                logger.info("[*] 板块 %s 无历史记录，从头开始爬取", category)
                                break
                        else:
                            logger.info("[*] 所有板块已完成，无需爬取")
                            return
                    else:
                        logger.info("[*] 未检测到历史断点，从头开始爬取")

                for category in categories:
                    if resume and resume_category is not None:
                        category_index = categories.index(category)
                        resume_index = categories.index(resume_category)
                        if category_index < resume_index:
                            logger.info("\n[断点续爬] 板块 %s 已完成，跳过", category)
                            continue
                        if category == resume_category:
                            actual_start = resume_page
                        else:
                            actual_start = start_page
                    else:
                        actual_start = start_page

                    self.before_category_crawl(category)
                    logger.info("\n[*] ================= 开始爬取板块: %s (起始页码: %s) =================", category, actual_start)
                    self._crawl_pages(actual_start, end_page, max_workers, class_name=category)
                    
                    if resume and category == resume_category:
                        resume_category = None
                
                if not is_test and resume:
                    self.db_manager.mark_source_completed(self.source_name)
                    logger.info("[+] %s 所有板块爬取完成，已标记完成状态", self.source_name)
            else:
                # 单板块爬取模式
                self._crawl_pages(start_page, end_page, max_workers)

        except KeyboardInterrupt:
            logger.warning("\n[中断] 检测到用户手动停止运行 (Ctrl+C)")
        except Exception as e:
            logger.error("\n[致命错误] 运行中发生未捕获的异常: %s", e)
        finally:
            self.on_finish()


from utils.browser_factory import browser_factory

class PlaywrightBaseCrawler(BaseCrawler):
    def __init__(self, db_manager, source_name):
        super().__init__(db_manager, source_name)
        self.r2_uploader = None
        self.pdf_generator = None
        self.use_persistent_context = False
        # Perf 1: 是否跨页面复用浏览器，避免每页销毁重建
        self._reuse_browser = True
        # ========== Anti-detection 配置 ==========
        from config import is_headless_forced, HEADFUL_LOCAL, HEADFUL_CLOUD, is_stealth_enabled, is_local_mode
        # 浏览器类型固定为 chromium
        self.browser_type = "chromium"
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
        self.pdf_generator = PDFGenerator(self.r2_uploader)
        if not self.quiet:
            if self.r2_uploader:
                logger.info("[*] Cloudflare R2 上传器已启用 (%s)", self.source_name)
            else:
                if is_local_mode():
                    logger.info("[*] 本地模式已激活，PDF 将保存到本地目录 (%s)", self.source_name)
                else:
                    logger.info("[*] 未配置 R2 环境变量，PDF 将保存到本地目录 (%s)", self.source_name)
        
        # 初始化代理管理器
        from config import is_proxy_manager_enabled
        if is_proxy_manager_enabled():
            if not self.quiet:
                logger.info("[*] 代理管理器已启用，正在获取和验证代理IP...")
            from utils.proxy_manager import get_proxy_manager
            from config import get_proxy_verify_workers
            try:
                manager = get_proxy_manager()
                if manager:
                    manager.fetch_proxies(force=False)
                    test_url = getattr(self, 'proxy_test_url', None) or getattr(self, 'base_domain', None) or getattr(self, 'base_url', None)
                    expected_content = getattr(self, 'proxy_expected_content', None)
                    manager.verify_proxies(
                        force=False, 
                        max_workers=get_proxy_verify_workers(), 
                        test_url=test_url, 
                        expected_content=expected_content
                    )
                    stats = manager.get_stats()
                    if not self.quiet:
                        logger.info("[*] 代理管理器就绪: 总计 %s 个，可用 %s 个", stats['total'], stats['working'])
            except Exception as e:
                if not self.quiet:
                    logger.error("初始化代理管理器时发生异常: %s", e)

    def release_thread_resources(self):
        """
        生命周期钩子：释放当前工作线程持有的 Playwright 资源
        
        当 _reuse_browser=True 时（默认），页面间只清理页面级资源，不销毁浏览器，
        由 on_finish 统一清理，避免每页销毁重建的开销。
        """
        if not self._reuse_browser:
            # 旧行为：每页销毁并重建浏览器
            self._destroy_thread_resources()
        else:
            # Perf 1: 仅清理页面级资源，保留浏览器供下一页复用
            try:
                from utils.browser_factory import browser_factory
                # 使用 browser_factory 的线程本地存储，确保正确访问当前线程的 page 对象
                thread_local = browser_factory._thread_local
                page = getattr(thread_local, "page", None)
                if page:
                    page.close()
                    del thread_local.page
                    logger.info("[+] 已关闭页面资源，保留浏览器供复用")
            except Exception as e:
                logger.warning("关闭页面资源失败: %s", e)

    def on_finish(self):
        """释放 Playwright 渲染资源"""
        logger.info("[*] 正在释放主线程 Playwright 资源 (%s)...", self.source_name)
        # 1. 优先清理主线程自身的资源
        self.release_thread_resources()
        
        self.db_manager.commit()

    def _get_thread_resources(self, no_proxy=False):
        """获取当前线程特有的 Playwright 实例
        
        Args:
            no_proxy: 若为 True，则 Playwright 启动时跳过代理配置（直连）
        """
        from config import is_local_mode
        local_mode = is_local_mode()
        
        # 根据配置决定 headless/headful
        if self._force_headless:
            headless = True
        elif local_mode and self.headful_local:
            headless = False
        elif not local_mode and self.headful_cloud:
            # 云端模式启用 headful（需要图形界面支持如 xvfb）
            headless = False
        else:
            headless = True
            
        return browser_factory.create_browser_context(
            headless=headless,
            browser_type=self.browser_type,
            enable_stealth=self.enable_stealth,
            use_persistent_context=self.use_persistent_context,
            no_proxy=no_proxy
        )

    def _destroy_thread_resources(self):
        """清理当前线程的 Playwright 资源，以便下一次重新创建"""
        browser_factory.destroy_thread_resources()

    def _save_pdf(self, page_or_url, publish_date, title, no_proxy=False):
        """Playwright 统一保存 PDF 的向后兼容包装方法
        
        Args:
            no_proxy: 若为 True，则 Playwright 启动时跳过代理配置（直连）
        """
        if getattr(self, 'no_pdf', False):
            return ""
        if not self.pdf_generator:
            self.pdf_generator = PDFGenerator(self.r2_uploader)
        
        config = getattr(self, "pdf_config", None)
        if not config:
            config = PDFRenderConfig()
            
        _, _, context = self._get_thread_resources(no_proxy=no_proxy)
        return self.pdf_generator.generate_pdf(
            page_or_context=context,
            target_url_or_page=page_or_url,
            publish_date=publish_date,
            title=title,
            source_name=self.source_name,
            config=config
        )

    def retry_generate_pdf(self, url_or_page, publish_date, title, max_retries=3,
                           destroy_on_retry=True, no_proxy_last=False, after_destroy_cb=None):
        """统一的 PDF 生成重试逻辑

        Args:
            url_or_page: 详情页 URL 或 Playwright Page 对象
            publish_date: 发布日期
            title: 标题
            max_retries: 最大重试次数 (默认 3)
            destroy_on_retry: 重试前是否销毁 Playwright 资源 (默认 True)
            no_proxy_last: 最后一次重试是否使用直连 (默认 False)
            after_destroy_cb: 资源销毁后的回调 (用于 seju 等需要重建页面的场景)

        Returns:
            PDF 路径 (成功) 或空字符串 (全部失败)
        """
        if getattr(self, 'no_pdf', False):
            return ""
        if getattr(self, 'is_test', False):
            return ""

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                no_proxy = no_proxy_last and (attempt == max_retries)
                if no_proxy:
                    logger.error("[PDF-SAVE] 标题: %s 前%s次代理均失败，第%s次尝试直连...", title, max_retries - 1, max_retries)
                    try:
                        self._destroy_thread_resources()
                    except Exception:
                        pass
                    time.sleep(random.uniform(1.0, 2.0))
                    if after_destroy_cb:
                        after_destroy_cb()

                saved_path = self._save_pdf(url_or_page, publish_date, title, no_proxy=no_proxy)
                if saved_path:
                    logger.info("[PDF-SAVE] 标题: %s -> PDF 路径: %s", title, saved_path)
                    return saved_path
                else:
                    last_error = f"第 {attempt}/{max_retries} 次尝试返回空"
                    logger.error("[PDF-SAVE] 标题: %s 生成 PDF 失败，进行第 %s/%s 次尝试", title, attempt, max_retries)
            except Exception as e:
                last_error = f"第 {attempt}/{max_retries} 次尝试异常: {e}"
                logger.error("[PDF-SAVE] 标题: %s %s", title, last_error)

            if attempt < max_retries:
                if destroy_on_retry and not no_proxy:
                    try:
                        self._destroy_thread_resources()
                    except Exception as recreate_err:
                        logger.warning("[!] 重构 Playwright 资源失败: %s", recreate_err)
                time.sleep(random.uniform(1.5, 3.0))

        logger.error("[PDF-SAVE] 标题: %s 生成 PDF 失败 (已达最大重试次数): %s", title, last_error)
        return ""


class DomainRotationMixin:
    def __init_subclass__(cls, **kwargs):
        """确保子类实例化时 _domain_lock 被初始化"""
        super().__init_subclass__(**kwargs)
        # 保存原始 __init__，以便在子类中注入锁初始化
        original_init = cls.__init__

        def wrapped_init(self, *args, **kwargs):
            # 确保 _domain_lock 存在（子类可能已初始化，此处作为兜底）
            if not hasattr(self, '_domain_lock'):
                self._domain_lock = threading.Lock()
            original_init(self, *args, **kwargs)

        cls.__init__ = wrapped_init

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
                        logger.info("[+] %s 从缓存加载 %s 个域名:", self.source_name.upper(), len(unique_domains))
                        for d in unique_domains:
                            logger.info("    - %s", d)
                        return True
        except Exception as e:
            logger.warning("[!] %s 读取域名缓存失败: %s", self.source_name.upper(), e)
        return False

    def _save_domains_to_cache(self):
        """将当前域名列表保存到本地缓存"""
        cache_path = self._get_domain_cache_path()
        try:
            with self._domain_lock:
                to_save = list(dict.fromkeys(self.domains))  # 去重保序
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=2)
            logger.info("[+] %s 域名已缓存至: %s", self.source_name.upper(), cache_path)
        except Exception as e:
            logger.warning("[!] %s 保存域名缓存失败: %s", self.source_name.upper(), e)

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
                    logger.warning("[!] %s 域名切换至: %s", self.source_name.upper(), self.base_domain)
                    return
            
            # 所有域名都在冷却中
            if len(self.domains) > 1:
                min_wait = max(0, min(
                    self._cooldown_seconds - (now - self._domain_cooldown.get(d, 0))
                    for d in self.domains
                ))
                wait_time = max(min_wait, 10) + random.uniform(2, 5)
                logger.warning("[!] 所有域名均在冷却中，等待 %.1f 秒...", wait_time)
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
            logger.warning("[!] 冷却结束，%s 域名切换至: %s", self.source_name.upper(), self.base_domain)

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
            logger.warning("[!] 检测到跳转页面但未能提取到域名")
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
            logger.info("[+] %s 检测到域名变更！提取到 %s 个最新域名:", self.source_name.upper(), len(unique_domains))
            for i, d in enumerate(unique_domains, 1):
                tag = " [新]" if d in added else ""
                logger.info("    域名%s: %s%s", i, d, tag)
            if removed:
                logger.info("    已失效: %s", ', '.join(removed))
        
        # 自动持久化到本地缓存
        self._save_domains_to_cache()
        
        return True

    def _fetch_domains_from_main_station(self):
        """从主站域名动态获取最新可用镜像域名列表"""
        if not getattr(self, "main_domain", None):
            return False
        
        logger.info("[*] %s 开始从主站 %s 动态获取最新域名列表...", self.source_name.upper(), self.main_domain)
        headers = self._build_headers(referer=self.main_domain)
        
        # 1. 优先获取代理
        from config import get_effective_proxy
        proxies = get_effective_proxy()
            
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
                logger.error("[-] curl_cffi 请求主站 %s 失败: %s", self.main_domain, e)
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
            logger.info("[*] Playwright 并发访问主站: %s", self.main_domain)
            page = None
            try:
                _, _, context = self._get_thread_resources()
                page = context.new_page()
                page.goto(self.main_domain, timeout=30000, wait_until="domcontentloaded")
                import time
                time.sleep(3.0)
                return page.content()
            except Exception as e:
                logger.error("[-] Playwright 访问主站 %s 异常: %s", self.main_domain, e)
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
            logger.warning("[!] 无法从主站 %s 获取到页面内容", self.main_domain)
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
            logger.warning("[!] 未能在主站内容中匹配提取到镜像域名 (正则: %s)", pattern)
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
            logger.warning("[!] 解析出的镜像域名列表为空")
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
            
        logger.info("[+] 成功从主站拉取并更新了 %s 个最新域名:", len(unique_domains))
        for i, d in enumerate(unique_domains, 1):
            logger.info("    域名%s: %s", i, d)
            
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
            logger.error("[-] HTML 解密失败: %s", e)
            return None

    def decrypt_title(self, encrypted_title_b64):
        """解密详情页或列表页的加密标题"""
        import base64
        try:
            return base64.b64decode(encrypted_title_b64).decode('utf-8')
        except Exception as e:
            logger.error("[-] 标题解密失败: %s", e)
            return ""


class DecryptSiteBaseCrawler(PlaywrightBaseCrawler, DomainRotationMixin, DecryptMixin):
    """
    支持域名轮换、HTML 解密的站点爬虫抽象基类

    提取 DatangCrawler / MadouCrawler 的公共逻辑：
    - 多分类板块遍历 (guochan/wuma/oumei)
    - 域名轮换 fetch_list_page
    - 域名轮换 process_sub_page_if_needed
    - 断点续爬
    - PDF 重试
    """
    CATEGORIES = []  # 子类定义分类列表，如 ["guochan", "wuma", "oumei"]

    def __init__(self, db_manager, source_name, config: Optional[CrawlConfig] = None):
        super().__init__(db_manager, source_name)
        self.current_domain_idx = 0
        self.current_class = ""
        self._domain_cooldown = {}
        self._cooldown_seconds = 60
        self._domain_lock = threading.Lock()

        if config is not None:
            # 从 CrawlConfig 统一初始化
            self.check_resource_link = config.check_resource_link
            self.max_consecutive_existing = config.max_consecutive_existing
            self.domain_pattern = config.domain_pattern
            self.main_domain = config.main_domain
            self.domains = list(config.domains)
            self.base_domain = config.base_domain
            self.base_list_url = config.base_list_url
            if config.categories:
                self.CATEGORIES = config.categories
        else:
            # 向后兼容：子类手动设置各属性
            self.check_resource_link = True
            self.domains = []
            self.base_domain = ""
            self.base_list_url = ""
            self.max_consecutive_existing = 15

    def _get_full_list_url(self, page_num):
        """子类可覆盖，默认格式 base_list_url.format(class, page)"""
        return self.base_list_url.format(self.current_class, page_num)

    def fetch_list_page(self, page_num, retry_with_main=True):
        """请求列表页并解密 HTML，支持域名轮换重试和自动域名发现"""
        if not self.domains:
            if not getattr(self, '_fetch_domains_from_main_station', None) or not self._fetch_domains_from_main_station():
                logger.warning("[!] 域名列表为空且从主站获取失败，无法继续抓取")
                return None

        for _ in range(len(self.domains)):
            url = self._get_full_list_url(page_num)
            headers = self._build_headers()
            redirect_content = None

            for attempt in range(3):
                proxies = None
                if attempt < 2:
                    from config import get_effective_proxy
                    proxies = get_effective_proxy()

                try:
                    response = requests.get(url, headers=headers, timeout=15, proxies=proxies, impersonate="chrome120")
                    if response.status_code == 200:
                        decrypted = self.decrypt_html(response.text)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            return decrypted
                        if decrypted and "正在检测最新可用线路" in decrypted:
                            redirect_content = decrypted
                        elif "正在检测最新可用线路" in response.text:
                            redirect_content = response.text
                    elif response.status_code == 403:
                        logger.warning("[!] 列表页返回 403，疑似触发反爬: %s", url)
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

            # Playwright 兜底
            logger.info("[*] 使用 Playwright 兜底访问列表页: %s", url)
            try:
                _, _, context = self._get_thread_resources()
                page = context.new_page()
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                time.sleep(random.uniform(2.0, 4.0))
                html = page.content()
                page.close()
                if self._is_valid_list_page(html):
                    return html
                decrypted = self.decrypt_html(html)
                if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                    return decrypted
                if decrypted and "正在检测最新可用线路" in decrypted:
                    redirect_content = decrypted
                elif "正在检测最新可用线路" in html:
                    redirect_content = html
            except Exception as e:
                logger.error("[-] Playwright 兜底抓取列表页异常: %s", e)
                if is_proxy_manager_enabled():
                    from utils.proxy_manager import get_proxy_manager
                    manager = get_proxy_manager()
                    if manager:
                        proxy_url = manager._thread_proxy_map.get(threading.get_ident())
                        if proxy_url:
                            manager.report_failure(proxy_url)
                self._destroy_thread_resources()

            if redirect_content and self._update_domains_from_redirect(redirect_content):
                logger.info("[+] 域名列表已更新，使用新域名重试...")
                continue

            logger.warning("[!] 当前域名疑似被封，冷却等待后切换...")
            time.sleep(random.uniform(8.0, 15.0))
            self._rotate_domain()

        if getattr(self, 'main_domain', None) and retry_with_main:
            logger.warning("[!] %s 所有现有镜像域名均尝试失败，尝试从主站更新域名列表...", self.source_name.upper())
            if self._fetch_domains_from_main_station():
                logger.info("[+] 成功从主站拉取到新域名，开始重新尝试请求列表页...")
                return self.fetch_list_page(page_num, retry_with_main=False)
        return None

    def _is_valid_list_page(self, html):
        """子类覆盖：判断 Playwright 兜底时页面是否有效"""
        return bool(html)

    def run(self, is_test=False, start_page=1, end_page=1, max_workers=None, **kwargs):
        """带多分类板块遍历的 run() 方法"""
        self.is_test = is_test
        self.quiet = kwargs.get('quiet', False)
        resume = kwargs.get('resume', False)
        self.resume = resume
        self.no_pdf = kwargs.get('no_pdf', False)

        classes = self.CATEGORIES
        if not classes:
            classes = ["guochan"]

        logger.info("[*] 启动 %s 爬虫流程...", self.source_name)
        self.on_start()

        no_early_stop = kwargs.get('no_early_stop', False)
        if no_early_stop:
            logger.info("[*] 禁用早停机制，将强制爬取指定范围内所有页面。")
            self.max_consecutive_existing = None
            self.max_consecutive_duplicate_pages = None
        elif not resume:
            self.max_consecutive_existing = None
            self.max_consecutive_duplicate_pages = None

        if max_workers is None:
            if getattr(self, 'no_pdf', False):
                max_workers = 30
            else:
                max_workers = 10

        resume_class = None
        resume_page = start_page

        if resume:
            all_states = self.db_manager.load_crawl_state(self.source_name)
            if all_states:
                if "__all__" in all_states and all_states["__all__"].get("completed", False):
                    logger.info("[*] 检测到 %s 已完成全部爬取，跳过所有板块", self.source_name)
                    self.db_manager.clear_crawl_state(self.source_name)
                    logger.info("[+] 已清除完成标记，下次运行将重新爬取")
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
                            logger.info("[*] 检测到板块 %s 爬取断点，从第 %s 页继续", cls, resume_page)
                            break
                        logger.info("[断点续爬] 板块 %s 已完成，跳过", cls)
                    else:
                        resume_class = cls
                        resume_page = start_page
                        logger.info("[*] 板块 %s 无历史记录，从头开始爬取", cls)
                        break
                else:
                    logger.info("[*] 所有板块已完成，无需爬取")
                    try:
                        if is_test:
                            self._run_test_mode(start_page)
                        return
                    finally:
                        self.on_finish()
                    return
            else:
                logger.info("[*] 未检测到历史断点，从头开始爬取")

        try:
            if is_test:
                self._run_test_mode(start_page)
                return

            for cls in classes:
                if resume and resume_class is not None:
                    cls_index = classes.index(cls)
                    resume_index = classes.index(resume_class)
                    if cls_index < resume_index:
                        logger.info("\n[断点续爬] 板块 %s 已完成，跳过", cls)
                        continue
                    actual_start = resume_page if cls == resume_class else start_page
                else:
                    actual_start = start_page

                self.current_class = cls
                logger.info("\n[*] ================= 开始爬取 %s 板块: %s (起始页码: %s) =================", self.source_name, cls, actual_start)
                self._crawl_pages(actual_start, end_page, max_workers, class_name=cls)

                if resume and cls == resume_class:
                    resume_class = None

            if not is_test and resume:
                self.db_manager.mark_source_completed(self.source_name)
                logger.info("[+] %s 所有板块爬取完成，已标记完成状态", self.source_name)

        except KeyboardInterrupt:
            logger.warning("\n[中断] 检测到用户手动停止运行 (Ctrl+C)")
        except Exception as e:
            logger.error("\n[致命错误] 运行中发生未捕获的异常: %s", e)
        finally:
            self.on_finish()

    # ------------------------------------------------------------------ #
    #  以下为子类可覆盖的钩子方法，用于 process_sub_page_if_needed() 中的差异化逻辑
    # ------------------------------------------------------------------ #

    def _is_valid_detail_page(self, html):
        """子类覆盖：判断 Playwright 兜底时详情页是否有效"""
        return True

    def _should_rewrite_url(self, netloc):
        """子类覆盖：判断是否应使用当前域名重写 URL（用于域名轮换）"""
        return any(d in netloc for d in self.domains)

    def _get_category_map(self):
        """子类覆盖：返回分类名称映射字典 e.g. {'guochan': '国产', ...}"""
        return {}

    def _extract_detail_metadata(self, detail_html, raw_item):
        """从详情页提取发布时间、大小、格式

        子类可覆盖以支持自定义提取逻辑（如不同的 HTML 结构）。
        默认尝试匹配通用格式 【发布时间】/【影片大小】/【影片格式】

        Returns:
            (date_str, size_val, res_format) 三元组
        """
        date_str = raw_item['date_str']
        size_val = ""
        res_format = ""

        date_match = re.search(r"【发布时间】：\s*(\d{4}-\d{2}-\d{2})", detail_html)
        if date_match:
            date_str = date_match.group(1)
        size_match = re.search(r"【影片大小】：\s*([^<]+)", detail_html)
        if size_match:
            size_val = size_match.group(1).strip()
        format_match = re.search(r"【影片格式】：\s*([^<]+)", detail_html)
        if format_match:
            res_format = format_match.group(1).strip()

        return date_str, size_val, res_format

    # ------------------------------------------------------------------ #
    #  统一的详情页处理流程（替代子类中的重复实现）
    # ------------------------------------------------------------------ #

    def process_sub_page_if_needed(self, raw_item, idx):
        """请求详情页，解析资源元数据并生成 PDF，支持域名轮换重试

        通过调用 _is_valid_detail_page / _should_rewrite_url / _get_category_map
        / _extract_detail_metadata 等钩子方法处理站点差异。
        """
        original_url = raw_item['url']
        is_existing = False

        # 每个详情页请求前随机延迟，模拟人类浏览行为
        if getattr(self, 'no_pdf', False):
            time.sleep(random.uniform(0.3, 0.8))
        else:
            time.sleep(random.uniform(2.0, 5.0))

        detail_html = None
        url = original_url

        # 最多尝试轮换所有域名的次数
        for _ in range(len(self.domains)):
            from urllib.parse import urlparse, urlunparse
            parsed_url = urlparse(original_url)
            with self._domain_lock:
                current_base = self.base_domain
            parsed_base = urlparse(current_base)
            if self._should_rewrite_url(parsed_url.netloc):
                parsed_url = parsed_url._replace(netloc=parsed_base.netloc, scheme=parsed_base.scheme)
                url = urlunparse(parsed_url)
            else:
                url = original_url

            list_url = self.base_list_url.format(self.current_class, 1)
            headers = self._build_headers(referer=list_url)
            redirect_content = None

            # 1. 优先使用 requests
            for attempt in range(3):
                proxies = None
                if attempt < 2:
                    from config import get_effective_proxy
                    proxies = get_effective_proxy()

                try:
                    response = requests.get(url, headers=headers, timeout=15, proxies=proxies, impersonate="chrome120")
                    if response.status_code == 200:
                        decrypted = self.decrypt_html(response.text)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            detail_html = decrypted
                            break
                        if decrypted and "正在检测最新可用线路" in decrypted:
                            redirect_content = decrypted
                        elif "正在检测最新可用线路" in response.text:
                            redirect_content = response.text
                    elif response.status_code == 403:
                        logger.warning("[!] 详情页返回 403，疑似触发反爬: %s", url)
                        break
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
                    if self._is_valid_detail_page(html):
                        detail_html = html
                        break
                    else:
                        decrypted = self.decrypt_html(html)
                        if decrypted and "正在检测最新可用线路" not in decrypted and "403 Forbidden" not in decrypted:
                            detail_html = decrypted
                            break
                        if decrypted and "正在检测最新可用线路" in decrypted:
                            redirect_content = decrypted
                        elif "正在检测最新可用线路" in html:
                            redirect_content = html
                except Exception as e:
                    logger.error("[-] Playwright 兜底抓取详情页异常 (%s): %s", url, e)

            if detail_html:
                break

            # 尝试从跳转页面提取最新域名
            if redirect_content and self._update_domains_from_redirect(redirect_content):
                continue

            # 当前域名请求失败，冷却等待后轮换域名重试
            time.sleep(random.uniform(5.0, 10.0))
            self._rotate_domain()

        if not detail_html:
            logger.error("[-] 详情页 %s 抓取失败（最终尝试 URL: %s）", original_url, url)
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
            logger.error("[-] 在详情页中未找到磁力链接: %s", original_url)
            return False, None

        date_str, size_val, res_format = self._extract_detail_metadata(detail_html, raw_item)

        category_map = self._get_category_map()
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

        # === 提前去重：在 PDF 生成前检查磁力链接是否已存在 ===
        if self.check_resource_link and magnet_link:
            existing_links = self.db_manager.filter_existing_resource_links([magnet_link])
            if magnet_link in existing_links:
                logger.info("[%s] 磁力链接已存在，跳过 PDF 生成: %s...", idx, magnet_link[:60])
                data['source'] = self.source_name
                return True, data

        # 处理 PDF 文件生成
        if self.is_test:
            logger.info("-> 测试模式下跳过保存 PDF 以节省时间")
        else:
            data['pdf_path'] = self.retry_generate_pdf(
                url, date_str, raw_item['title'],
                max_retries=4, no_proxy_last=True
            )

        return is_existing, data