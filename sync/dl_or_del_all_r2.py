"""
从 Cloudflare R2 下载所有 PDF 到本地文件夹，保持年份目录结构。
下载完成后可选择删除 R2 上的文件（支持批量删除、前缀过滤、模拟运行等）。

不指定任何参数时，会交互式询问要下载还是删除。

用法:
    python download_all_r2.py                              # 交互式选择下载或删除
    python download_all_r2.py --output D:/pdf_backup        # 指定输出目录
    python download_all_r2.py --year 2025                   # 只下载指定年份
    python download_all_r2.py --max 100                     # 只下载前 100 个
    python download_all_r2.py --resume                      # 跳过已存在的文件（断点续传）
    python download_all_r2.py --workers 20                  # 20 个并发（默认 10）
    python download_all_r2.py --delete                      # 下载完后询问是否从 R2 删除已下载的文件
    python download_all_r2.py --delete --delete-force       # 跳过确认直接删除
    python download_all_r2.py --delete --dry-run            # 只列出要删除的文件，不实际删除
    python download_all_r2.py --delete-prefix pdfs/2025/    # 下载并删除指定前缀的文件
    python download_all_r2.py --delete-only                 # 仅执行删除，不下载（配合 --year/--delete-prefix）
    python download_all_r2.py --delete-only --dry-run       # 仅模拟删除预览
    python download_all_r2.py --delete-only --delete-force  # 仅删除，跳过所有确认
"""
import os
import sys
import time
import argparse
import threading
import concurrent.futures
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_ENDPOINT_URL


def get_r2_client():
    """初始化 R2 S3 客户端"""
    if not (R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME and R2_ENDPOINT_URL):
        print("[-] 错误：R2 环境变量未配置完整，请检查 .env 文件")
        print("    需要设置: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_ENDPOINT_URL")
        sys.exit(1)

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(s3={"addressing_style": "path"}),
    )


def list_all_pdfs(client, prefix="pdfs/", max_keys=None):
    """列出 R2 中的所有 PDF 文件"""
    pdfs = []
    paginator = client.get_paginator("list_objects_v2")
    page_count = 0
    last_print = 0

    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
        page_count += 1
        if "Contents" not in page:
            continue
        for obj in page["Contents"]:
            key = obj["Key"]
            if not key.lower().endswith(".pdf"):
                continue
            pdfs.append({
                "key": key,
                "size": obj.get("Size", 0),
                "last_modified": obj.get("LastModified", ""),
            })
        # 每 5000 个或完成时打印进度
        if len(pdfs) - last_print >= 5000 or (max_keys and len(pdfs) >= max_keys):
            print(f"  [进度] 已列出 {len(pdfs)} 个 PDF 文件...")
            last_print = len(pdfs)
        if max_keys and len(pdfs) >= max_keys:
            pdfs = pdfs[:max_keys]
            break

    return pdfs


def format_size(size_bytes):
    """格式化文件大小"""
    if size_bytes > 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    elif size_bytes > 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"


# ========== 线程安全计数器 ==========
_counter_lock = threading.Lock()
_counter = {"success": 0, "skipped": 0, "failed": 0, "total_bytes": 0, "done": 0}


def _make_client():
    """每个线程独立创建 R2 客户端（boto3 客户端不是线程安全的）"""
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(s3={"addressing_style": "path"}),
    )


def _download_one(pdf, output_dir, resume):
    """下载单个文件，供线程池调用"""
    key = pdf["key"]
    relative_path = key[len("pdfs/"):]
    local_path = os.path.join(output_dir, relative_path)
    local_dir = os.path.dirname(local_path)

    # 断点续传
    if resume and os.path.exists(local_path):
        if os.path.getsize(local_path) == pdf["size"]:
            with _counter_lock:
                _counter["skipped"] += 1
                _counter["done"] += 1
            return "skipped", relative_path, 0

    os.makedirs(local_dir, exist_ok=True)

    # 每个线程独立 client
    client = _make_client()
    try:
        client.download_file(R2_BUCKET_NAME, key, local_path)
        with _counter_lock:
            _counter["success"] += 1
            _counter["total_bytes"] += pdf["size"]
            _counter["done"] += 1
        return "ok", relative_path, pdf["size"]
    except ClientError as e:
        with _counter_lock:
            _counter["failed"] += 1
            _counter["done"] += 1
        return "fail", f"{relative_path} ({e})", 0


