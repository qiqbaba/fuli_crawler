import os
import re
import time
import random
from urllib.parse import urlparse, urljoin
from playwright.sync_api import sync_playwright
from config import USER_AGENTS, PDF_BASE_DIR
from crawlers.base_crawler import BaseCrawler

def sanitize_filename(filename):
    """清理文件名中的非法字符"""
    return re.sub(r'[\\/:*?"<>|]', '_', filename).strip()

class SejuCrawler(BaseCrawler):
    def __init__(self, db_manager):
        super().__init__(db_manager, "seju")
        self.base_url = "https://seju.life/page/{}/"
        self.target_domain = "seju.life"
        self.playwright = None
        self.browser = None
        self.context = None
        self.list_page = None

    def on_start(self):
        """初始化 Playwright 环境"""
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.context = self.browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={'width': 1920, 'height': 1080}
        )
        self.context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.list_page = self.context.new_page()

    def on_finish(self):
        """释放 Playwright 资源"""
        if self.browser:
            try:
                self.browser.close()
            except Exception as e:
                print(f"[-] 关闭浏览器失败: {e}")
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception as e:
                print(f"[-] 停止 Playwright 失败: {e}")
        # 确保数据库进行收尾提交
        self.db_manager.commit()

    def fetch_list_page(self, page_num):
        """加载列表页并返回当前 page 对象"""
        list_url = self.base_url.format(page_num)
        print(f"[*] 正在访问列表页: {list_url}")
        try:
            self.list_page.goto(list_url, timeout=60000, wait_until="domcontentloaded")
            time.sleep(random.uniform(2, 4))
            return self.list_page
        except Exception as e:
            print(f"[-] 列表页 {list_url} 加载失败: {e}")
            return None

    def parse_list_page(self, list_page, page_num):
        """解析列表页卡片，提取出所有子页面的完整 URL 列表"""
        articles_locator = list_page.locator('//div[@class="content"]/article')
        card_count = articles_locator.count()
        
        sub_urls = []
        list_url = self.base_url.format(page_num)
        for i in range(card_count):
            try:
                card = articles_locator.nth(i)
                card_title_node = card.locator('xpath=./header/h2/a') 
                sub_url_path = card_title_node.get_attribute('href')
                if not sub_url_path:
                    continue
                sub_urls.append(urljoin(list_url, sub_url_path))
            except Exception as e:
                print(f"[-] 解析第 {i+1} 个卡片链接时出错: {e}")
        return sub_urls

    def process_sub_page_if_needed(self, sub_url, idx):
        """
        处理单个子页面的抓取、信息提取、PDF 保存。
        raw_item 在这里即为 sub_url
        """
        # 1. 检查子页面链接是否已存在
        is_existing = self.db_manager.check_url_exists(sub_url)
        if is_existing and not self.is_test:
            print(f"[{idx}] 网址已存在数据库中，跳过抓取: {sub_url}")
            return True, None  # (is_existing, data)

        sub_page = None  
        try:
            sub_page = self.context.new_page()
            sub_page.goto(sub_url, timeout=60000, wait_until="domcontentloaded")
            sub_page.wait_for_load_state("load", timeout=10000)
            time.sleep(random.uniform(1.5, 3.5))
            
            current_url = sub_page.url
            parsed_url = urlparse(current_url)
            is_external = self.target_domain not in parsed_url.netloc
            
            # 2. 检查重定向后的真实链接是否已存在
            if current_url != sub_url:
                if self.db_manager.check_url_exists(current_url) and not self.is_test:
                    print(f"[{idx}] 重定向后的真实网址已存在，跳过抓取: {current_url}")
                    return True, None

            if is_external:
                print(f"检测到跳转至外部网站: {current_url}")
                title = sub_page.title()
                pub_time = ""
                category = "外部跳转"
                res_link = current_url
                link_type = "外链"
                pdf_path = ""
            else:
                title_loc = sub_page.locator('//h1[@class="article-title"]/a')
                title = title_loc.text_content().strip() if title_loc.count() > 0 else "无标题"
                
                time_loc = sub_page.locator('//header[@class="article-header"]/div[@class="meta"]/time')
                pub_time = time_loc.text_content().strip() if time_loc.count() > 0 else ""
                
                cat_loc = sub_page.locator('//header[@class="article-header"]/div[@class="meta"]/span[1]')
                category = cat_loc.text_content().strip() if cat_loc.count() > 0 else ""
                
                p_texts = sub_page.locator('//article[@class="article-content"]//p').all_text_contents()
                cleaned_p_texts = [t.strip() for t in p_texts if t.strip()]
                
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
            
            # 针对内部网页，需要为其生成并保存 PDF 文件
            if not is_external:
                if self.is_test:
                    print("-> 测试模式下跳过保存 PDF 以节省时间")
                else:
                    # 获取清洗后推导出来的年份与格式化日期
                    publish_date = data['publish_time']  # YYYY-MM-DD
                    year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
                    
                    save_dir = os.path.join(PDF_BASE_DIR, year)
                    os.makedirs(save_dir, exist_ok=True)
                    
                    safe_title = sanitize_filename(title)
                    base_filename = f"{publish_date}_{safe_title}"
                    pdf_path = os.path.join(save_dir, f"{base_filename}.pdf")
                    
                    counter = 1
                    while os.path.exists(pdf_path):
                        pdf_path = os.path.join(save_dir, f"{base_filename}_{counter}.pdf")
                        counter += 1
                    
                    data['pdf_path'] = pdf_path
                    
                    try:
                        sub_page.pdf(path=pdf_path, format="A4", print_background=True)
                        print(f"已保存 PDF: {pdf_path}")
                    except Exception as pdf_e:
                        print(f"-> 警告：数据提取成功，但 PDF 保存失败: {pdf_e}")
            else:
                print("-> 外部网站，已跳过 PDF 保存")
                
            return is_existing, data  # (is_existing, data)
                
        except Exception as e:
            print(f"[-] 抓取子页面 {sub_url} 时发生错误: {e}")
            return False, None
        finally:
            if sub_page:
                try:
                    sub_page.close()
                except Exception as e:
                    print(f"[-] 关闭子页面失败: {e}")
            time.sleep(random.uniform(1, 2))
