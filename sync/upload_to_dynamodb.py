"""
对比本地 SQLite 和 AWS DynamoDB 的 url 及磁力链接，
以本地数据为基准，上传云端缺失的数据
"""
import os
import sqlite3
import time
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# 加载本地 .env 环境变量
load_dotenv()

# ========== AWS 配置（从环境变量读取）==========
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
AWS_REGION = "ap-northeast-1"
TABLE_NAME = "fuli_resources"

if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
    raise ValueError(
        "AWS 密钥未配置！请确保 .env 文件中设置了 AWS_ACCESS_KEY_ID 和 AWS_SECRET_ACCESS_KEY，"
        "并已在运行前执行: set -a && source .env && set +a"
    )

# ========== 本地数据库路径（复用爬虫的数据库配置）==========
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_db_path
DB_PATH = get_db_path()


def create_table(table_name, key_name, key_type="S"):
    """创建 DynamoDB 表（如果不存在）"""
    dynamodb = boto3.client(
        "dynamodb",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )

    try:
        # 检查表是否已存在
        existing_tables = dynamodb.list_tables()["TableNames"]
        if table_name in existing_tables:
            print(f"[*] 表 {table_name} 已存在，跳过创建")
            return

        dynamodb.create_table(
            TableName=table_name,
            AttributeDefinitions=[{"AttributeName": key_name, "AttributeType": key_type}],
            KeySchema=[{"AttributeName": key_name, "KeyType": "HASH"}],

            # 1. 设置为预置容量模式
            BillingMode="PROVISIONED",

            # 2. 直接设置为免费额度允许的最大值：25
            ProvisionedThroughput={
                "ReadCapacityUnits": 25,   # 免费额度：每秒 25 次读取
                "WriteCapacityUnits": 25   # 免费额度：每秒 25 次写入
            }
        )
        # 等待表激活
        waiter = dynamodb.get_waiter("table_exists")
        waiter.wait(TableName=table_name)
        print(f"[+] 表 {table_name} 创建成功！")
    except ClientError as e:
        print(f"[-] 创建表失败: {e}")
        raise


def create_tables():
    """创建 DynamoDB 表（如果不存在）"""
    print("[*] 检查/创建 DynamoDB 表...")
    create_table(TABLE_NAME, "url")


def get_cloud_keys(dynamodb_client, table_name, key_name):
    """全量扫描 DynamoDB，返回云端所有指定 key 的集合"""
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

        response = dynamodb_client.scan(**kwargs)
        for item in response.get("Items", []):
            key_val = item.get(key_name, {})
            # client.scan() 返回 DynamoDB JSON 格式: {"S": "http://..."}
            if "S" in key_val and key_val["S"]:
                cloud_keys.add(key_val["S"])

        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break

    return cloud_keys


def upload_items(table_name, key_name, local_items, label):
    """
    对比本地 SQLite 和云端 DynamoDB 的 key 集合，
    以本地数据为基准，向云端上传缺失的数据
    """
    local_keys = set(local_items.keys())

    dynamodb_client = boto3.client(
        "dynamodb",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )

    print(f"[+] 本地共有 {len(local_keys)} 条 {label}")

    if len(local_keys) == 0:
        print(f"[✓] 本地无 {label} 需要处理")
        return

    # ====== 读取云端所有 key ======
    print(f"[*] 正在扫描 DynamoDB 获取云端 {label} 集合...")
    cloud_keys = get_cloud_keys(dynamodb_client, table_name, key_name)
    print(f"[+] 云端共有 {len(cloud_keys)} 条 {label}")

    # ====== 取差集 ======
    missing_keys = local_keys - cloud_keys
    missing_list = sorted(missing_keys)
    total = len(missing_list)
    print(f"[+] 本地有但云端缺失的 {label}: {total} 条")

    if total == 0:
        print(f"[✓] 本地与云端完全一致，无需上传！")
        return

    # ====== 批量上传缺失的数据 ======
    BATCH_SIZE = 25
    inserted = 0
    skipped = 0
    batch = []
    batch_keys = []

    for i, key in enumerate(missing_list, 1):
        item = {key_name: {"S": key}}
        # 如果有磁力链接，一并上传
        magnet = local_items.get(key, "")
        if magnet:
            item["resource_link"] = {"S": magnet}
        batch.append({"PutRequest": {"Item": item}})
        batch_keys.append(key)
        if len(batch) == BATCH_SIZE or i == total:
            try:
                response = dynamodb_client.batch_write_item(
                    RequestItems={table_name: batch}
                )
                unprocessed = response.get("UnprocessedItems", {}).get(table_name, [])

                # 重试机制
                retry_count = 0
                while unprocessed and retry_count < 3:
                    time.sleep(2 ** retry_count * 0.5)  # 退避重试
                    response = dynamodb_client.batch_write_item(
                        RequestItems={table_name: unprocessed}
                    )
                    unprocessed = response.get("UnprocessedItems", {}).get(table_name, [])
                    retry_count += 1

                # 找出最终未写入成功的 key
                unprocessed_keys = set()
                if unprocessed:
                    for item in unprocessed:
                        unprocessed_key_s = item.get("PutRequest", {}).get("Item", {}).get(key_name, {})
                        unprocessed_key = unprocessed_key_s.get("S")
                        if unprocessed_key:
                            unprocessed_keys.add(unprocessed_key)
                    skipped += len(unprocessed_keys)

                # 最终成功写入的 key
                success_keys = [k for k in batch_keys if k not in unprocessed_keys]
                inserted += len(success_keys)

                print(f"[*] 进度 ({label}): 已上传 {inserted}/{total} 条" + (f" (本批失败: {len(unprocessed_keys)} 条)" if unprocessed_keys else ""))
            except ClientError as e:
                print(f"[-] 写入批次失败: {e}")
                skipped += len(batch)
            batch = []
            batch_keys = []

    print(f"\n[✓] {label} 上传完成！")
    print(f"[✓] 本次成功写入: {inserted} 条")
    if skipped:
        print(f"[!] 本次跳过/失败: {skipped} 条")


def upload_resources():
    """
    从本地 SQLite 读取 url 和磁力链接，上传到云端 DynamoDB
    以 url 为主键，resource_link 作为额外属性
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 读取本地所有 url 及对应的磁力链接
    cursor.execute(
        "SELECT url, resource_link FROM resources WHERE url IS NOT NULL AND url != ''"
    )
    local_rows = cursor.fetchall()
    local_items = {row[0]: row[1] if row[1] else "" for row in local_rows}
    conn.close()

    upload_items(TABLE_NAME, "url", local_items, "资源")


if __name__ == "__main__":

    print("=" * 50)
    print("DynamoDB 同步工具（本地 → 云端）")
    print("=" * 50)

    # Step 1: 创建表
    print("\n[Step 1/2] 创建 DynamoDB 表...")
    if not os.path.exists(DB_PATH):
        print(f"[-] 数据库不存在: {DB_PATH}")
        print("[-] 请先运行 export_urls.py 导出数据")
        exit(1)
    create_tables()

    # Step 2: 上传资源（url + 磁力链接）
    print("\n[Step 2/2] 对比并上传缺失的资源...")
    upload_resources()

    print("\n[✓] 全部完成！")