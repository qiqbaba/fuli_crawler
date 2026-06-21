import os
import sqlite3
import sys

# 导入公共配置和公共元数据解析函数
from config import DB_PATHS
from utils.metadata_parser import parse_link_metadata

def fix_database(db_path):
    print(f"\n================ 开始修复数据库: {db_path} ================")
    if not os.path.exists(db_path):
        print(f"数据库文件不存在，跳过: {db_path}")
        return False
        
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 查找 size 或 resource_format 为空/NULL 的记录，且 resource_link 不为空
        cursor.execute("""
            SELECT id, title, resource_link, size, resource_format 
            FROM resources 
            WHERE (size IS NULL OR size = '' OR resource_format IS NULL OR resource_format = '')
              AND resource_link IS NOT NULL 
              AND resource_link != ''
        """)
        rows = cursor.fetchall()
        print(f"找到 {len(rows)} 条包含空字段且有资源链接的记录。")
        
        updated_count = 0
        
        for row_id, title, res_link, current_size, current_format in rows:
            link_size, link_format = parse_link_metadata(res_link)
            
            new_size = current_size
            new_format = current_format
            updated = False
            
            if (not current_size or current_size == '') and link_size:
                new_size = link_size
                updated = True
                
            if (not current_format or current_format == '') and link_format:
                new_format = link_format
                updated = True
                
            if updated:
                cursor.execute("""
                    UPDATE resources 
                    SET size = ?, resource_format = ? 
                    WHERE id = ?
                """, (new_size, new_format, row_id))
                updated_count += 1
                # 过滤掉无法用 GBK 编码的特殊字符（如 emoji），防止控制台报错
                safe_title = title.encode('gbk', 'ignore').decode('gbk')
                print(f"[ID {row_id}] 标题: {safe_title[:25]}...")
                if current_size != new_size:
                    print(f"  -> Size: {repr(current_size)} -> {repr(new_size)}")
                if current_format != new_format:
                    print(f"  -> Format: {repr(current_format)} -> {repr(new_format)}")
                
        if updated_count > 0:
            conn.commit()
            print(f"数据库 {db_path} 修复完成！共成功回填更新了 {updated_count} 条记录。")
        else:
            print("未发现可通过链接补充提取到元数据的记录。")
            
        conn.close()
        return True
    except Exception as e:
        print(f"修复数据库 {db_path} 时发生错误: {e}")
        return False

def main():
    # 路径去重并规范化
    seen = set()
    unique_paths = []
    for p in DB_PATHS:
        normalized = os.path.abspath(p).lower()
        if normalized not in seen:
            seen.add(normalized)
            unique_paths.append(p)
            
    fixed_any = False
    for path in unique_paths:
        if fix_database(path):
            fixed_any = True
            
    if not fixed_any:
        print("\n未在任何有效路径中找到数据库文件。")

if __name__ == "__main__":
    # 强制控制台输出使用 utf-8 编码，防止中文乱码
    if sys.platform.startswith('win'):
        if sys.stdout.encoding != 'utf-8':
            try:
                sys.stdout.reconfigure(encoding='utf-8')
                sys.stderr.reconfigure(encoding='utf-8')
            except AttributeError:
                pass
    main()
