"""Supabase (PostgreSQL) 云端到本地 SQLite 数据同步、去重合并与归档工具 (sync/supabase_sync.py)

本脚本负责将部署在 Supabase 云端数据库 (PostgreSQL) 中的爬虫数据安全、无损地拉取并合并至本地 SQLite 数据库，并可选择在本地备份完毕后分批清理云端已同步的数据以释放 Supabase 500MB 免费配额。

核心执行流程与功能特性：

1. 本地数据库安全前置备份:
   - 同步开始前，自动创建带时间戳的本地 SQLite 备份副本 (格式: db.bak_supabase_YYYYMMDD_HHMMSS)，防止本地数据被意外污染或中断损坏。支持 --no-backup 参数跳过。

2. 本地表结构自检与唯一索引保障:
   - 自动调用 DBManager.ensure_tables 初始化本地 resources 表结构与 12 项标准字段，并构建 url 唯一索引 idx_resource_url，为去重插入做好准备。

3. 基于 ID 游标的高性能流式拉取与幂等合并:
   - 采用 ID 游标分页 (.gt("id", last_id).order("id").limit(1000))，避免深度分页性能衰减与数据遗漏。
   - 采用 SQLite 事务批量 INSERT OR IGNORE 方式写入，对于已存在相同 URL 的记录自动去重忽略，确保云端到本地合并的完全幂等性与数据无损性。

4. 结构化同步结果汇报:
   - 清晰统计并输出：云端读取总数、本地新增条数、重复忽略条数及同步后本地总记录数。

5. 云端已同步数据分批安全清理:
   - 在确认本地数据已成功合并后，支持交互式确认（输入 'DELETE'）或通过参数 --delete-cloud 自动触发云端清理。
   - 采用大步长区间切片 (每次 10,000 条 ID 范围) 批量执行云端 DELETE，安全平滑释放 Supabase 的数据库存储空间。

用法与命令示例:
  python sync/supabase_sync.py                                    # 交互式同步（同步后询问是否清理云端）
  python sync/supabase_sync.py --db /path/to/custom.db            # 指定自定义本地 SQLite 数据库路径
  python sync/supabase_sync.py --no-backup                        # 跳过同步前的本地数据库备份步骤
  python sync/supabase_sync.py --delete-cloud                     # 同步合并完成后自动分批删除云端已同步记录
"""

import os
import sys
import shutil
import sqlite3
import time
import argparse
from datetime import datetime
from urllib.parse import urlparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from utils.db_manager import DBManager
from utils import setup_console_utf8


def get_supabase_client():
    """获取清洗过 URL 的 Supabase 客户端"""
    url = config.SUPABASE_URL.strip() if config.SUPABASE_URL else ""
    key = config.SUPABASE_KEY.strip() if config.SUPABASE_KEY else ""
    if not url or not key:
        print("[-] 错误：未在环境变量中检测到 SUPABASE_URL 或 SUPABASE_KEY")
        sys.exit(1)
    
    from supabase import create_client
    parsed = urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}"
    return create_client(clean_url, key)


def backup_local_db(db_path: str):
    """备份本地 SQLite 数据库"""
    if not os.path.exists(db_path):
        print(f"[*] 本地数据库 {db_path} 不存在，无需备份，稍后将自动创建新库。")
        return
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_supabase_{timestamp}"
    try:
        shutil.copy2(db_path, backup_path)
        print(f"[+] 本地数据库备份成功: {backup_path}")
    except Exception as e:
        print(f"[-] 备份本地数据库失败: {e}")
        sys.exit(1)


def get_row_count(conn: sqlite3.Connection) -> int:
    """获取 SQLite 数据库中 resources 表的记录条数"""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM resources")
    return cursor.fetchone()[0]


def sync_data(db_path: str = None, do_backup: bool = True, delete_cloud: bool = False):
    """从 Supabase 分页同步数据到本地 SQLite"""
    db_path = db_path or config.get_db_path()
    print("=" * 60)
    print("       Supabase 云端数据同步与归档（云端 → 本地）")
    print("=" * 60)
    print(f"[*] 本地数据库路径: {db_path}")

    if do_backup:
        backup_local_db(db_path)

    print("[*] 正在初始化本地数据库表结构与索引...")
    DBManager.ensure_tables(db_path)

    local_conn = sqlite3.connect(db_path)
    print("[*] 正在连接云端 Supabase 数据库...")
    sb_client = get_supabase_client()
    table_name = "resources"

    last_id = 0
    batch_size = 1000
    total_fetched = 0
    synced_ids = []

    initial_local_count = get_row_count(local_conn)
    print(f"[*] 同步前本地数据库资源数: {initial_local_count}")
    print("[*] 开始分页同步云端数据...")

    while True:
        try:
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

        last_id = data_list[-1]['id']

    final_local_count = get_row_count(local_conn)
    local_added = final_local_count - initial_local_count
    local_ignored = total_fetched - local_added

    print("\n" + "=" * 50)
    print("📊 同步合并结果汇报:")
    print(f"  云端读取总数       : {total_fetched} 条")
    print(f"  本地新增合并数     : {local_added} 条")
    print(f"  本地重复忽略数     : {local_ignored} 条")
    print(f"  当前本地总记录数   : {final_local_count} 条")
    print("=" * 50 + "\n")

    local_conn.close()

    if total_fetched == 0:
        print("[*] 云端数据库无新数据。")
        return

    # 安全清理云端已同步数据
    should_clear = delete_cloud
    if not should_clear:
        confirm_clear = input('''[?] 是否需要从云端数据库中删除这部分已成功备份的记录？
    警告: 此操作将批量删除云端数据以释放配额，请输入 'DELETE' 确认执行，或按其他键跳过: ''').strip().upper()
        should_clear = (confirm_clear == 'DELETE')

    if should_clear:
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
                break
            current_start = current_end
        print("[+] 云端数据库已同步的数据清理完成！")
    else:
        print("[*] 跳过云端清理。本地已成功备份并去重合并。")


def main():
    setup_console_utf8()
    parser = argparse.ArgumentParser(description="Supabase 到本地 SQLite 数据同步与归档工具")
    parser.add_argument("--db", type=str, default=None, help="本地 SQLite 数据库路径")
    parser.add_argument("--no-backup", action="store_true", default=False, help="跳过本地数据库备份")
    parser.add_argument("--delete-cloud", action="store_true", default=False, help="同步后自动清理云端已同步数据")
    args = parser.parse_args()

    sync_data(
        db_path=args.db,
        do_backup=not args.no_backup,
        delete_cloud=args.delete_cloud
    )


if __name__ == "__main__":
    main()
