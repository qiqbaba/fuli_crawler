# -*- coding: utf-8 -*-
"""后台任务子进程入口。

用法：python -m bridge.worker <tool_id> <json_params>
父进程直接捕获本进程的 stdout/stderr 作为任务日志，因此无需劫持全局
sys.stdout 或挂载 logging handler，也不会污染 Web 服务进程。
"""
from __future__ import annotations

import builtins
import importlib
import json
import os
import sys
from typing import Any, Dict

from .registry import TOOLS, normalize_params

# 现有 CLI 中存在 input() 交互；后台任务一律默认拒绝确认，避免挂死或误执行
builtins.input = lambda *args, **kwargs: "n"


def _system_health(params: Dict[str, Any]) -> None:
    import sqlite3

    import config
    from config import PDF_BASE_DIR, get_db_path, use_r2, use_supabase

    db_path = get_db_path()
    print("[系统自检] 开始检查本地工作台运行环境")
    print(f"数据库路径: {db_path}")
    print(f"数据库存在: {os.path.exists(db_path)}")
    if os.path.exists(db_path):
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        print(f"资源记录数: {count:,}")
    print(f"PDF 根目录: {PDF_BASE_DIR}")
    print(f"PDF 目录存在: {os.path.isdir(PDF_BASE_DIR)}")
    if os.path.isdir(PDF_BASE_DIR):
        try:
            entries = len(os.listdir(PDF_BASE_DIR))
            print(f"PDF 根目录一级条目: {entries:,}")
        except OSError as exc:
            print(f"PDF 目录列举失败: {exc}")
    print(f"Supabase 凭据: {'已配置' if config.SUPABASE_URL and config.SUPABASE_KEY else '未配置'}")
    print(f"R2 凭据: {'已配置' if config.R2_ACCESS_KEY_ID and config.R2_SECRET_ACCESS_KEY and config.R2_BUCKET_NAME else '未配置'}")
    print(f"DynamoDB 凭据: {'已配置' if config.AWS_ACCESS_KEY_ID and config.AWS_SECRET_ACCESS_KEY else '未配置'}")
    print(f"爬虫云端模式 (use_supabase / use_r2): {use_supabase()} / {use_r2()}")
    print("[系统自检] 注: 本地模式下 use_* 为 False 属预期，不影响云端工具显式连接")
    print("[系统自检] 检查完成")


def _channel_probe(params: Dict[str, Any]) -> None:
    """流式输出日志，用于验证 SSE 推送、取消与并发上限。"""
    import time

    lines = int(params.get("lines", 20))
    interval = int(params.get("interval_ms", 500)) / 1000.0
    print(f"[通道压测] 计划输出 {lines} 行，间隔 {interval:.2f}s", flush=True)
    for index in range(1, lines + 1):
        print(f"[通道压测] {index}/{lines}", flush=True)
        time.sleep(interval)
    print("[通道压测] 完成", flush=True)


def _db_backup(params: Dict[str, Any]) -> None:
    from config import get_db_path
    from fixes.db_utils import backup_db

    tag = str(params.get("tag") or "web").replace(" ", "_")
    print(f"[备份] 开始备份数据库，标签: {tag}")
    path = backup_db(get_db_path(), prefix_tag=tag)
    print(f"[备份] 完成: {path}")


def _db_vacuum(params: Dict[str, Any]) -> None:
    import sqlite3

    from config import get_db_path
    from fixes.db_utils import vacuum_db

    db_path = get_db_path()
    before = os.path.getsize(db_path)
    print(f"[VACUUM] 开始压缩，当前大小: {before / 1024 / 1024:.1f} MB")
    with sqlite3.connect(db_path) as conn:
        vacuum_db(conn)
    after = os.path.getsize(db_path)
    print(f"[VACUUM] 完成，压缩后: {after / 1024 / 1024:.1f} MB")


