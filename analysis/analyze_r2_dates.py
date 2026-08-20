"""
云端 PDF 数量与体积按日期分布分析工具

功能：
  1. 连接 Cloudflare R2 对象存储，检索所有 PDF 对象元数据。
  2. 维度一：统计按【云端上传/修改时间】(LastModified, UTC+8) 的数量与体积分布。
  3. 维度二：统计按【资源发布/文件名日期】(Publish Date) 的数量与体积分布。
  4. 支持导出分析结果为 CSV / JSON 报告。

用法:
    python analysis/analyze_r2_dates.py                  # 完整分析并打印控制台报表
    python analysis/analyze_r2_dates.py --prefix pdfs/2026/  # 指定前缀扫描
    python analysis/analyze_r2_dates.py --export-csv     # 导出结果到 CSV 文件
    python analysis/analyze_r2_dates.py --export-json    # 导出结果到 JSON 文件
"""

import os
import sys
import re
import csv
import json
import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# 确保能加载项目根目录模块
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from sync.dl_or_del_all_r2 import get_r2_client


def fmt_size(sz):
    """格式化字节大小为可读单位"""
    if sz >= 1024 ** 4:
        return f"{sz / (1024 ** 4):.2f} TB"
    elif sz >= 1024 ** 3:
        return f"{sz / (1024 ** 3):.2f} GB"
    elif sz >= 1024 ** 2:
        return f"{sz / (1024 ** 2):.2f} MB"
    elif sz >= 1024:
        return f"{sz / 1024:.2f} KB"
    return f"{sz} B"


