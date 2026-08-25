"""AWS DynamoDB 数据同步中心 (sync/dynamodb_sync.py)

整合 DynamoDB 相关操作：
  1. upload    - 对比本地 SQLite 与云端 DynamoDB，增量上传缺失的 URL 与磁力链接 (原 upload_to_dynamodb.py)
  2. sync-keys - 批量生成规范化相对路径去重键并同步到 DynamoDB 及本地 Bloom Filter (原 sync_relative_dedup_keys.py)

用法:
  python sync/dynamodb_sync.py                     # 交互式菜单
  python sync/dynamodb_sync.py upload              # 增量比对并上传
  python sync/dynamodb_sync.py sync-keys           # 批量同步相对路径去重键
  python sync/dynamodb_sync.py sync-keys --qps 24  # 免费 Provisioned 模式限速同步
"""

import argparse
import os
import sys
import time
import sqlite3
from typing import Dict, List, Optional, Set
from concurrent.futures import ThreadPoolExecutor

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from utils.logger import get_logger
from utils import setup_console_utf8
from utils.deduplication import DynamoDBDeduplicationService, get_url_dedup_key

logger = get_logger(__name__)

TABLE_NAME = "fuli_resources"


def get_dynamodb_client():
    """获取初始化的 boto3 DynamoDB 客户端"""
    import boto3
    if not config.AWS_ACCESS_KEY_ID or not config.AWS_SECRET_ACCESS_KEY:
        raise ValueError(
            "AWS 密钥未配置！请确保 .env 文件中设置了 AWS_ACCESS_KEY_ID 和 AWS_SECRET_ACCESS_KEY。"
        )
    return boto3.client(
        "dynamodb",
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def ensure_dynamodb_table(client=None, table_name=TABLE_NAME, key_name="url"):
    """检查并自动创建 DynamoDB 表（若不存在）"""
    import boto3
    from botocore.exceptions import ClientError
    client = client or get_dynamodb_client()
    try:
        existing_tables = client.list_tables()["TableNames"]
        if table_name in existing_tables:
            return
        print(f"[*] 表 {table_name} 不存在，正在创建 (25 RCU / 25 WCU)...")
        client.create_table(
            TableName=table_name,
            AttributeDefinitions=[{"AttributeName": key_name, "AttributeType": "S"}],
            KeySchema=[{"AttributeName": key_name, "KeyType": "HASH"}],
            BillingMode="PROVISIONED",
            ProvisionedThroughput={
                "ReadCapacityUnits": 25,
                "WriteCapacityUnits": 25,
            }
        )
        waiter = client.get_waiter("table_exists")
        waiter.wait(TableName=table_name)
        print(f"[+] 表 {table_name} 创建成功！")
    except ClientError as e:
        print(f"[-] 检查/创建表失败: {e}")


# ===================================================================
# 1. upload: 全量对比本地与云端，增量上传缺失的 URL 与磁力
# ===================================================================

def get_cloud_keys(client, table_name: str, key_name: str) -> Set[str]:
    """全量扫描 DynamoDB 返回云端所有的 key 集合"""
    cloud_keys = set()
    last_evaluated_key = None
    while True:
        kwargs = {
            "TableName": table_name,
            "ProjectionExpression": "#key",
            "ExpressionAttributeNames": {"#key": key_name},
        }
        if last_evaluated_key:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        response = client.scan(**kwargs)
        for item in response.get("Items", []):
            key_val = item.get(key_name, {})
            if "S" in key_val and key_val["S"]:
                cloud_keys.add(key_val["S"])
        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return cloud_keys


def run_upload(args=None):
    """对比本地 SQLite 和云端 DynamoDB，增量上传缺失的数据"""
    from botocore.exceptions import ClientError
    db_path = getattr(args, "db", None) or config.get_db_path()

    print("=" * 60)
    print("        AWS DynamoDB 增量数据同步（本地 → 云端）")
    print("=" * 60)
    print(f"[*] 本地数据库: {db_path}")
    print(f"[*] DynamoDB 目标表: {TABLE_NAME}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 本地数据库文件不存在: {db_path}")
        return

    client = get_dynamodb_client()
    ensure_dynamodb_table(client, TABLE_NAME, "url")

    # 读取本地所有记录
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT url, resource_link FROM resources WHERE url IS NOT NULL AND url != ''")
    local_rows = cur.fetchall()
    local_items = {row[0]: row[1] if row[1] else "" for row in local_rows}
    conn.close()

    local_keys = set(local_items.keys())
    print(f"[+] 本地共有 {len(local_keys)} 条有效 URL 记录")

    if not local_keys:
        print("[+] 本地无数据，无需上传。")
        return

    print("[*] 正在扫描云端 DynamoDB 获取现有 URL 集合...")
    cloud_keys = get_cloud_keys(client, TABLE_NAME, "url")
    print(f"[+] 云端现有 {len(cloud_keys)} 条记录")

    missing_keys = local_keys - cloud_keys
    missing_list = sorted(missing_keys)
    total = len(missing_list)
    print(f"[+] 本地有但云端缺失的记录: {total} 条")

    if total == 0:
        print("[✓] 本地与云端数据完全一致，无需上传！")
        return

    BATCH_SIZE = 25
    inserted = 0
    skipped = 0
    batch = []
    batch_keys = []

    for i, key in enumerate(missing_list, 1):
        item = {"url": {"S": key}}
        magnet = local_items.get(key, "")
        if magnet:
            item["resource_link"] = {"S": magnet}
        batch.append({"PutRequest": {"Item": item}})
        batch_keys.append(key)

        if len(batch) == BATCH_SIZE or i == total:
            try:
                response = client.batch_write_item(RequestItems={TABLE_NAME: batch})
                unprocessed = response.get("UnprocessedItems", {}).get(TABLE_NAME, [])
                retry_count = 0
                while unprocessed and retry_count < 3:
                    time.sleep(2 ** retry_count * 0.5)
                    response = client.batch_write_item(RequestItems={TABLE_NAME: unprocessed})
                    unprocessed = response.get("UnprocessedItems", {}).get(TABLE_NAME, [])
                    retry_count += 1

                unprocessed_keys = set()
                if unprocessed:
                    for u_item in unprocessed:
                        ukey = u_item.get("PutRequest", {}).get("Item", {}).get("url", {}).get("S")
                        if ukey:
                            unprocessed_keys.add(ukey)
                    skipped += len(unprocessed_keys)

                success_keys = [k for k in batch_keys if k not in unprocessed_keys]
                inserted += len(success_keys)
                if inserted % 200 == 0 or i == total:
                    print(f"[*] 进度: 已上传 {inserted}/{total} 条" + (f" (跳过/未处理: {skipped} 条)" if skipped else ""))
            except ClientError as e:
                print(f"[-] 写入批次失败: {e}")
                skipped += len(batch)

            batch = []
            batch_keys = []

    print(f"\n[✓] 上传完成！成功写入: {inserted} 条，跳过/失败: {skipped} 条")


# ===================================================================
# 2. sync-keys: 提取相对路径键并同步至 DynamoDB 与 Bloom Filter
# ===================================================================

def run_sync_keys(args=None):
    """提取规范化相对路径去重键批量同步写入 DynamoDB 与本地 Bloom Filter"""
    db_path = getattr(args, "db", None) or config.get_db_path()
    batch_size = getattr(args, "batch_size", 25)
    max_workers = getattr(args, "workers", 10)
    limit = getattr(args, "limit", None)
    sources = getattr(args, "sources", None)
    qps = getattr(args, "qps", None)

    if not os.path.exists(db_path):
        logger.error("[-] SQLite 数据库文件不存在: %s", db_path)
        return

    logger.info("[*] 正在连接 SQLite 数据库: %s", db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    where_clauses = ["url IS NOT NULL", "url != ''"]
    params = []
    if sources:
        placeholders = ','.join(['?'] * len(sources))
        where_clauses.append(f"source IN ({placeholders})")
        params.extend(sources)
    where_str = " AND ".join(where_clauses)

    cur.execute(f"SELECT COUNT(*) FROM resources WHERE {where_str}", params)
    total_count = cur.fetchone()[0]
    logger.info("[*] 数据库中共有 %s 条待处理 URL 记录 (指定来源: %s)", total_count, sources or '全部')

    service = DynamoDBDeduplicationService()
    query = f"SELECT source, url, resource_link FROM resources WHERE {where_str}"
    if limit:
        query += f" LIMIT {limit}"
    cur.execute(query, params)

    items_to_put = []
    seen_keys = set()
    start_time = time.time()

    logger.info("[*] 正在生成相对路径规范化键...")
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        for source, url, resource_link in rows:
            rel_key = get_url_dedup_key(url, source)
            if rel_key and rel_key not in seen_keys:
                seen_keys.add(rel_key)
                items_to_put.append((rel_key, resource_link or ""))
                service._url_bloom.add(rel_key)
                service._url_bloom.add(url)

    conn.close()
    logger.info("[+] 提取完成，共生成 %s 个独立的相对路径去重键", len(items_to_put))

    def _write_batch_chunk(chunk):
        request_items = {
            service.table_name: [
                {
                    "PutRequest": {
                        "Item": {
                            "url": {"S": k},
                            **({"resource_link": {"S": r}} if (r and len(r.encode('utf-8')) <= 2048) else {})
                        }
                    }
                }
                for k, r in chunk
            ]
        }
        max_retries = 5
        for attempt in range(max_retries):
            try:
                resp = service.client.batch_write_item(RequestItems=request_items)
                unprocessed = resp.get("UnprocessedItems", {}).get(service.table_name, [])
                if not unprocessed:
                    return len(chunk)
                request_items = {service.table_name: unprocessed}
                time.sleep(0.5 * (attempt + 1))
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.warning("写入 DynamoDB 批次异常: %s", e)
                time.sleep(1.0)
        return len(chunk)

    chunks = [items_to_put[i:i + batch_size] for i in range(0, len(items_to_put), batch_size)]
    total_batches = len(chunks)
    logger.info("[*] 开始写入 DynamoDB（共 %s 个批次, %s 个键, 并发数 %s, 速率限制 QPS: %s）...", 
                total_batches, len(items_to_put), max_workers, qps or '无限制')

    written_count = 0
    if qps and qps > 0:
        interval = batch_size / qps
        for idx, chunk in enumerate(chunks, 1):
            t_chunk_start = time.time()
            written_count += _write_batch_chunk(chunk)
            if idx % 10 == 0 or idx == total_batches:
                elapsed = time.time() - start_time
                pct = (idx / total_batches) * 100
                speed = written_count / elapsed if elapsed > 0 else 0
                remaining_batches = total_batches - idx
                eta_sec = (remaining_batches * batch_size) / speed if speed > 0 else 0
                msg = f"[{pct:5.1f}%] 批次 {idx}/{total_batches} | 已同步 {written_count}/{len(items_to_put)} 键 | 速度: {speed:5.1f} 键/秒 | 预估剩余: {int(eta_sec)}秒"
                print(f"[进度] {msg}", flush=True)
                if idx % 50 == 0 or idx == total_batches:
                    logger.info(msg)
            t_used = time.time() - t_chunk_start
            if t_used < interval:
                time.sleep(interval - t_used)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_write_batch_chunk, c) for c in chunks]
            for idx, fut in enumerate(futures, 1):
                written_count += fut.result()
                if idx % 50 == 0 or idx == total_batches:
                    elapsed = time.time() - start_time
                    pct = (idx / total_batches) * 100
                    speed = written_count / elapsed if elapsed > 0 else 0
                    remaining_batches = total_batches - idx
                    eta_sec = (remaining_batches * batch_size) / speed if speed > 0 else 0
                    msg = f"[{pct:5.1f}%] 批次 {idx}/{total_batches} | 已同步 {written_count}/{len(items_to_put)} 键 | 速度: {speed:5.1f} 键/秒 | 预估剩余: {int(eta_sec)}秒"
                    print(f"[进度] {msg}", flush=True)
                    if idx % 200 == 0 or idx == total_batches:
                        logger.info(msg)

    service._bloom_dirty = True
    service._save_bloom_filter_sync()
    service.shutdown()

    total_time = time.time() - start_time
    logger.info("[🎉] 同步完成！总耗时: %.1f 秒，成功写入 %s 个规范化相对路径键至 DynamoDB 和 Bloom Filter 缓存！", 
                total_time, written_count)


