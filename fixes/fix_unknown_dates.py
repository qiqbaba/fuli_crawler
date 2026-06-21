import os
import sys
import time
import sqlite3
import random
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_db_path, USER_AGENTS
from utils.date_parser import parse_date

TARGET_DOMAIN = "seju.life"

def fix_dates():
    db_path = get_db_path()
    print(f"正在读取数据库: {db_path}")
    if not os.path.exists(db_path):
        print("数据库文件不存在。")
        return
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 查找所有 publish_time 为 Unknown_Date 的记录
    cursor.execute("SELECT id, url, title FROM resources WHERE publish_time = 'Unknown_Date'")
    rows = cursor.fetchall()
    
    if not rows:
        print("未发现 publish_time 为 'Unknown_Date' 的记录。")
        conn.close()
        return

    print(f"共发现 {len(rows)} 条 'Unknown_Date' 记录，准备重新解析...")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={'width': 1920, 'height': 1080}
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            page = context.new_page()
            
            updated_count = 0
            for record_id, url, title in rows:
                print(f"正在处理 [{record_id}] {title} ...")
                
                # 如果是外部链接，通常没有日期解析，跳过或特殊处理
                parsed_url = urlparse(url)
                if TARGET_DOMAIN not in parsed_url.netloc:
                    print(f"  -> 跳过外部链接: {url}")
                    continue

                try:
                    page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    page.wait_for_load_state("load", timeout=10000)
                    time.sleep(random.uniform(1.0, 2.0))
                    
                    # 重新定位日期元素
                    time_loc = page.locator('//header[@class="article-header"]/div[@class="meta"]/time')
                    if time_loc.count() > 0:
                        pub_time_raw = time_loc.text_content().strip()
                        _, formatted_date = parse_date(pub_time_raw)
                        
                        if formatted_date != "Unknown_Date":
                            cursor.execute("UPDATE resources SET publish_time = ? WHERE id = ?", (formatted_date, record_id))
                            conn.commit()
                            updated_count += 1
                            print(f"  -> 成功更新日期: {formatted_date} (原: {pub_time_raw})")
                        else:
                            print(f"  -> 依然无法解析日期: {pub_time_raw}")
                    else:
                        print("  -> 页面上未找到日期元素")
                        
                except Exception as e:
                    print(f"  -> 处理出错: {e}")
                
                time.sleep(random.uniform(0.5, 1.5))
            
            print(f"\n修复任务完成！共更新 {updated_count} 条记录。")
            browser.close()
            
    except Exception as fatal_e:
        print(f"程序运行中发生致命错误: {fatal_e}")
    finally:
        conn.close()

if __name__ == "__main__":
    if sys.platform.startswith('win'):
        if sys.stdout.encoding != 'utf-8':
            try:
                sys.stdout.reconfigure(encoding='utf-8')
                sys.stderr.reconfigure(encoding='utf-8')
            except AttributeError:
                pass
    fix_dates()