def analyze_r2_pdf_dates(prefix="pdfs/", tz_offset_hours=8):
    """
    扫描并分析 R2 存储桶中的 PDF 文件日期分布
    """
    # 强制启用云端模式以读取 R2 配置
    config.set_run_mode("cloud")
    client = get_r2_client()

    paginator = client.get_paginator("list_objects_v2")
    tz = timezone(timedelta(hours=tz_offset_hours))

    upload_date_stats = defaultdict(lambda: {"count": 0, "size": 0})
    upload_month_stats = defaultdict(lambda: {"count": 0, "size": 0})

    publish_date_stats = defaultdict(lambda: {"count": 0, "size": 0})
    publish_month_stats = defaultdict(lambda: {"count": 0, "size": 0})
    publish_year_stats = defaultdict(lambda: {"count": 0, "size": 0})

    folder_stats = defaultdict(lambda: {"count": 0, "size": 0})

    total_count = 0
    total_size = 0

    date_pattern = re.compile(r"(\d{4}[-_]\d{2}[-_]\d{2})")

    print(f"[*] 正在从 Cloudflare R2 ({config.R2_BUCKET_NAME}) 检索前缀 '{prefix}' 的所有 PDF 对象...")

    for page in paginator.paginate(Bucket=config.R2_BUCKET_NAME, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(".pdf"):
                continue

            size = obj.get("Size", 0)
            lm = obj.get("LastModified")

            total_count += 1
            total_size += size

            # 1. 目录结构统计
            parts = key.split("/")
            folder = parts[1] if len(parts) > 1 else "root"
            folder_stats[folder]["count"] += 1
            folder_stats[folder]["size"] += size

            # 2. 云端上传时间 (转为指定时区，默认 UTC+8)
            if lm:
                lm_tz = lm.astimezone(tz)
                up_day = lm_tz.strftime("%Y-%m-%d")
                up_month = lm_tz.strftime("%Y-%m")
                upload_date_stats[up_day]["count"] += 1
                upload_date_stats[up_day]["size"] += size
                upload_month_stats[up_month]["count"] += 1
                upload_month_stats[up_month]["size"] += size

            # 3. 文件名中的资源发布日期
            filename = parts[-1]
            m = date_pattern.search(filename)
            if m:
                pub_date = m.group(1).replace("_", "-")
                pub_month = pub_date[:7]
                pub_year = pub_date[:4]
            else:
                pub_date = "未知日期"
                pub_month = "未知月份"
                pub_year = folder if folder.isdigit() else "未知年份"

            publish_date_stats[pub_date]["count"] += 1
            publish_date_stats[pub_date]["size"] += size
            publish_month_stats[pub_month]["count"] += 1
            publish_month_stats[pub_month]["size"] += size
            publish_year_stats[pub_year]["count"] += 1
            publish_year_stats[pub_year]["size"] += size

    return {
        "total_count": total_count,
        "total_size": total_size,
        "folder_stats": dict(folder_stats),
        "upload_month_stats": dict(upload_month_stats),
        "upload_date_stats": dict(upload_date_stats),
        "publish_year_stats": dict(publish_year_stats),
        "publish_month_stats": dict(publish_month_stats),
        "publish_date_stats": dict(publish_date_stats),
    }


def print_report(results):
    """打印控制台统计报表"""
    total_count = results["total_count"]
    total_size = results["total_size"]

    print("\n" + "=" * 66)
    print("📊 云端 PDF 总体概况")
    print("=" * 66)
    print(f"总文件数: {total_count:,} 个")
    print(f"总存储体积: {fmt_size(total_size)} ({total_size:,} 字节)")
    avg_sz = total_size / total_count if total_count else 0
    print(f"平均单文件大小: {fmt_size(avg_sz)}")
    r2_free_quota = 10 * (1024 ** 3)
    quota_pct = (total_size / r2_free_quota * 100) if r2_free_quota else 0
    print(f"R2 免费额度占用: {quota_pct:.2f}% (共 10 GB 免费额度)")

    print("\n" + "=" * 66)
    print("📁 按 R2 存储目录分布")
    print("=" * 66)
    for f, s in sorted(results["folder_stats"].items()):
        pct = (s["size"] / total_size * 100) if total_size else 0
        cnt_pct = (s["count"] / total_count * 100) if total_count else 0
        print(f"  - 目录 {f:12s}: {s['count']:6,d} 个 ({cnt_pct:5.1f}%) | {fmt_size(s['size']):>10s} ({pct:5.1f}%)")

    print("\n" + "=" * 66)
    print("🕒 维度一：按【云端上传/修改时间】分布 (Upload Date, UTC+8)")
    print("=" * 66)
    print("【按上传月份统计】:")
    for m, s in sorted(results["upload_month_stats"].items()):
        pct = (s["size"] / total_size * 100) if total_size else 0
        cnt_pct = (s["count"] / total_count * 100) if total_count else 0
        print(f"  {m}: {s['count']:6,d} 个文件 ({cnt_pct:5.1f}%) | {fmt_size(s['size']):>10s} ({pct:5.1f}%)")

    print("\n【按上传具体日期明细】:")
    print(f"  {'日期 (Date)':<12} | {'文件数量 (Count)':<14} | {'体积大小 (Size)':<12} | {'体积占比':<8} | {'平均单文件'}")
    print("  " + "-" * 66)
    for d, s in sorted(results["upload_date_stats"].items()):
        pct = (s["size"] / total_size * 100) if total_size else 0
        cnt_pct = (s["count"] / total_count * 100) if total_count else 0
        avg_d = s["size"] / s["count"] if s["count"] else 0
        print(f"  {d:<12} | {s['count']:>6,d} 个 ({cnt_pct:4.1f}%) | {fmt_size(s['size']):>10s} | {pct:>6.2f}% | {fmt_size(avg_d):>10s}")

    print("\n" + "=" * 66)
    print("📅 维度二：按【资源发布/文件名日期】分布 (Publish Date)")
    print("=" * 66)
    print("【按发布年份统计】:")
    for y, s in sorted(results["publish_year_stats"].items()):
        pct = (s["size"] / total_size * 100) if total_size else 0
        print(f"  {y}: {s['count']:6,d} 个文件 | {fmt_size(s['size']):>10s} ({pct:5.1f}%)")

    print("\n【按发布具体日期明细】:")
    print(f"  {'发布日期 (Date)':<14} | {'文件数量 (Count)':<14} | {'体积大小 (Size)':<12} | {'体积占比':<8} | {'平均单文件'}")
    print("  " + "-" * 68)
    for d, s in sorted(results["publish_date_stats"].items()):
        pct = (s["size"] / total_size * 100) if total_size else 0
        cnt_pct = (s["count"] / total_count * 100) if total_count else 0
        avg_d = s["size"] / s["count"] if s["count"] else 0
        print(f"  {d:<14} | {s['count']:>6,d} 个 ({cnt_pct:4.1f}%) | {fmt_size(s['size']):>10s} | {pct:>6.2f}% | {fmt_size(avg_d):>10s}")


def export_to_csv(results, output_dir):
    """导出分析结果到 CSV 文件"""
    os.makedirs(output_dir, exist_ok=True)
    total_size = results["total_size"]
    total_count = results["total_count"]

    # 1. 导出上传日期统计
    upload_csv = os.path.join(output_dir, "r2_upload_date_stats.csv")
    with open(upload_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["上传日期(UTC+8)", "文件数量", "数量占比(%)", "体积大小(字节)", "体积大小(可读)", "体积占比(%)", "平均单文件大小"])
        for d, s in sorted(results["upload_date_stats"].items()):
            cnt_pct = f"{(s['count'] / total_count * 100):.2f}" if total_count else "0"
            sz_pct = f"{(s['size'] / total_size * 100):.2f}" if total_size else "0"
            avg_d = fmt_size(s["size"] / s["count"]) if s["count"] else "0 B"
            writer.writerow([d, s["count"], cnt_pct, s["size"], fmt_size(s["size"]), sz_pct, avg_d])

    # 2. 导出资源发布日期统计
    publish_csv = os.path.join(output_dir, "r2_publish_date_stats.csv")
    with open(publish_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["资源发布日期", "文件数量", "数量占比(%)", "体积大小(字节)", "体积大小(可读)", "体积占比(%)", "平均单文件大小"])
        for d, s in sorted(results["publish_date_stats"].items()):
            cnt_pct = f"{(s['count'] / total_count * 100):.2f}" if total_count else "0"
            sz_pct = f"{(s['size'] / total_size * 100):.2f}" if total_size else "0"
            avg_d = fmt_size(s["size"] / s["count"]) if s["count"] else "0 B"
            writer.writerow([d, s["count"], cnt_pct, s["size"], fmt_size(s["size"]), sz_pct, avg_d])

    print(f"\n[+] CSV 报表已成功导出至:")
    print(f"    - {upload_csv}")
    print(f"    - {publish_csv}")


def export_to_json(results, output_path):
    """导出分析结果到 JSON 文件"""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[+] JSON 数据已成功导出至: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Cloudflare R2 云端 PDF 数量与体积日期分布分析")
    parser.add_argument("--prefix", type=str, default="pdfs/", help="指定 R2 扫描前缀 (默认: pdfs/)")
    parser.add_argument("--export-csv", action="store_true", help="是否导出 CSV 统计表格")
    parser.add_argument("--export-json", action="store_true", help="是否导出 JSON 原始统计数据")
    parser.add_argument("--output-dir", type=str, default=os.path.join(PROJECT_ROOT, "analysis"), help="导出文件存放目录")

    args = parser.parse_args()

    results = analyze_r2_pdf_dates(prefix=args.prefix)
    print_report(results)

    if args.export_csv:
        export_to_csv(results, args.output_dir)

    if args.export_json:
        json_path = os.path.join(args.output_dir, "r2_date_distribution.json")
        export_to_json(results, json_path)


if __name__ == "__main__":
    main()