def _warmup_cache(params: Dict[str, Any]) -> None:
    import os
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from config import get_db_path
    from fixes.warmup_pdf_cache import (
        PDF_THUMB_CACHE_DIR,
        render_single_pdf,
        warmup_all_pdf_cache,
    )

    from .db import resolve_pdf_path

    limit = int(params.get("limit", 0))
    workers = int(params.get("workers", 8))
    dpi = int(params.get("dpi", 105))
    quality = int(params.get("quality", 75))
    os.makedirs(PDF_THUMB_CACHE_DIR, exist_ok=True)

    with sqlite3.connect(get_db_path()) as conn:
        total_pdf_records = conn.execute(
            "SELECT COUNT(*) FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''"
        ).fetchone()[0]

    cached_units = sum(1 for name in os.listdir(PDF_THUMB_CACHE_DIR) if name.endswith("_done.flag"))
    print(f"[缓存预热] 含 PDF 记录 {total_pdf_records:,} 条，已有缓存 {cached_units:,} 个", flush=True)

    if limit == 0:
        print("[缓存预热] 全量模式：扫描全部含 PDF 的记录，已缓存的文档自动跳过", flush=True)
        warmup_all_pdf_cache(max_workers=workers, dpi=dpi, quality=quality)
    else:
        sql = "SELECT pdf_path FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''"
        with sqlite3.connect(get_db_path()) as conn:
            rows = [r[0] for r in conn.execute(sql + " LIMIT ?", (limit,))]
        paths = [path for path in (resolve_pdf_path(raw) for raw in rows) if path]
        print(f"[缓存预热] 读取 {len(rows)} 条记录，命中本地 PDF {len(paths)} 个", flush=True)

        success = 0
        failed = 0
        completed = 0
        total = len(paths)

        def work(path: str) -> bool:
            try:
                return render_single_pdf(path, os.path.getmtime(path), dpi, quality)
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, path) for path in paths]
            for future in as_completed(futures):
                completed += 1
                if future.result():
                    success += 1
                else:
                    failed += 1
                if completed % 5 == 0 or completed == total:
                    pct = completed / total * 100 if total else 100.0
                    print(
                        f"[缓存预热] 进度 {completed}/{total} ({pct:.1f}%) | 成功 {success} | 失败 {failed}",
                        flush=True,
                    )
        print(f"[缓存预热] 成功 {success} / {total}", flush=True)

    cached_after = sum(1 for name in os.listdir(PDF_THUMB_CACHE_DIR) if name.endswith("_done.flag"))
    coverage = cached_after / total_pdf_records * 100 if total_pdf_records else 0.0
    print(
        f"[缓存预热] 完成，缓存单元 {cached_after:,} / 记录 {total_pdf_records:,}，覆盖率 {coverage:.1f}%",
        flush=True,
    )


def _orphan_manage(params: Dict[str, Any]) -> None:
    """孤立 PDF 隔离 / 归位：把底层终端 y/N 交互前移为工具参数。"""
    import os
    from types import SimpleNamespace

    from config import PDF_BASE_DIR, get_db_path
    from fixes import pdf_maintenance

    mode = str(params.get("mode", "隔离到根目录"))
    run = bool(params.get("run"))
    is_restore = mode.startswith("归位") or mode == "restore"
    db_path = get_db_path()
    target = pdf_maintenance._restore_orphans_from_root if is_restore else pdf_maintenance._move_orphans_to_root
    action = "归位到年份目录" if is_restore else "隔离到根目录"

    print(f"[孤立PDF] 模式: {action} | {'正式执行' if run else '仅预览 (Dry Run)'}", flush=True)
    original_input = builtins.input
    builtins.input = lambda *args, **kwargs: ("y" if run else "n")
    try:
        target(db_path, os.path.abspath(PDF_BASE_DIR), SimpleNamespace(run=run, db=db_path))
    finally:
        builtins.input = original_input


