"""
查找数据库中所有网址包含 tcm.589656.xyz 的记录，将域名替换为 jnx.322265.xyz
"""
import sqlite3
import re
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_db_path

DB_PATH = get_db_path()

def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. 查找所有匹配的记录
    cursor.execute("SELECT id, url FROM resources WHERE url LIKE '%tcm.589656.xyz%'")
    rows = cursor.fetchall()
    print(f"找到 {len(rows)} 条包含 tcm.589656.xyz 的记录:")
    for row in rows:
        print(f"  id={row[0]}, url={row[1]}")
    
    if not rows:
        print("没有需要替换的记录。")
        conn.close()
        return
    
    # 2. 执行替换
    updated_count = 0
    for row_id, old_url in rows:
        new_url = old_url.replace("tcm.589656.xyz", "jnx.322265.xyz")
        cursor.execute("UPDATE resources SET url = ? WHERE id = ?", (new_url, row_id))
        updated_count += 1
        print(f"  [更新] id={row_id}: {old_url} -> {new_url}")
    
    conn.commit()
    print(f"\n共更新 {updated_count} 条记录。")
    conn.close()

if __name__ == "__main__":
    main()