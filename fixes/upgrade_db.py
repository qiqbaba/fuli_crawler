import os
import sys
import sqlite3

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import setup_console_utf8
from utils.metadata_parser import parse_title, parse_pikpak_link
from config import DB_PATHS


def upgrade_database(db_path):
    print(f"\n================ 开始处理数据库: {db_path} ================")
    if not os.path.exists(db_path):
        print(f"数据库文件不存在，跳过: {db_path}")
        return False
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 1. 检测表结构并升级
    try:
        cursor.execute("PRAGMA table_info(resources)")
        columns_info = cursor.fetchall()
        if not columns_info:
            print("  -> 未找到 resources 表，可能尚未初始化。")
            conn.close()
            return False
            
        current_columns = [col[1] for col in columns_info]
        target_columns = ['id', 'title', 'publish_time', 'category', 'resource_link', 'pikpak_link', 'size', 'resource_format', 'link_type', 'url', 'pdf_path', 'source']
        
        need_migration = (current_columns != target_columns)
        
        if need_migration:
            print("  -> 检测到表结构需要升级重构，正在进行数据迁移...")
            # 开启事务
            cursor.execute("BEGIN TRANSACTION")
            
            # 创建临时表
            cursor.execute('''
                CREATE TABLE resources_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    publish_time TEXT,
                    category TEXT,
                    resource_link TEXT NOT NULL,
                    pikpak_link TEXT,
                    size TEXT,
                    resource_format TEXT,
                    link_type TEXT,
                    url TEXT,
                    pdf_path TEXT,
                    source TEXT
                )
            ''')
            
            # 动态映射拷贝旧表数据
            select_parts = []
            for col in target_columns:
                if col in current_columns:
                    select_parts.append(col)
                else:
                    select_parts.append("NULL")
            
            copy_sql = f"INSERT INTO resources_new ({', '.join(target_columns)}) SELECT {', '.join(select_parts)} FROM resources"
            cursor.execute(copy_sql)
            
            # 删除旧表并重命名新表
            cursor.execute("DROP TABLE resources")
            cursor.execute("ALTER TABLE resources_new RENAME TO resources")
            
            # 重新创建索引
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_url ON resources(url)")
            
            conn.commit()
            print("  -> 成功迁移表结构并删除了旧字段")
        else:
            print("  -> 数据库表结构已是最新，无需迁移")
            
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"  -> 数据库迁移失败: {e}")
        conn.close()
        return False
        
    # 2. 提取并更新历史数据
    print("正在加载历史数据进行解析与修补...")
    cursor.execute("SELECT id, title, resource_link FROM resources")
    rows = cursor.fetchall()
    total_records = len(rows)
    print(f"共找到 {total_records} 条记录。")
    
    update_data = []
    matched_count = 0
    pikpak_count = 0
    
    for row_id, title, res_link in rows:
        size_val, res_format = parse_title(title)
        pikpak_link = parse_pikpak_link(res_link)
        
        if size_val or res_format:
            matched_count += 1
        if pikpak_link:
            pikpak_count += 1
            
        update_data.append((size_val, res_format, pikpak_link, row_id))
        
    if update_data:
        print("开始批量写入数据库中...")
        cursor.executemany(
            "UPDATE resources SET size = ?, resource_format = ?, pikpak_link = ? WHERE id = ?",
            update_data
        )
        conn.commit()
        if total_records > 0:
            print(f"  -> 批量更新完成！成功解析并填入有效元数据共 {matched_count}/{total_records} 条记录 ({matched_count/total_records:.2%})。")
            print(f"  -> 成功提取并填入 PikPak 链接共 {pikpak_count}/{total_records} 条记录。")
        else:
            print("  -> 数据库中没有记录。")
    else:
        print("无记录需要更新。")
        
    conn.close()
    return True

def main():
    setup_console_utf8()
    updated_any = False
    for path in DB_PATHS:
        if upgrade_database(path):
            updated_any = True
            
    if not updated_any:
        print("\n[警告] 未在指定路径找到任何数据库文件，请检查数据库路径配置。")

if __name__ == '__main__':
    main()
