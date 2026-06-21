import os
import re
import sys
import sqlite3

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 兼容的数据库路径列表
DB_PATHS = [
    r"d:\programme\seju\all_data.db",
    r"d:\seju\all_data.db"
]

def parse_title(title):
    if not title:
        return None, None
        
    # 1. 优先尝试匹配方括号中的内容，这是绝大多数最规范的格式
    bracket_matches = re.findall(r'\[([^\]]+)\]', title)
    bracket_content = bracket_matches[-1] if bracket_matches else None
    
    size_val = None
    formats = []
    
    if bracket_content:
        parts = bracket_content.split('/')
        for part in parts:
            part = part.strip()
            # 视频 (e.g. 16V) 或者 图片 (e.g. 1077P)
            if re.match(r'^\d+[Vv]$', part):
                formats.append(part.upper())
            elif re.match(r'^\d+[Pp]$', part):
                formats.append(part.upper())
            elif re.match(r'^\d+(?:\.\d+)?\s*(?:[a-zA-Z]+)?$', part):
                size_val = part.upper()
            else:
                if not size_val and not any(c in part.upper() for c in ['V', 'P']):
                    size_val = part
    else:
        # 2. 如果没有方括号，通过正则匹配标题中的特定元数据
        # 视频数匹配 (排除前后是数字或字母的干扰)
        v_matches = re.findall(r'(?<![a-zA-Z0-9])(\d+)[Vv](?![a-zA-Z0-9])', title)
        # 图片数匹配
        p_matches = re.findall(r'(?<![a-zA-Z0-9])(\d+)[Pp](?![a-zA-Z0-9])', title)
        
        valid_v = []
        for v in v_matches:
            valid_v.append(f"{v}V")
            
        valid_p = []
        for p in p_matches:
            p_int = int(p)
            # 排除常见的视频分辨率 1080P, 720P
            if p_int in [1080, 720]:
                continue
            # 单独的小数字 P (如 3P, 4P) 通常代表玩法而不是图片张数，若没有 V 伴随，则过滤
            if p_int <= 5 and not v_matches:
                continue
            valid_p.append(f"{p}P")
            
        if valid_v or valid_p:
            formats = valid_v + valid_p
                
        # 匹配大小，例如 10.9G, 5.83G, 10.9GB, 500MB 等
        size_match = re.search(r'(?<![a-zA-Z0-9])(\d+(?:\.\d+)?\s*[GgMmTt][Bb]?)(?![a-zA-Z0-9])', title)
        if size_match:
            size_val = size_match.group(1).upper()
            
    # 格式化资源形式，例如 "30V" 或 "137V/537P"
    if formats:
        v_parts = [f for f in formats if 'V' in f]
        p_parts = [f for f in formats if 'P' in f]
        resource_format = "/".join(v_parts + p_parts)
    else:
        resource_format = None
        
    return size_val, resource_format

def parse_pikpak_link(resource_link):
    """
    尝试从 resource_link 中提取 PikPak 链接
    """
    if not resource_link:
        return None
    match = re.search(r'(https?://[a-zA-Z0-9][-a-zA-Z0-9]{0,62}(?:\.[a-zA-Z0-9][-a-zA-Z0-9]{0,62})*pikpak\.[a-zA-Z]{2,}(?:/[^\s]*)?)', resource_link)
    if match:
        return match.group(1).strip()
    return None

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
        except:
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
    updated_any = False
    for path in DB_PATHS:
        if upgrade_database(path):
            updated_any = True
            
    if not updated_any:
        print("\n[警告] 未在指定路径找到任何数据库文件，请检查数据库路径配置。")

if __name__ == '__main__':
    main()