def _progress_reporter(total, stop_event):
    """后台定时打印进度"""
    while not stop_event.is_set():
        with _counter_lock:
            done = _counter["done"]
            s = _counter["success"]
            sk = _counter["skipped"]
            f = _counter["failed"]
        if done >= total:
            break
        pct = done / total * 100
        print(f"  [{done}/{total}] ✅ {s} | ⏭ {sk} | ❌ {f} ({pct:.0f}%)")
        stop_event.wait(2)  # 每 2 秒报告一次
    # 最终 100% 报告
    with _counter_lock:
        s = _counter["success"]
        sk = _counter["skipped"]
        f = _counter["failed"]
    print(f"  [{total}/{total}] ✅ {s} | ⏭ {sk} | ❌ {f} (100%)")


def download_all_pdfs(output_dir, pdfs, resume=False, workers=10):
    """并发下载所有 PDF 到本地目录"""
    total = len(pdfs)
    if total == 0:
        print("[*] 没有找到 PDF 文件")
        return

    print(f"\n[*] 共找到 {total} 个 PDF 文件，开始下载到: {output_dir}")
    print(f"[*] 并发数: {workers} | 断点续传: {'启用' if resume else '禁用'}\n")

    # 重置计数器
    global _counter
    _counter = {"success": 0, "skipped": 0, "failed": 0, "total_bytes": 0, "done": 0}

    # 启动后台进度报告线程
    stop_event = threading.Event()
    reporter = threading.Thread(target=_progress_reporter, args=(total, stop_event), daemon=True)
    reporter.start()

    # 并发下载
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_download_one, pdf, output_dir, resume) for pdf in pdfs]
        concurrent.futures.wait(futures)

    stop_event.set()
    reporter.join(timeout=1)

    # 汇总
    with _counter_lock:
        s = _counter["success"]
        sk = _counter["skipped"]
        f = _counter["failed"]
        tb = _counter["total_bytes"]

    print(f"\n{'='*50}")
    print(f"下载完成！")
    print(f"  成功: {s}")
    print(f"  跳过: {sk}")
    print(f"  失败: {f}")
    print(f"  总大小: {format_size(tb)}")
    print(f"  保存路径: {output_dir}")
    print(f"{'='*50}")


def list_all_objects(client, prefix=""):
    """列出 R2 桶中所有对象（不限于 PDF）"""
    objects = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
        if "Contents" not in page:
            continue
        for obj in page["Contents"]:
            objects.append({
                "key": obj["Key"],
                "size": obj.get("Size", 0),
                "last_modified": obj.get("LastModified", ""),
            })
    return objects


def delete_all_objects(client, objects, dry_run=False):
    """批量删除所有对象（每次最多 1000 个），支持模拟运行"""
    total = len(objects)
    if total == 0:
        print("[*] 没有找到需要删除的文件")
        return 0, 0

    if dry_run:
        print(f"\n[*] 模拟运行模式，以下文件将被删除：")
        for obj in objects:
            print(f"  {obj['key']}  ({format_size(obj['size'])})")
        return 0, 0

    deleted = 0
    failed = 0
    batch_size = 1000
    start_time = time.time()

    for i in range(0, total, batch_size):
        batch = objects[i:i + batch_size]
        delete_keys = [{"Key": obj["key"]} for obj in batch]

        try:
            resp = client.delete_objects(
                Bucket=R2_BUCKET_NAME,
                Delete={"Objects": delete_keys, "Quiet": True}
            )
            deleted += len(batch) - len(resp.get("Errors", []))
            failed += len(resp.get("Errors", []))
        except Exception as e:
            failed += len(batch)
            print(f"  [-] 批次删除失败: {e}")

        pct = min(i + batch_size, total) / total * 100
        elapsed = time.time() - start_time
        rate = (i + batch_size) / elapsed if elapsed > 0 else 0
        eta = (total - (i + batch_size)) / rate if rate > 0 else 0
        print(f"  [{min(i + batch_size, total)}/{total}] 🗑️ 已删 {deleted} | ❌ 失败 {failed} "
              f"({pct:.0f}%) | 速度: {rate:.0f} 个/秒 | 预计剩余: {eta:.0f}秒")

    return deleted, failed