def _export_links(params: Dict[str, Any]) -> None:
    import os
    import sqlite3
    from datetime import datetime

    from config import PROJECT_ROOT, get_db_path

    src = str(params.get("src_path") or "").strip() or get_db_path()
    dst = str(params.get("dst_path") or "").strip() or os.path.join(
        PROJECT_ROOT, "cache", "urls_only.db"
    )
    if not os.path.isfile(src):
        print(f"[导出] 源数据库不存在或不是文件: {src}")
        return
    if not dst.lower().endswith(".db"):
        print("[导出] 目标路径必须以 .db 结尾")
        return
    if os.path.abspath(src) == os.path.abspath(dst):
        print("[导出] 源库与目标库不能是同一文件，已拒绝执行")
        return
    print(f"[导出] 源库: {src}", flush=True)
    overwrite = bool(params.get("overwrite"))
    if os.path.exists(dst):
        if not overwrite:
            print(f"[导出] 目标文件已存在，为安全起见拒绝覆盖: {dst}")
            print("[导出] 如需重建该轻量库，请勾选「允许覆盖已存在的目标文件」")
            return
        try:
            os.remove(dst)
            print(f"[导出] 已按确认删除目标文件: {dst}")
        except OSError as exc:
            print(f"[导出] 无法删除目标文件: {exc}")
            return

    limit = int(params.get("limit", 0))
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    try:
        src_cur = src_conn.cursor()
        dst_cur = dst_conn.cursor()
        url_sql = "SELECT url FROM resources WHERE length(url) > 0"
        mag_sql = "SELECT resource_link FROM resources WHERE length(resource_link) > 0"
        url_rows = src_cur.execute(
            url_sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ()
        ).fetchall()
        mag_rows = src_cur.execute(
            mag_sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ()
        ).fetchall()
        print(f"[导出] 读取 URL {len(url_rows):,} 条、磁力链接 {len(mag_rows):,} 条", flush=True)

        dst_cur.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                exported_at TEXT NOT NULL
            )
        """)
        dst_cur.execute("CREATE INDEX IF NOT EXISTS idx_url ON urls(url)")
        dst_cur.execute("""
            CREATE TABLE IF NOT EXISTS magnets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_link TEXT NOT NULL,
                exported_at TEXT NOT NULL
            )
        """)
        dst_cur.execute("CREATE INDEX IF NOT EXISTS idx_resource_link ON magnets(resource_link)")

        batch = 1000
        for offset in range(0, len(url_rows), batch):
            chunk = url_rows[offset:offset + batch]
            dst_cur.executemany(
                "INSERT INTO urls (url, exported_at) VALUES (?, ?)",
                [(row[0], now) for row in chunk],
            )
        for offset in range(0, len(mag_rows), batch):
            chunk = mag_rows[offset:offset + batch]
            dst_cur.executemany(
                "INSERT INTO magnets (resource_link, exported_at) VALUES (?, ?)",
                [(row[0], now) for row in chunk],
            )
        dst_conn.commit()
        size_mb = os.path.getsize(dst) / 1024 / 1024
        print(f"[导出] 完成: {dst} ({size_mb:.2f} MB)", flush=True)
    finally:
        dst_conn.close()
        src_conn.close()


def _run_cloud_probe(params: Dict[str, Any]) -> None:
    from .cloud_probe import run_probes

    run_probes(params)


RUNNERS = {
    "system_health": _system_health,
    "channel_probe": _channel_probe,
    "db_backup": _db_backup,
    "db_vacuum": _db_vacuum,
    "warmup_cache": _warmup_cache,
    "orphan_manage": _orphan_manage,
    "export_links": _export_links,
    "cloud_probe": _run_cloud_probe,
}


def main() -> int:
    if len(sys.argv) < 2:
        print("[错误] 缺少 tool_id")
        return 2
    tool_id = sys.argv[1]
    tool = TOOLS.get(tool_id)
    if tool is None:
        print(f"[错误] 未注册的工具: {tool_id}")
        return 2
    try:
        raw = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    except json.JSONDecodeError as exc:
        print(f"[错误] 参数不是合法 JSON: {exc}")
        return 2
    params = normalize_params(tool, raw)

    print(f"[任务] 工具: {tool.title} ({tool.id})")
    print(f"[任务] 参数: {json.dumps(params, ensure_ascii=False)}")

    # 危险工具必须由工作台显式携带风险确认，形成页面勾选之外的第二道拦截。
    # 拒绝时返回非零退出码，避免「未执行」被误报为任务成功。
    if tool.danger and raw.get("_ack") is not True:
        print("[任务] 未通过风险确认，已拒绝执行（危险工具需携带 _ack）")
        return 3

    if tool.mode == "module":
        argv = list(tool.command)
        if tool.argv_builder:
            argv += tool.argv_builder(params)
        sys.argv = [tool.module, *argv]
        print(f"[任务] 调用: python -m {tool.module} {' '.join(argv)}")
        module = importlib.import_module(tool.module)
        main_fn = getattr(module, "main", None)
        if main_fn is None:
            print(f"[错误] 模块缺少 main(): {tool.module}")
            return 2
        main_fn()
    else:
        runner = RUNNERS.get(tool.runner)
        if runner is None:
            print(f"[错误] 未实现的 runner: {tool.runner}")
            return 2
        runner(params)
    return 0


if __name__ == "__main__":
    sys.exit(main())
