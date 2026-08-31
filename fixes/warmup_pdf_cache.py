#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF 预览缩略图全量预热工具 (PDF Thumbnail Cache Warmup)
用于批量/离线并发将数据库中所有 PDF 文件的预览缩略图光栅化并写入 cache/pdf_thumbs，
使得在 Web 界面 (viewer.py) 浏览时 100% 命中缓存，实现 0 毫秒秒开。
"""

import os
import sys
import sqlite3
import hashlib
import time
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import pymupdf

# 确保项目根目录在 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import get_db_path, PDF_BASE_DIR

PDF_THUMB_CACHE_DIR = os.path.join(PROJECT_ROOT, "cache", "pdf_thumbs")


def resolve_pdf_path(raw_path: str) -> str:
    """智能解析 PDF 本地真实路径"""
    if not raw_path:
        return ""
    if os.path.isabs(raw_path) and os.path.exists(raw_path):
        return raw_path
    
    clean_rel = raw_path.replace("\\", "/").lstrip("/")
    candidates = [
        os.path.join(PROJECT_ROOT, clean_rel),
        os.path.join(PDF_BASE_DIR, clean_rel),
        os.path.join(PROJECT_ROOT, "pdf", clean_rel),
        os.path.join(PROJECT_ROOT, "..", "seju", clean_rel),
        os.path.join(PROJECT_ROOT, "..", clean_rel),
    ]
    if clean_rel.startswith("pdf/"):
        stripped = clean_rel[4:]
        candidates.extend([
            os.path.join(PDF_BASE_DIR, stripped),
            os.path.join(PROJECT_ROOT, "pdf", stripped),
            os.path.join(PROJECT_ROOT, "..", "seju", "pdf", stripped),
        ])
    for path in candidates:
        abs_p = os.path.abspath(path)
        if os.path.exists(abs_p) and os.path.isfile(abs_p):
            return abs_p
    return ""


def render_single_pdf(file_path: str, mtime: float, dpi: int = 105, quality: int = 75) -> bool:
    """底层光栅化与持久化磁盘缓存处理函数"""
    try:
        key = hashlib.md5(f"{file_path}_{mtime}_{dpi}_{quality}".encode("utf-8")).hexdigest()
        done_flag = os.path.join(PDF_THUMB_CACHE_DIR, f"{key}_done.flag")
        
        if os.path.exists(done_flag):
            return True

        doc = pymupdf.open(file_path)
        for i, page in enumerate(doc):
            cpath = os.path.join(PDF_THUMB_CACHE_DIR, f"{key}_{i}.jpg")
            pix = page.get_pixmap(dpi=dpi)
            img_bytes = pix.tobytes("jpeg", quality)
            with open(cpath, "wb") as f:
                f.write(img_bytes)
        doc.close()
        
        with open(done_flag, "w", encoding="utf-8") as f:
            f.write("1")
        return True
    except Exception:
        return False


def warmup_all_pdf_cache(max_workers: int = None, dpi: int = 105, quality: int = 75):
    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(f"❌ 找不到数据库文件: {db_path}")
        return

    print(f"📊 正在从数据库读取 PDF 记录...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, pdf_path FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    rows = cursor.fetchall()
    conn.close()

    print(f"📋 共找到 {len(rows)} 条含 PDF 路径的记录。")
    if not rows:
        return

    os.makedirs(PDF_THUMB_CACHE_DIR, exist_ok=True)
    
    valid_tasks = []
    skipped_count = 0
    missing_count = 0

    for item_id, title, raw_path in rows:
        resolved = resolve_pdf_path(raw_path)
        if not resolved or not os.path.exists(resolved):
            missing_count += 1
            continue
        try:
            mtime = os.path.getmtime(resolved)
            key = hashlib.md5(f"{resolved}_{mtime}_{dpi}_{quality}".encode("utf-8")).hexdigest()
            done_flag = os.path.join(PDF_THUMB_CACHE_DIR, f"{key}_done.flag")
            if os.path.exists(done_flag):
                skipped_count += 1
            else:
                valid_tasks.append((resolved, mtime, title))
        except Exception:
            missing_count += 1

    print(f"⚡ 已有缓存 (跳过): {skipped_count} 个")
    print(f"⚠️ 路径失效 (跳过): {missing_count} 个")
    print(f"🚀 待预热生成: {len(valid_tasks)} 个 PDF")

    if not valid_tasks:
        print("✅ 所有有效 PDF 均已完成缓存预热！")
        return

    workers = max_workers or min(16, (os.cpu_count() or 4) * 2)
    print(f"⚙️ 启动 {workers} 个线程并发光栅化处理...")

    t0 = time.perf_counter()
    success_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(render_single_pdf, path, mtime, dpi, quality): title
            for path, mtime, title in valid_tasks
        }
        
        total_tasks = len(future_map)
        completed = 0
        
        for future in as_completed(future_map):
            completed += 1
            try:
                res = future.result()
                if res:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1
                
            if completed % 10 == 0 or completed == total_tasks:
                pct = completed / total_tasks * 100
                sys.stdout.write(f"\r progress: [{completed}/{total_tasks}] ({pct:.1f}%) | 成功: {success_count} | 失败: {fail_count}")
                sys.stdout.flush()

    t1 = time.perf_counter()
    print(f"\n\n🎉 预热完成！总耗时: {t1 - t0:.2f} 秒，平均每个 PDF: {(t1 - t0)/total_tasks*1000:.1f}ms")
    print(f"✅ 成功缓存: {success_count} 个，❌ 失败: {fail_count} 个")


if __name__ == "__main__":
    warmup_all_pdf_cache()
