"""
统计三个平台的数据量和大小：
  1. Supabase (PostgreSQL) — 记录数、表大小
  2. Cloudflare R2 — PDF 文件数、总大小、按年份分布
  3. AWS DynamoDB — 记录数、表大小

用法:
    python stats.py                          # 查询所有平台
    python stats.py --supabase               # 只查 Supabase
    python stats.py --r2                     # 只查 R2
    python stats.py --dynamodb               # 只查 DynamoDB
    python stats.py --r2 --year 2025         # 只查 R2 指定年份
"""
import os
import sys
import argparse
import time
from datetime import datetime

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 确保能加载项目根目录的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def format_size(size_bytes):
    """格式化字节数为人类可读格式"""
    if size_bytes >= 1024 ** 4:
        return f"{size_bytes / 1024 ** 4:.2f} TB"
    elif size_bytes >= 1024 ** 3:
        return f"{size_bytes / 1024 ** 3:.2f} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{size_bytes / 1024 ** 2:.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def query_supabase():
    """查询 Supabase 数据库的记录数和表大小"""
    print("\n" + "=" * 50)
    print("📊 Supabase (PostgreSQL) 统计")
    print("=" * 50)

    if not config.use_supabase():
        print("[-] Supabase 未配置，跳过")
        return

    from supabase import create_client
    from urllib.parse import urlparse

    url = config.SUPABASE_URL.strip()
    key = config.SUPABASE_KEY.strip()
    parsed = urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}"
    client = create_client(clean_url, key)

    # 1. 获取总记录数
    print("[*] 正在查询 Supabase 记录数...")
    try:
        resp = client.table("resources").select("*", count="exact").execute()
        total_count = resp.count
        print(f"[+] 总记录数: {total_count:,}")
    except Exception as e:
        print(f"[-] 查询记录数失败: {e}")
        return

    # 2. 按 source 分组统计
    print("[*] 正在按来源分组统计...")
    try:
        resp = client.table("resources").select("source").execute()
        sources = {}
        for row in resp.data:
            src = row.get("source", "unknown")
            sources[src] = sources.get(src, 0) + 1

        print(f"\n  按来源分布:")
        for src, cnt in sorted(sources.items(), key=lambda x: -x[1]):
            print(f"    {src}: {cnt:,} 条")
    except Exception as e:
        print(f"[-] 按来源分组失败: {e}")

    # 3. 查询表大小（通过 SQL 端点）
    print("[*] 正在查询表大小...")
    try:
        import requests
        sql = "SELECT pg_size_pretty(pg_total_relation_size('resources')) AS size;"
        resp = requests.post(
            f"{clean_url}/rest/v1/sql",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            },
            json={"query": sql},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                table_size = data[0].get('size', 'N/A')
                print(f"[+] 表大小: {table_size}")
                print(f"    💡 免费额度: 500 MB 数据库存储 (Supabase Free Tier)")
        else:
            print(f"[-] 查询表大小失败 (HTTP {resp.status_code})")
    except Exception as e:
        print(f"[-] 查询表大小失败: {e}")

    # 4. 查询 PDF 文件数（有 pdf_path 的记录）
    print("[*] 正在查询 PDF 文件数...")
    try:
        resp = client.table("resources").select("pdf_path", count="exact").neq("pdf_path", "").execute()
        pdf_count = resp.count
        print(f"[+] 有 PDF 路径的记录: {pdf_count:,}")
    except Exception as e:
        print(f"[-] 查询 PDF 记录数失败: {e}")