# ===================================================================
# 3. 交互式菜单与 CLI 入口
# ===================================================================

def interactive_menu():
    """DynamoDB 交互式同步菜单"""
    print(f"\n{'=' * 60}")
    print("             AWS DynamoDB 同步中心")
    print(f"{'=' * 60}")
    print("  1. upload    - 对比本地与云端，增量上传缺失的 URL 与磁力链接")
    print("  2. sync-keys - 批量生成规范化相对路径去重键并同步 (含 Bloom Filter)")
    print()
    print("  0. 退出")
    print("=" * 60)

    try:
        choice = input("请输入序号 [0-2] (直接回车退出): ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n[*] 操作已取消。")
        return

    if choice == "1":
        run_upload()
    elif choice == "2":
        qps_str = input("请输入限速 QPS (免费 Provisioned 模式请输入 24, 直接回车为极速并发模式): ").strip()
        qps = float(qps_str) if qps_str else None
        ns = argparse.Namespace(
            db=None,
            batch_size=25,
            workers=10,
            limit=None,
            sources=None,
            qps=qps
        )
        run_sync_keys(ns)
    else:
        print("[*] 已退出。")


def main():
    setup_console_utf8()
    parser = argparse.ArgumentParser(description="AWS DynamoDB 数据同步中心 (增量上传 / 相对路径去重键同步)")
    subparsers = parser.add_subparsers(dest="command", help="可用的子命令")

    # upload
    p_up = subparsers.add_parser("upload", help="对比本地 SQLite 与 DynamoDB，增量上传缺失的 URL 与磁力")
    p_up.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_up.set_defaults(func=run_upload)

    # sync-keys
    p_keys = subparsers.add_parser("sync-keys", help="提取相对路径去重键批量同步到 DynamoDB 与 Bloom Filter")
    p_keys.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_keys.add_argument("--sources", nargs="+", default=None, help="指定来源过滤 (如 datang jingpin taose)")
    p_keys.add_argument("--workers", type=int, default=10, help="并发写入线程数 (默认 10)")
    p_keys.add_argument("--qps", type=float, default=None, help="写入速率上限 QPS (例: 24)")
    p_keys.add_argument("--limit", type=int, default=None, help="限制同步条数 (测试用)")
    p_keys.set_defaults(func=run_sync_keys)

    args = parser.parse_args()

    if args.command is None:
        interactive_menu()
    else:
        args.func(args)


if __name__ == "__main__":
    main()