def run_delete_flow(client, output_dir, delete_candidates, del_prefix, args, show_details=True, extra_search_dirs=None):
    """独立执行删除流程：检查本地文件 → 自动删除有副本的 → 询问删除无副本的
    extra_search_dirs: 额外的本地搜索目录列表（如爬虫保存 PDF 的目录），用于检测本地副本"""
    total_size = sum(obj["size"] for obj in delete_candidates)

    print(f"\n{'='*50}")
    print(f"找到 {len(delete_candidates)} 个文件")
    print(f"总大小: {format_size(total_size)}")
    print(f"{'='*50}")

    # 模拟运行
    if args.dry_run:
        print(f"\n[*] 模拟运行：将删除前缀 '{del_prefix}' 下的文件")
        deleted, failed = delete_all_objects(client, delete_candidates, dry_run=True)
        print(f"\n[*] 模拟运行完成，共 {len(delete_candidates)} 个文件将被删除")
        return

    # ========== 检查本地文件：已有本地副本的自动删除 ==========
    local_have = []   # 本地已存在 → 直接删除
    local_missing = []  # 本地不存在 → 汇总后询问
    search_dirs = [output_dir]
    if extra_search_dirs:
        search_dirs.extend(extra_search_dirs)
    for obj in delete_candidates:
        key = obj["key"]
        # 构造本地路径：去掉 "pdfs/" 前缀后拼接到各搜索目录
        if key.startswith("pdfs/"):
            relative_path = key[len("pdfs/"):]
        else:
            relative_path = key
        # 在任意搜索目录中找到副本即视为本地存在
        found_local = False
        for sd in search_dirs:
            local_path = os.path.join(sd, relative_path)
            if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                found_local = True
                break
        if found_local:
            local_have.append(obj)
        else:
            local_missing.append(obj)

    # 自动删除本地已有的文件
    if local_have:
        print(f"\n[*] 检测到 {len(local_have)} 个文件在本地已有副本，自动从 R2 删除...")
        deleted_ok, deleted_fail = delete_all_objects(client, local_have)
        if deleted_ok > 0:
            print(f"  ✅ 已自动删除 {deleted_ok} 个本地已有副本的文件")
        if deleted_fail > 0:
            print(f"  ❌ {deleted_fail} 个文件删除失败")
    else:
        print(f"\n[*] 未检测到本地已有副本的文件")
        deleted_ok = 0
        deleted_fail = 0

    # 汇总本地没有的文件，询问用户是否删除
    if local_missing:
        missing_size = sum(obj["size"] for obj in local_missing)
        print(f"\n{'='*50}")
        print(f"以下 {len(local_missing)} 个文件在本地没有副本（总计 {format_size(missing_size)}）：")
        if show_details:
            for obj in local_missing:
                local_part = obj["key"]
                if obj["key"].startswith("pdfs/"):
                    local_part = obj["key"][len("pdfs/"):]
                print(f"  📄 {local_part}  ({format_size(obj['size'])})")
        else:
            print(f"  (文件列表已隐藏，可通过回答 Y 查看详情)")
        print(f"{'='*50}")

        # 确认（--delete-force 可跳过）
        should_delete_missing = False
        if args.delete_force:
            should_delete_missing = True
        else:
            try:
                answer = input(f"\n❓ 本地没有上述 {len(local_missing)} 个文件的副本，是否仍从 R2 删除？(y/N): ").strip().lower()
                if answer in ("y", "yes"):
                    should_delete_missing = True
            except (EOFError, KeyboardInterrupt):
                print("\n[*] 已取消")

        if should_delete_missing:
            print(f"\n[*] 开始从 R2 删除 {len(local_missing)} 个本地无副本的文件...")
            deleted_missing_ok, deleted_missing_fail = delete_all_objects(client, local_missing)
            deleted_ok += deleted_missing_ok
            deleted_fail += deleted_missing_fail
        else:
            print(f"[*] 已跳过 {len(local_missing)} 个本地无副本的文件")
    else:
        print(f"[*] 所有文件在本地均有副本，无需额外确认")

    # 最终汇总
    print(f"\n{'='*50}")
    print(f"删除操作完成！")
    print(f"  成功: {deleted_ok}")
    print(f"  失败: {deleted_fail}")
    if deleted_fail > 0:
        print(f"  ⚠️  有 {deleted_fail} 个文件删除失败，请重试")
    print(f"{'='*50}")


def _confirm_listing(prefix, args):
    """询问用户是否显示详细文件列表（仅控制显示，不影响实际检索）。返回 True 要显示，False 跳过显示。"""
    if args.delete_force:
        return True
    try:
        hint = f"前缀 '{prefix}' 下文件数量可能很大"
        answer = input(f"\n❓ {hint}，是否显示文件详细列表？(Y/n): ").strip().lower()
        return answer not in ("n", "no")
    except (EOFError, KeyboardInterrupt):
        return False