def query_r2(year=None):
    """查询 Cloudflare R2 的 PDF 文件数和大小"""
    print("\n" + "=" * 50)
    print("📊 Cloudflare R2 统计")
    print("=" * 50)

    if not config.use_r2():
        print("[-] R2 未配置，跳过")
        return

    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    client = boto3.client(
        "s3",
        endpoint_url=config.R2_ENDPOINT_URL,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(s3={"addressing_style": "path"}),
    )

    # 构建前缀列表
    if year:
        prefixes = [f"pdfs/{year}/"]
    else:
        # 扫描所有年份前缀
        prefixes = ["pdfs/"]

    total_count = 0
    total_size = 0
    year_stats = {}

    for prefix in prefixes:
        print(f"[*] 正在扫描 R2 前缀: {prefix}")
        paginator = client.get_paginator("list_objects_v2")
        page_count = 0

        for page in paginator.paginate(Bucket=config.R2_BUCKET_NAME, Prefix=prefix):
            page_count += 1
            if "Contents" not in page:
                continue

            for obj in page["Contents"]:
                key = obj["Key"]
                if not key.lower().endswith(".pdf"):
                    continue

                size = obj.get("Size", 0)
                total_count += 1
                total_size += size

                # 提取年份
                parts = key.split("/")
                if len(parts) >= 2:
                    y = parts[1]
                    if y.isdigit():
                        year_stats[y] = year_stats.get(y, {"count": 0, "size": 0})
                        year_stats[y]["count"] += 1
                        year_stats[y]["size"] += size

        print(f"  [进度] 已扫描 {page_count} 页")

    # 输出结果
    print(f"\n[+] PDF 文件总数: {total_count:,}")
    print(f"[+] PDF 总大小: {format_size(total_size)} ({total_size:,} bytes)")
    print(f"    💡 免费额度: 10 GB 存储 + 1,000,000 次 Class A 操作/月 (R2 Free Tier)")

    if year_stats:
        print(f"\n  按年份分布:")
        for y in sorted(year_stats.keys()):
            s = year_stats[y]
            print(f"    {y}: {s['count']:,} 个文件, {format_size(s['size'])}")


def query_dynamodb():
    """查询 AWS DynamoDB 的记录数和表大小"""
    print("\n" + "=" * 50)
    print("📊 AWS DynamoDB 统计")
    print("=" * 50)

    aws_key = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    aws_region = os.environ.get("AWS_REGION", "ap-northeast-1")

    if not aws_key or not aws_secret:
        print("[-] AWS 凭证未配置，跳过")
        return

    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client(
        "dynamodb",
        region_name=aws_region,
        aws_access_key_id=aws_key,
        aws_secret_access_key=aws_secret,
    )

    table_name = "fuli_resources"

    # 1. 获取表描述（包含近似计数和表大小）
    print(f"[*] 正在查询 DynamoDB 表: {table_name}")
    try:
        response = client.describe_table(TableName=table_name)
        table = response["Table"]

        item_count = table.get("ItemCount", 0)
        table_size = table.get("TableSizeBytes", 0)
        status = table.get("TableStatus", "N/A")

        print(f"[+] 表状态: {status}")
        print(f"[+] 近似记录数: {item_count:,}")
        print(f"[+] 表大小: {format_size(table_size)} ({table_size:,} bytes)")
        print(f"    💡 免费额度: 25 GB 存储 + 25 RCUs + 25 WCUs (AWS Free Tier)")

        # 2. 精确计数（消耗读取容量，仅当近似计数为 0 或用户需要时）
        if item_count == 0:
            print("[*] 近似计数为 0，正在执行精确计数...")
            try:
                scan_resp = client.scan(TableName=table_name, Select="COUNT")
                exact_count = scan_resp.get("Count", 0)
                print(f"[+] 精确记录数: {exact_count:,}")
            except Exception as e:
                print(f"[-] 精确计数失败: {e}")

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "ResourceNotFoundException":
            print(f"[-] 表 {table_name} 不存在")
        else:
            print(f"[-] 查询失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="统计三个平台的数据量和大小")
    parser.add_argument("--supabase", action="store_true", help="查询 Supabase")
    parser.add_argument("--r2", action="store_true", help="查询 Cloudflare R2")
    parser.add_argument("--dynamodb", action="store_true", help="查询 AWS DynamoDB")
    parser.add_argument("--year", type=str, help="R2 查询指定年份")

    args = parser.parse_args()

    # 如果没有指定任何平台，则查询所有
    if not (args.supabase or args.r2 or args.dynamodb):
        args.supabase = True
        args.r2 = True
        args.dynamodb = True

    print("=" * 50)
    print("📊 多平台数据统计工具")
    print(f"   运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    t0 = time.time()

    if args.supabase:
        query_supabase()

    if args.r2:
        query_r2(year=args.year)

    if args.dynamodb:
        query_dynamodb()

    elapsed = time.time() - t0
    print("\n" + "=" * 50)
    print(f"✅ 统计完成，耗时 {elapsed:.2f} 秒")
    print("=" * 50)


if __name__ == "__main__":
    main()
