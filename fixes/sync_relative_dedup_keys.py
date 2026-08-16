"""
批量同步历史数据的规范化相对路径去重键到 AWS DynamoDB 和本地 Bloom Filter
用于解决站点镜像域名轮换（*.xyz）导致历史 URL 无法命中第一道去重的瓶颈问题。
"""
import os
import sys
import time
import sqlite3
from concurrent.futures import ThreadPoolExecutor

# 添加项目根目录到 Python 路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from utils.deduplication import DynamoDBDeduplicationService, extract_url_relative_path, get_url_dedup_key
from utils.logger import get_logger

logger = get_logger(__name__)


def sync_relative_keys(db_path="all_data.db", batch_size=25, max_workers=10, limit=None, sources=None, qps=None):
    """从 SQLite 数据库提取所有资源的相对路径键，并批量写入 DynamoDB"""
    if not os.path.exists(db_path):
        logger.error("[-] SQLite 数据库文件不存在: %s", db_path)
        return

    logger.info("[*] 正在连接 SQLite 数据库: %s", db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 构建查询条件
    where_clauses = ["url IS NOT NULL", "url != ''"]
    params = []
    if sources:
        placeholders = ','.join(['?'] * len(sources))
        where_clauses.append(f"source IN ({placeholders})")
        params.extend(sources)
    where_str = " AND ".join(where_clauses)

    # 统计数据量
    cur.execute(f"SELECT COUNT(*) FROM resources WHERE {where_str}", params)
    total_count = cur.fetchone()[0]
    logger.info("[*] 数据库中共有 %s 条待同步 URL 记录 (指定来源: %s)", total_count, sources or '全部')

    # 初始化 DynamoDB 服务
    service = DynamoDBDeduplicationService()
    
    # 提取所有符合条件的 source, url, resource_link
    query = f"SELECT source, url, resource_link FROM resources WHERE {where_str}"
    if limit:
        query += f" LIMIT {limit}"
    cur.execute(query, params)

    items_to_put = []
    seen_keys = set()

    count = 0
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
                # 同时加进内存 Bloom Filter
                service._url_bloom.add(rel_key)
                service._url_bloom.add(url)

    conn.close()
    logger.info("[+] 提取完成，共生成 %s 个独立的相对路径去重键", len(items_to_put))

    # 批量写入 DynamoDB (batch_write_item 最大支持 25 条)
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
        # 限速平稳模式 (适用于免费 25 WCU Provisioned 模式，0 费用)
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
        # 极速并发模式 (适用于 PAY_PER_REQUEST 按需模式)
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

    # 保存 Bloom Filter
    service._bloom_dirty = True
    service._save_bloom_filter_sync()
    service.shutdown()

    total_time = time.time() - start_time
    logger.info("[🎉] 同步完成！总耗时: %.1f 秒，成功写入 %s 个规范化相对路径键至 DynamoDB 和 Bloom Filter 缓存！", 
                total_time, written_count)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="批量同步相对路径去重键到 DynamoDB")
    parser.add_argument("--db", type=str, default="all_data.db", help="SQLite 数据库路径")
    parser.add_argument("--sources", nargs="+", default=None, help="指定需要同步的来源 (如 datang jingpin taose)")
    parser.add_argument("--workers", type=int, default=10, help="并发写入线程数")
    parser.add_argument("--qps", type=float, default=None, help="写入速率上限 QPS (例: 24 用于 100%% 免费 Provisioned 模式)")
    parser.add_argument("--limit", type=int, default=None, help="限制同步条数 (用于测试)")
    args = parser.parse_args()

    sync_relative_keys(
        db_path=args.db,
        max_workers=args.workers,
        qps=args.qps,
        limit=args.limit,
        sources=args.sources
    )