def main():
    parser = argparse.ArgumentParser(description="从 Cloudflare R2 下载所有 PDF 到本地")
    parser.add_argument("--output", "-o", default=None,
                        help="本地输出目录 (默认: 当前目录下的 r2_pdfs)")
    parser.add_argument("--year", "-y", default=None,
                        help="只下载指定年份 (如 2025)")
    parser.add_argument("--max", "-m", type=int, default=None,
                        help="最多下载的文件数")
    parser.add_argument("--resume", "-r", action="store_true",
                        help="启用断点续传（跳过已存在的文件）")
    parser.add_argument("--workers", "-w", type=int, default=10,
                        help="并发下载数 (默认 10)")
    parser.add_argument("--delete", "-d", action="store_true",
                        help="下载完成后询问是否从 R2 删除已下载的文件")
    parser.add_argument("--delete-force", "-df", action="store_true",
                        help="删除时跳过确认提示，直接删除（需同时使用 --delete）")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="只列出要删除的文件，不实际删除（需同时使用 --delete）")
    parser.add_argument("--delete-prefix", default=None,
                        help="删除时仅删除指定前缀的文件（如 pdfs/2025/），默认为本次下载的前缀（需同时使用 --delete）")
    parser.add_argument("--delete-only", action="store_true",
                        help="仅执行删除操作，不下载（可配合 --year/--delete-prefix/--dry-run/--delete-force 使用）")
    args = parser.parse_args()

    # 确定输出目录
    if args.output:
        output_dir = args.output
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(script_dir, "r2_pdfs")

    # 爬虫保存 PDF 的目录（项目根目录下的 pdf/），用于检测本地副本
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)  # sync/ 的上一级
    pdf_dir = os.path.join(project_root, "pdf")

    # ========== 交互模式：没有指定任何操作时，询问用户 ==========
    if not args.delete_only and not args.delete:
        try:
            print(f"\n{'='*50}")
            print("请选择操作：")
            print("  1. 下载 PDF（默认）")
            print("  2. 删除 R2 文件")
            print(f"{'='*50}")
            choice = input("请输入选项 (1/2)，回车默认下载: ").strip()
            if choice == "2":
                args.delete_only = True
            # 否则保持默认下载
        except (EOFError, KeyboardInterrupt):
            print("\n[*] 已取消")
            return

    print(f"[*] 初始化 R2 客户端...")
    client = get_r2_client()

    # 构建前缀
    prefix = "pdfs/"
    if args.year:
        prefix = f"pdfs/{args.year}/"
        print(f"[*] 仅操作 {args.year} 年的文件")

    # ========== 仅删除模式（跳过下载） ==========
    if args.delete_only:
        del_prefix = args.delete_prefix if args.delete_prefix else prefix
        print(f"[*] 仅删除模式，前缀: {del_prefix}")

        # 询问是否详细列出文件名（仅影响显示，不影响实际检索）
        show_details = _confirm_listing(del_prefix, args)

        print(f"[*] 正在列出 R2 中的文件...（无论是否显示，都会检索文件列表）")
        delete_candidates = list_all_objects(client, prefix=del_prefix)
        print(f"[*] 找到 {len(delete_candidates)} 个文件")

        run_delete_flow(client, output_dir, delete_candidates, del_prefix, args,
                        show_details=show_details, extra_search_dirs=[pdf_dir])
        return

    # ========== 下载模式 ==========
    show_details = _confirm_listing(prefix, args)
    if not show_details:
        print("[*] 跳过文件详细列表显示，但仍在检索文件清单...")
    print(f"[*] 正在列出 R2 中的 PDF 文件 (前缀: {prefix})...")
    pdfs = list_all_pdfs(client, prefix=prefix, max_keys=args.max)
    print(f"[*] 找到 {len(pdfs)} 个 PDF 文件")

    download_all_pdfs(output_dir, pdfs, resume=args.resume, workers=args.workers)

    # ========== 下载完成后删除 R2 文件 ==========
    if args.delete and len(pdfs) > 0:
        # 确定要删除的前缀：优先使用 --delete-prefix，否则使用下载时的前缀
        del_prefix = args.delete_prefix if args.delete_prefix else prefix

        # 如果指定了 --delete-prefix，需要重新列出该前缀下的所有文件（不限于 PDF）
        if args.delete_prefix:
            print(f"[*] 正在列出前缀 '{del_prefix}' 下的所有文件...")
            delete_candidates = list_all_objects(client, prefix=del_prefix)
        else:
            delete_candidates = pdfs

        run_delete_flow(client, output_dir, delete_candidates, del_prefix, args,
                        show_details=show_details, extra_search_dirs=[pdf_dir])


if __name__ == "__main__":
    main()