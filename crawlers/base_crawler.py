import time
import random
from utils.date_parser import parse_date
from utils.metadata_parser import parse_title, parse_link_metadata, parse_pikpak_link

class BaseCrawler:
    def __init__(self, db_manager, source_name):
        self.db_manager = db_manager
        self.source_name = source_name
        self.max_consecutive_existing = 20

    def on_start(self):
        """生命周期钩子：爬网开始前（子类可选覆盖）"""
        pass

    def on_finish(self):
        """生命周期钩子：爬网结束后（子类可选覆盖）"""
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

    def run(self, is_test=False, start_page=1, end_page=1, **kwargs):
        """
        统一的爬虫执行骨架
        """
        self.is_test = is_test
        print(f"[*] 启动 {self.source_name} 爬虫流程...")
        self.on_start()
        
        consecutive_count = 0
        
        try:
            if is_test:
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
                    # 获取详细数据
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
                return

            # 正式爬取模式
            for page_num in range(start_page, end_page + 1):
                print(f"\n================ 正在抓取第 {page_num}/{end_page} 页 ================")
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
                
                inserted_count = 0
                skipped_count = 0
                
                for idx, raw_item in enumerate(raw_items, 1):
                    res = self.process_sub_page_if_needed(raw_item, idx)
                    if not res:
                        continue
                    is_existing, data = res
                    if not data:
                        continue
                    
                    if is_existing:
                        skipped_count += 1
                        consecutive_count += 1
                        print(f"[*] 连续发现已存在数据: {consecutive_count}/{self.max_consecutive_existing}")
                        if consecutive_count >= self.max_consecutive_existing:
                            print(f"\n[触发停止条件] 连续 {self.max_consecutive_existing} 条数据已存在，停止处理当前页！")
                            break
                    else:
                        # 尝试写入数据库
                        success = self.db_manager.insert_resource(data)
                        if success:
                            inserted_count += 1
                            consecutive_count = 0  # 写入新数据，计数清零
                        else:
                            skipped_count += 1
                            consecutive_count += 1
                            print(f"[*] 连续发现已存在数据 (DB IGNORE): {consecutive_count}/{self.max_consecutive_existing}")
                            if consecutive_count >= self.max_consecutive_existing:
                                print(f"\n[触发停止条件] 连续 {self.max_consecutive_existing} 条数据已存在，停止当前页处理！")
                                break
                                
                print(f"[+] 页面 {page_num} 处理完成：写入 {inserted_count} 条，跳过 {skipped_count} 条。")
                
                if consecutive_count >= self.max_consecutive_existing:
                    print(f"\n[任务结束] 爬虫已追溯到历史抓取位置，安全退出翻页循环。")
                    break
                    
                time.sleep(random.uniform(1.5, 3.0))

        except KeyboardInterrupt:
            print("\n[中断] 检测到用户手动停止运行 (Ctrl+C)")
        except Exception as e:
            print(f"\n[致命错误] 运行中发生未捕获的异常: {e}")
        finally:
            self.on_finish()
