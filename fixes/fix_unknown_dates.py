import os
import sys
import time
import sqlite3
import random
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_db_path
from utils import setup_console_utf8
from utils.date_parser import parse_date


def _create_browser_context(p, user_agent=None, viewport=None):
    """创建 Playwright 浏览器上下文（内联版，替代已废弃的 create_browser_context）"""
    from config import USER_AGENTS, get_crawler_proxy, is_proxy_manager_enabled
    from utils.stealth import get_browser_launch_args, apply_stealth
    launch_args = get_browser_launch_args(browser_type='chromium', headless=True)
    playwright_proxy = None
    crawler_proxy = get_crawler_proxy()
    if crawler_proxy:
        playwright_proxy = {"server": crawler_proxy}
    elif is_proxy_manager_enabled():
        try:
            from utils.proxy_manager import get_proxy_string
            proxy_url = get_proxy_string()
            if proxy_url:
                playwright_proxy = {"server": proxy_url}
        except Exception as ex:
            print(f"[!] 获取自动代理失败: {ex}")
    if playwright_proxy:
        print(f"[*] Playwright 启动代理: {playwright_proxy['server']}")
    else:
        print("[*] Playwright 未启用代理")
    browser = p.chromium.launch(headless=True, args=launch_args, proxy=playwright_proxy)
    ctx_args = {"locale": "zh-CN", "user_agent": user_agent or random.choice(USER_AGENTS)}
    ctx_args["viewport"] = viewport or {"width": 1920, "height": 1080}
    context = browser.new_context(**ctx_args)
    
    # 使用统一 stealth 模块注入伪装脚本
    apply_stealth(context)
    
    return browser, context

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
            browser, context = _create_browser_context(p)
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
    setup_console_utf8()
    fix_dates()
