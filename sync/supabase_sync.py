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
  python sync/supabase_sync.py --clean-only                       # 独立清理云端已同步记录（安全校验本地副本）
  python sync/supabase_sync.py --clean-only --dry-run             # 仅模拟预览待清理记录，不执行实际删除
  python sync/supabase_sync.py --clean-only --limit 5000          # 限制单次清理的最大记录数
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


def delete_synced_data(db_path: str = None, dry_run: bool = False, limit: int = 0, batch_size: int = 1000):
    """
    独立安全清理云端已同步记录：
    通过安全反向比对本地 SQLite 数据库中的有效资源（基于 url 唯一键），
    仅清理本地已确凿存在有效归档副本的云端记录，防止误删未同步的新数据。
    """
    db_path = db_path or config.get_db_path()
    print("=" * 60)
    print("       Supabase 云端已同步数据独立安全清理")
    print("=" * 60)
    print(f"[*] 本地比对数据库路径: {db_path}")

    if not os.path.exists(db_path):
        print(f"[-] 错误：本地数据库文件不存在 ({db_path})！无法比对归档状态，已终止操作以保护云端数据。")
        return {
            "total_scanned": 0,
            "verified_count": 0,
            "unmatched_count": 0,
            "deleted_count": 0,
            "error": "db_not_found"
        }

    print("[*] 正在加载本地数据库有效 URL 索引集合...")
    t_start = time.time()
    try:
        local_conn = sqlite3.connect(db_path)
        cursor = local_conn.cursor()
        cursor.execute("SELECT url FROM resources WHERE url IS NOT NULL AND url != ''")
        local_urls = set(row[0] for row in cursor.fetchall())
        local_conn.close()
        print(f"[+] 本地有效 URL 索引加载完成，共 {len(local_urls):,} 条记录 (耗时 {time.time() - t_start:.2f} 秒)")
    except Exception as e:
        print(f"[-] 读取本地数据库失败: {e}")
        return {
            "total_scanned": 0,
            "verified_count": 0,
            "unmatched_count": 0,
            "deleted_count": 0,
            "error": str(e)
        }

    print("[*] 正在连接云端 Supabase 数据库...")
    sb_client = get_supabase_client()
    table_name = "resources"

    try:
        resp_cnt = sb_client.table(table_name).select("id", count="exact").limit(1).execute()
        total_cloud = resp_cnt.count or 0
        print(f"[*] 云端数据库现有记录总数: {total_cloud:,} 条")
    except Exception as e:
        print(f"[!] 查询云端总记录数失败: {e}，将直接进行流式扫描")
        total_cloud = 0

    print("[*] 开始流式扫描云端记录并进行本地副本安全比对...")
    last_id = 0
    total_scanned = 0
    verified_ids = []
    unmatched_ids = []

    while True:
        try:
            resp = (
                sb_client.table(table_name)
                .select("id, url")
                .gt("id", last_id)
                .order("id", desc=False)
                .limit(batch_size)
                .execute()
            )
        except Exception as e:
            print(f"[-] 从云端读取记录失败: {e}")
            break

        data_list = resp.data
        if not data_list:
            break

        total_scanned += len(data_list)
        for item in data_list:
            rid = item.get("id")
            rurl = item.get("url")
            if rurl and rurl in local_urls:
                verified_ids.append(rid)
            else:
                unmatched_ids.append(rid)

        last_id = data_list[-1]["id"]
        print(f"[*] 已比对 {total_scanned:,} 条云端记录 (已归档: {len(verified_ids):,}, 未归档保护: {len(unmatched_ids):,})...")

        if limit > 0 and len(verified_ids) >= limit:
            verified_ids = verified_ids[:limit]
            print(f"[*] 已达到最大处理上限 ({limit:,} 条)，提前停止扫描。")
            break

    verified_count = len(verified_ids)
    unmatched_count = len(unmatched_ids)

    print("\n" + "=" * 50)
    print("📊 云端数据比对与安全校验结果:")
    print(f"  云端扫描总数       : {total_scanned:,} 条")
    print(f"  本地已归档(可清理) : {verified_count:,} 条")
    print(f"  本地未归档(受保护) : {unmatched_count:,} 条")
    print("=" * 50 + "\n")

    if verified_count == 0:
        print("[*] 未检测到任何已在本地完成归档的云端记录，无需清理。")
        return {
            "total_scanned": total_scanned,
            "verified_count": 0,
            "unmatched_count": unmatched_count,
            "deleted_count": 0
        }

    if dry_run:
        print("[!] 当前处于仅模拟预览模式 (dry-run)，未执行任何云端删除操作。")
        print(f"[!] 实际执行时将从云端分批删除 {verified_count:,} 条已同步记录以释放存储配额。")
        return {
            "total_scanned": total_scanned,
            "verified_count": verified_count,
            "unmatched_count": unmatched_count,
            "deleted_count": 0
        }

    print(f"[*] 准备从 Supabase 云端分批清理 {verified_count:,} 条已归档记录...")
    deleted_count = 0
    t_del_start = time.time()

    # 如果全量匹配且无截断，使用区间批量快速删除
    if unmatched_count == 0 and verified_count == total_scanned and (limit == 0 or limit >= total_scanned):
        min_id = verified_ids[0]
        max_id = verified_ids[-1]
        step = 5000
        current_start = min_id
        total_batches = (max_id - min_id) // step + 1
        current_batch = 0

        while current_start <= max_id:
            current_batch += 1
            current_end = min(current_start + step, max_id + 1)
            try:
                t0 = time.time()
                sb_client.table(table_name).delete(returning="minimal").gte("id", current_start).lt("id", current_end).execute()
                dur = time.time() - t0
                batch_expected = min(step, max_id - current_start + 1)
                deleted_count += batch_expected
                print(f"[+] 进度 {current_batch}/{total_batches}: 已清理 ID 在 [{current_start}, {current_end}) 之间的记录 (耗时 {dur:.2f} 秒)")
            except Exception as e:
                print(f"[-] 清理批次 [{current_start}, {current_end}) 失败: {e}")
                print("[!] 清理过程中断，部分数据已保留。")
                break
            current_start = current_end
        deleted_count = min(deleted_count, verified_count)
    else:
        # 存在未归档保护记录或 limit 截断时，按 ID 分批精确删除（每批 250 条）
        chunk_size = 250
        chunks = [verified_ids[i:i + chunk_size] for i in range(0, len(verified_ids), chunk_size)]
        total_batches = len(chunks)

        for idx, chunk in enumerate(chunks, 1):
            try:
                t0 = time.time()
                sb_client.table(table_name).delete(returning="minimal").in_("id", chunk).execute()
                dur = time.time() - t0
                deleted_count += len(chunk)
                print(f"[+] 进度 {idx}/{total_batches}: 已清理 {len(chunk)} 条记录 (累计: {deleted_count:,}/{verified_count:,}, 耗时 {dur:.2f} 秒)")
            except Exception as e:
                print(f"[-] 清理批次 {idx}/{total_batches} 失败: {e}")
                print("[!] 清理过程中断，部分数据已保留。")
                break

    del_dur = time.time() - t_del_start
    print("\n" + "=" * 50)
    print("📊 云端已同步数据清理完成汇报:")
    print(f"  云端扫描总数       : {total_scanned:,} 条")
    print(f"  本地已归档(可清理) : {verified_count:,} 条")
    print(f"  本地未归档(受保护) : {unmatched_count:,} 条")
    print(f"  实际成功删除       : {deleted_count:,} 条")
    print(f"  清理执行耗时       : {del_dur:.2f} 秒")
    print("=" * 50 + "\n")

    return {
        "total_scanned": total_scanned,
        "verified_count": verified_count,
        "unmatched_count": unmatched_count,
        "deleted_count": deleted_count
    }


def main():
    setup_console_utf8()
    parser = argparse.ArgumentParser(description="Supabase 到本地 SQLite 数据同步与归档工具")
    parser.add_argument("--db", type=str, default=None, help="本地 SQLite 数据库路径")
    parser.add_argument("--no-backup", action="store_true", default=False, help="跳过本地数据库备份")
    parser.add_argument("--delete-cloud", action="store_true", default=False, help="同步后自动清理云端已同步数据")
    parser.add_argument("--clean-only", action="store_true", default=False, help="单独清理云端已同步数据（安全校验本地副本，不执行同步）")
    parser.add_argument("--dry-run", action="store_true", default=False, help="仅模拟预览待清理数据，不执行实际删除")
    parser.add_argument("--limit", type=int, default=0, help="限制单次清理的最大记录数 (0为不限)")
    args = parser.parse_args()

    if args.clean_only:
        delete_synced_data(
            db_path=args.db,
            dry_run=args.dry_run,
            limit=args.limit
        )
    else:
        sync_data(
            db_path=args.db,
            do_backup=not args.no_backup,
            delete_cloud=args.delete_cloud
        )


if __name__ == "__main__":
    main()
