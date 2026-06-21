import os
import re
import sys
import time
import random
import sqlite3
from playwright.sync_api import sync_playwright

from config import get_db_path, USER_AGENTS

resource_patterns = [
    r'^magnet:\?',
    r'^ed2k://',
    r'^thunder://',
    r'^https?://',
    r'提取码',
    r'解压密码',
    r'天翼'
]

def is_resource_line(text):
    text_lower = text.lower()
    for pattern in resource_patterns:
        if re.search(pattern, text_lower):
            return True
    return False

def parse_res_link(p_texts):
    cleaned_p_texts = [t.strip() for t in p_texts if t.strip()]
    if not cleaned_p_texts:
        return ""
    
    if len(cleaned_p_texts) > 1:
        last_line = cleaned_p_texts[-1].lower()
        is_res = any(re.search(pat, last_line) for pat in resource_patterns)
        if not is_res:
            cleaned_p_texts = cleaned_p_texts[:-1]
            
    return "\n".join(cleaned_p_texts)

def main():
    db_path = get_db_path()
    print(f"正在读取数据库: {db_path}")
    if not os.path.exists(db_path):
        print("数据库文件不存在。")
        return
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 查找 resource_link 为空的记录
    cursor.execute("SELECT id, url, title FROM resources WHERE resource_link = ''")
    records = cursor.fetchall()
    print(f"找到数据库中共有 {len(records)} 条 resource_link 为空的记录。")
    
    if not records:
        print("没有需要修复的记录。")
        conn.close()
        return

    success_count = 0
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={'width': 1920, 'height': 1080}
            )
            page = context.new_page()
            
            for idx, (db_id, url, title) in enumerate(records, 1):
                if "seju.life" not in url:
                    continue
                    
                print(f"[{idx}/{len(records)}] 正在处理: {title[:20]}... ID: {db_id}")
                
                try:
                    page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    page.wait_for_load_state("load", timeout=15000)
                    
                    p_loc = page.locator('//article[@class="article-content"]//p')
                    p_count = p_loc.count()
                    p_texts = [p_loc.nth(i).text_content() for i in range(p_count)]
                    
                    res_link = parse_res_link(p_texts)
                    
                    if res_link:
                        cursor.execute("UPDATE resources SET resource_link = ? WHERE id = ?", (res_link, db_id))
                        conn.commit()
                        success_count += 1
                        print(f"  -> 成功修复链接: {res_link[:50]}...")
                    else:
                        print("  -> 提取结果仍为空（可能是纯文字教学页面或无资源）")
                        
                except Exception as page_e:
                    print(f"  -> 访问页面出错: {page_e}")
                
                time.sleep(random.uniform(1.5, 3.0))
                
            browser.close()
            
    except Exception as e:
        print(f"运行过程中出错: {e}")
    finally:
        conn.close()
        print(f"修复完成！共成功回填了 {success_count} 条记录。")

if __name__ == "__main__":
    if sys.platform.startswith('win'):
        if sys.stdout.encoding != 'utf-8':
            try:
                sys.stdout.reconfigure(encoding='utf-8')
                sys.stderr.reconfigure(encoding='utf-8')
            except AttributeError:
                pass
    main()
