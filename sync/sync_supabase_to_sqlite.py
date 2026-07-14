import os
import sys
import shutil
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

# 确保能加载当前目录下的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config
from utils.db_manager import DBManager

def get_supabase_client():
    """获取清洗过 URL 的 Supabase 客户端"""
    url = config.SUPABASE_URL.strip()
    key = config.SUPABASE_KEY.strip()
    if not url or not key:
        print("[-] 错误：未在环境变量中检测到 SUPABASE_URL 或 SUPABASE_KEY")
        sys.exit(1)
    
    from supabase import create_client
    parsed = urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}"
    return create_client(clean_url, key)

def backup_local_db(db_path):
    """备份本地 SQLite 数据库"""
    if not os.path.exists(db_path):
        print(f"[*] 本地数据库 {db_path} 不存在，无需备份，稍后将自动创建新库。")
        return
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_{timestamp}"
    try:
        shutil.copy2(db_path, backup_path)
        print(f"[+] 本地数据库备份成功: {backup_path}")
    except Exception as e:
        print(f"[-] 备份本地数据库失败: {e}")
        # 如果备份失败，为了安全性，在此终止运行
        sys.exit(1)

def get_row_count(conn):
    """获取 SQLite 数据库中 resources 表的记录条数"""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM resources")
    return cursor.fetchone()[0]

def sync_data():
    # 1. 确定本地数据库路径
    db_path = config.get_db_path()
    print(f"[*] 本地数据库路径: {db_path}")

    # 2. 备份本地数据库
    backup_local_db(db_path)

    # 3. 初始化并连接本地 SQLite
    # 借助 DBManager.ensure_tables 确保表结构和唯一键索引 idx_resource_url 正确初始化
    print("[*] 正在初始化本地数据库表结构与索引...")
    DBManager.ensure_tables(db_path)
    # 无需创建完整 DBManager 实例即可完成表结构初始化

    # 重新以事务模式连接本地数据库
    local_conn = sqlite3.connect(db_path)
    
    # 4. 初始化云端 Supabase 客户端
    print("[*] 正在连接云端 Supabase 数据库...")
    sb_client = get_supabase_client()
    table_name = "resources"

    # 5. 分页同步数据
    last_id = 0
    batch_size = 1000
    total_fetched = 0
    synced_ids = []

    # 获取同步前的本地记录数
    initial_local_count = get_row_count(local_conn)
    print(f"[*] 同步前本地数据库资源数: {initial_local_count}")

    print("[*] 开始分页同步云端数据...")
    while True:
        try:
            # 采用 ID Cursor 分页，速度快，内存占用小
            resp = (
                sb_client.table(table_name)
                .select("*")
                .gt("id", last_id)
                .order("id", desc=False)
                .limit(batch_size)
                .execute()
            )
        except Exception as e:
            print(f"[-] 从云端拉取数据失败: {e}")
            break

        data_list = resp.data
        if not data_list:
            break

        total_fetched += len(data_list)
        print(f"[*] 已从云端读取到 {len(data_list)} 条记录 (累计: {total_fetched})...")

        # 整理成批量插入的参数元组列表
        insert_tuples = []
        for item in data_list:
            insert_tuples.append((
                item.get('title'),
                item.get('publish_time'),
                item.get('category'),
                item.get('resource_link'),
                item.get('pikpak_link'),
                item.get('size'),
                item.get('resource_format'),
                item.get('link_type', ''),
                item.get('url'),
                item.get('pdf_path', ''),
                item.get('source')
            ))
            synced_ids.append(item.get('id'))

        # 在事务中批量写入 SQLite
        try:
            cursor = local_conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            cursor.executemany('''
                INSERT OR IGNORE INTO resources (
                    title, publish_time, category, resource_link, pikpak_link, 
                    size, resource_format, link_type, url, pdf_path, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', insert_tuples)
            local_conn.commit()
        except Exception as e:
            local_conn.rollback()
            print(f"[-] 批量写入本地 SQLite 失败: {e}")
            sys.exit(1)

        # 更新下一页游标
        last_id = data_list[-1]['id']

    # 获取同步后的本地记录数
    final_local_count = get_row_count(local_conn)
    local_added = final_local_count - initial_local_count
    local_ignored = total_fetched - local_added

    print("\n" + "="*40)
    print("同步合并结果汇报:")
    print(f"  云端读取总数       : {total_fetched} 条")
    print(f"  本地新增合并数     : {local_added} 条")
    print(f"  本地重复忽略数     : {local_ignored} 条")
    print(f"  当前本地总记录数   : {final_local_count} 条")
    print("="*40 + "\n")

    local_conn.close()

    if total_fetched == 0:
        print("[*] 云端数据库为空，无需任何操作。")
        return

    # 6. 安全清理云端已同步的数据
    confirm_clear = input("[?] 是否需要从云端数据库中删除这部分已成功备份的记录？
    警告: 此操作将批量删除云端数据，请输入 'DELETE' 确认执行，或按其他键取消: ").strip().upper()
    if confirm_clear == 'DELETE':
        # 查找云端当前的最小 ID，以便精确确定删除起点
        try:
            resp_min = sb_client.table(table_name).select("id").order("id", desc=False).limit(1).execute()
            if not resp_min.data:
                print("[-] 无法获取云端数据最小 ID，跳过清理。")
                return
            min_id = resp_min.data[0]['id']
        except Exception as e:
            print(f"[-] 获取云端最小 ID 失败: {e}，跳过清理。")
            return

        if min_id > last_id:
            print("[*] 最小 ID 大于同步最大 ID，无需清理。")
            return

        print(f"[*] 准备分批清理云端已同步的记录 (ID 范围: {min_id} 至 {last_id})...")
        step = 10000
        current_start = min_id
        total_batches = (last_id - min_id) // step + 1
        current_batch = 0
        import time

        while current_start <= last_id:
            current_batch += 1
            current_end = min(current_start + step, last_id + 1)
            try:
                t0 = time.time()
                sb_client.table(table_name).delete(returning="minimal").gte("id", current_start).lt("id", current_end).execute()
                dur = time.time() - t0
                print(f"[+] 进度 {current_batch}/{total_batches}: 已清理 ID 在 [{current_start}, {current_end}) 之间的云端记录 (耗时 {dur:.2f} 秒)")
            except Exception as e:
                print(f"[-] 清理批次 [{current_start}, {current_end}) 失败: {e}")
                print("[!] 清理过程中断，部分数据已保留。")
                sys.exit(1)
            current_start = current_end
        print("[+] 云端数据库已同步的数据清理完成！")
    else:
        print("[*] 跳过云端清理。本地已成功备份并去重合并。")

if __name__ == "__main__":
    sync_data()