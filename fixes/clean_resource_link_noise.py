"""
清理数据库中 resource_link 残留的标签行与说明行

清理内容（与 utils/resource_link_cleaner.py 保持一致）：
1. 首部纯标签行：如 "115 ed2k:"、"磁力："、"磁力资源："、"ed2k:" 等
2. 尾部说明/推广行：如 "ed2k请用115保存，迅雷等支持ed2k的客户端会失败"、"更多xx资源，尽在 https://..." 等
3. 兼容旧逻辑：尾部若仍非资源行，去掉一行

用法：
    python fixes/clean_resource_link_noise.py                          # 预览（不修改数据）
    python fixes/clean_resource_link_noise.py --run                    # 正式执行（更新本地 SQLite）
    python fixes/clean_resource_link_noise.py --run --sync-supabase    # 同时同步更新云端 Supabase
    python fixes/clean_resource_link_noise.py --run --sync-dynamodb    # 同时同步更新 AWS DynamoDB
"""
import argparse
import os
import shutil
import sqlite3
from datetime import datetime

from fixes.db_utils import setup_fixes_module, get_connection

setup_fixes_module()

from config import get_db_path, SUPABASE_URL, SUPABASE_KEY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION  # noqa: E402
from utils import setup_console_utf8  # noqa: E402
from utils.resource_link_cleaner import clean_resource_link  # noqa: E402

DYNAMODB_TABLE = "fuli_resources"


def backup_local_db(db_path):
    """正式执行前备份本地 SQLite 数据库"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_{timestamp}"
    try:
        shutil.copy2(db_path, backup_path)
        print(f"[+] 本地数据库备份成功: {backup_path}")
    except Exception as e:
        print(f"[-] 备份本地数据库失败，终止执行: {e}")
        raise


def find_dirty_records(conn):
    """扫描全部记录，返回需要清洗的 (id, url, old_link, new_link) 列表"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, url, resource_link FROM resources "
        "WHERE resource_link IS NOT NULL AND resource_link != ''"
    )
    rows = cursor.fetchall()

    changes = []
    for row_id, url, old_link in rows:
        new_link = clean_resource_link(old_link)
        if new_link != old_link:
            changes.append((row_id, url, old_link, new_link))
    return changes


def sync_supabase(changes):
    """将清洗结果同步到云端 Supabase（以 url 为匹配键）"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[-] 未配置 SUPABASE_URL / SUPABASE_KEY，跳过 Supabase 同步。")
        return

    from urllib.parse import urlparse
    from supabase import create_client

    parsed = urlparse(SUPABASE_URL.strip())
    clean_url = f"{parsed.scheme}://{parsed.netloc}"
    client = create_client(clean_url, SUPABASE_KEY.strip())

    success = 0
    failed = 0
    for idx, (row_id, url, old_link, new_link) in enumerate(changes, 1):
        if not url:
            continue
        try:
            client.table("resources").update(
                {"resource_link": new_link}
            ).eq("url", url).execute()
            success += 1
        except Exception as e:
            failed += 1
            print("[-] Supabase 更新失败 (url=%s...): %s", url[:60], e)
        if idx % 200 == 0:
            print(f"[*] Supabase 进度: {idx}/{len(changes)} (成功 {success}, 失败 {failed})")
    print(f"[+] Supabase 同步完成: 成功 {success} 条, 失败 {failed} 条")


def sync_dynamodb(changes):
    """将清洗结果同步到 AWS DynamoDB（仅更新已存在的 url 项，避免误建新项）"""
    if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
        print("[-] 未配置 AWS 凭证，跳过 DynamoDB 同步。")
        return

    import boto3
    from botocore.exceptions import ClientError

    dynamodb = boto3.client(
        "dynamodb",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )

    success = 0
    failed = 0
    skipped = 0
    for idx, (row_id, url, old_link, new_link) in enumerate(changes, 1):
        if not url:
            continue
        try:
            dynamodb.update_item(
                TableName=DYNAMODB_TABLE,
                Key={"url": {"S": url}},
                UpdateExpression="SET resource_link = :rl",
                ExpressionAttributeValues={":rl": {"S": new_link}},
                ConditionExpression="attribute_exists(url)",
            )
            success += 1
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                skipped += 1  # 云端无此 url，无需更新
            else:
                failed += 1
                print(f"[-] DynamoDB 更新失败 (url={url[:60]}...): {e}")
        except Exception as e:
            failed += 1
            print(f"[-] DynamoDB 更新失败 (url={url[:60]}...): {e}")
        if idx % 500 == 0:
            print(f"[*] DynamoDB 进度: {idx}/{len(changes)} (成功 {success}, 失败 {failed})")
    print(f"[+] DynamoDB 同步完成: 成功 {success} 条, 跳过 {skipped} 条, 失败 {failed} 条")


def main():
    setup_console_utf8()
    parser = argparse.ArgumentParser(
        description="清理数据库中 resource_link 残留的标签行与说明行。"
    )
    parser.add_argument(
        "--run",
        action="store_true",
        default=False,
        help="正式执行写入，不加此参数时仅进行预览 (Dry Run)。"
    )
    parser.add_argument(
        "--sync-supabase",
        action="store_true",
        default=False,
        help="写入本地 SQLite 后，同步更新云端 Supabase（需要 --run）。"
    )
    parser.add_argument(
        "--sync-dynamodb",
        action="store_true",
        default=False,
        help="写入本地 SQLite 后，同步更新 AWS DynamoDB（需要 --run）。"
    )
    args = parser.parse_args()

    db_path = get_db_path()
    print("=" * 60)
    print(f"[*] 运行模式: {'【正式执行模式】' if args.run else '【预览模式 (Dry Run)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        return

    conn = get_connection(db_path)
    try:
        changes = find_dirty_records(conn)
    except Exception as e:
        print(f"[-] 扫描数据库失败: {e}")
        conn.close()
        return

    print(f"[*] 需要清洗的记录数: {len(changes)}")
    if not changes:
        print("[+] 没有需要清洗的记录。")
        conn.close()
        return

    # 展示部分样例
    print("=" * 60)
    print("[*] 样例预览（最多 8 条）:")
    for row_id, url, old_link, new_link in changes[:8]:
        print(f"  ID={row_id}  url={url}")
        print(f"    BEFORE: {repr(old_link[:120])}")
        print(f"    AFTER : {repr(new_link[:120])}")
    print("=" * 60)

    if not args.run:
        print("[*] 当前为预览模式，未执行任何写入操作。")
        print("[*] 确认无误后请运行: python fixes/clean_resource_link_noise.py --run")
        conn.close()
        return

    # 正式写入前先备份数据库
    try:
        backup_local_db(db_path)
    except Exception:
        conn.close()
        return

    # 正式写入本地 SQLite
    cursor = conn.cursor()
    cursor.executemany(
        "UPDATE resources SET resource_link = ? WHERE id = ?",
        [(new_link, row_id) for row_id, url, old_link, new_link in changes],
    )
    conn.commit()
    print(f"[+] 本地 SQLite 更新完成: {len(changes)} 条记录。")

    if args.sync_supabase:
        sync_supabase(changes)
    if args.sync_dynamodb:
        sync_dynamodb(changes)

    conn.close()
    print("[+] 全部完成！")


if __name__ == "__main__":
    main()