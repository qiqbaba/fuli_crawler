# -*- coding: utf-8 -*-
"""云端服务只读连通性自检。

设计约束：
- 只做最轻量的只读调用（1 行查询 / 1 个对象 / 表列表），绝不写入云端数据；
- 任何输出都不得包含凭据、完整 endpoint 或异常里的 token，统一经过 _safe() 脱敏；
- 直接读取 config 常量，不经过 use_supabase()/use_r2()，避免被 is_local_mode() 屏蔽，
  也不调用 set_run_mode('cloud')，因此不会影响爬虫的运行模式语义。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Dict, List, Tuple

import config

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\.]{24,}")


def _safe(text: Any) -> str:
    """脱敏：URL 打码、长串 token 打码、截断长度。"""
    cleaned = _URL_RE.sub("<endpoint>", str(text))
    cleaned = _TOKEN_RE.sub("<redacted>", cleaned)
    return cleaned[:240]


def _example_bucket() -> str:
    """未配置 bucket 时，从模板里取候选值用于只读探测（不写回 .env）。"""
    path = os.path.join(config.PROJECT_ROOT, ".env.example")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("R2_BUCKET_NAME="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _timed(func: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        result = func()
    except Exception as exc:
        return {"ok": False, "error": _safe(exc)}
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def probe_supabase() -> Dict[str, Any]:
    url = (config.SUPABASE_URL or "").strip()
    key = (config.SUPABASE_KEY or "").strip()
    if not url or not key:
        return {"ok": False, "reason": "SUPABASE_URL / SUPABASE_KEY 未配置"}

    def call() -> Dict[str, Any]:
        from urllib.parse import urlparse

        from supabase import create_client

        parsed = urlparse(url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}"
        client = create_client(clean_url, key)
        response = client.table("resources").select("id", count="exact").limit(1).execute()
        return {"ok": True, "record_count": response.count}

    return _timed(call)


def probe_r2(bucket_override: str = "") -> Dict[str, Any]:
    missing = [
        name for name in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL")
        if not getattr(config, name, "")
    ]
    if missing:
        return {"ok": False, "reason": f"缺少 {', '.join(missing)}"}

    bucket = (bucket_override or "").strip() or (config.R2_BUCKET_NAME or "").strip()
    source = "参数覆盖" if bucket_override.strip() else ""
    if not bucket:
        bucket = _example_bucket()
        source = ".env.example 候选值（未写入 .env）"
    if not bucket:
        return {"ok": False, "reason": "缺少 R2_BUCKET_NAME"}

    def call() -> Dict[str, Any]:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=config.R2_ENDPOINT_URL,
            aws_access_key_id=config.R2_ACCESS_KEY_ID,
            aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(
                s3={"addressing_style": "path"},
                connect_timeout=8,
                read_timeout=12,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
        response = client.list_objects_v2(Bucket=bucket, MaxKeys=1)
        return {
            "ok": True,
            "bucket": bucket,
            "bucket_source": source or "config",
            "sample_objects": len(response.get("Contents", [])),
            "has_more": bool(response.get("IsTruncated")),
        }

    result = _timed(call)
    if "bucket" not in result:
        result["bucket"] = bucket
        result["bucket_source"] = source or "config"
    return result


def probe_dynamodb() -> Dict[str, Any]:
    if not config.AWS_ACCESS_KEY_ID or not config.AWS_SECRET_ACCESS_KEY:
        return {"ok": False, "reason": "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY 未配置"}

    def call() -> Dict[str, Any]:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "dynamodb",
            region_name=config.AWS_REGION,
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            config=Config(
                connect_timeout=8,
                read_timeout=12,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
        response = client.list_tables(Limit=20)
        tables = response.get("TableNames", [])
        return {
            "ok": True,
            "table_count": len(tables),
            "target_table_present": "fuli_resources" in tables,
        }

    return _timed(call)


def run_probes(params: Dict[str, Any]) -> int:
    """在子进程里执行探测并打印脱敏日志。返回成功的服务数量。"""
    targets: List[Tuple[str, Callable[[], Dict[str, Any]]]] = []
    if params.get("supabase", True):
        targets.append(("Supabase", probe_supabase))
    if params.get("r2", True):
        bucket = str(params.get("r2_bucket") or "")
        targets.append(("Cloudflare R2", lambda: probe_r2(bucket)))
    if params.get("dynamodb", True):
        targets.append(("DynamoDB", probe_dynamodb))

    print("[云端诊断] 只读探测开始：不会写入任何云端数据，输出已脱敏", flush=True)
    print(f"[云端诊断] 待检服务: {len(targets)} 个", flush=True)

    succeeded = 0
    for name, probe in targets:
        print(f"[云端诊断] 正在连接 {name} ...", flush=True)
        result = probe()
        if result.get("ok"):
            succeeded += 1
            details = {
                key: value for key, value in result.items()
                if key not in ("ok", "latency_ms", "error", "reason")
            }
            print(
                f"[OK] {name} 连接成功 · 耗时 {result.get('latency_ms')} ms · "
                f"{_safe(details)}",
                flush=True,
            )
        else:
            reason = result.get("error") or result.get("reason") or "未知原因"
            print(f"[FAIL] {name} 连接失败 · {reason}", flush=True)

    print(f"[云端诊断] 汇总: {succeeded}/{len(targets)} 个服务可用", flush=True)
    return succeeded


# ---------------------------------------------------------------------------
# 结构化多云用量快照
# 对齐旧版「多云用量监控」面板：Supabase 来源分布、R2 年份分布、DynamoDB
# 表水位。这里不复用 sync.stats，避免它在 import 时 set_run_mode('cloud')
# 修改全局运行模式；也不解析日志，直接返回结构化数据给前端表格。
# ---------------------------------------------------------------------------

def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def _supabase_usage() -> Dict[str, Any]:
    url = (config.SUPABASE_URL or "").strip()
    key = (config.SUPABASE_KEY or "").strip()
    empty = {"ok": False, "sources": [], "quota_mb": 500}
    if not url or not key:
        return {**empty, "reason": "SUPABASE_URL / SUPABASE_KEY 未配置"}
    try:
        from collections import Counter
        from urllib.parse import urlparse

        import requests
        from supabase import create_client

        parsed = urlparse(url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}"
        client = create_client(clean_url, key)
        table = client.table("resources")
        record_count = table.select("id", count="exact").limit(1).execute().count or 0
        pdf_count = table.select("id", count="exact").neq("pdf_path", "").limit(1).execute().count or 0

        counter: Counter = Counter()
        page_size = 1000
        for offset in range(0, 20_000, page_size):
            batch = table.select("source").range(offset, offset + page_size - 1).execute().data or []
            for row in batch:
                if row.get("source"):
                    counter[str(row["source"])] += 1
            if len(batch) < page_size:
                break

        size_bytes = record_count * 600
        size_text = ""
        try:
            response = requests.post(
                f"{clean_url}/rest/v1/rpc/get_table_size",
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
                timeout=8,
            )
            if response.status_code == 200:
                size_text = _safe(response.json())
        except Exception:
            pass

        return {
            "ok": True,
            "record_count": record_count,
            "pdf_count": pdf_count,
            "size_bytes": size_bytes,
            "size_human": _human(size_bytes),
            "size_rpc": size_text,
            "size_estimated": not bool(size_text),
            "sources": [{"source": name, "count": count} for name, count in counter.most_common()],
            "quota_mb": 500,
        }
    except Exception as exc:
        return {**empty, "reason": _safe(exc)}


def _r2_usage(year: str = "") -> Dict[str, Any]:
    empty = {"ok": False, "years": [], "quota_gb": 10}
    missing = [
        name for name in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME")
        if not getattr(config, name, "")
    ]
    if missing:
        return {**empty, "reason": f"缺少 {', '.join(missing)}"}
    try:
        client = _r2_client()
        prefix = f"pdf/{year}/" if year else "pdf/"
        total_count = 0
        total_size = 0
        years: Dict[str, Dict[str, int]] = {}
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=config.R2_BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = str(obj.get("Key", ""))
                if not key.lower().endswith(".pdf"):
                    continue
                size = int(obj.get("Size", 0))
                total_count += 1
                total_size += size
                parts = key.split("/")
                label = parts[1] if len(parts) >= 2 and parts[1] else "未分类"
                bucket = years.setdefault(label, {"count": 0, "size_bytes": 0})
                bucket["count"] += 1
                bucket["size_bytes"] += size
        return {
            "ok": True,
            "prefix": prefix,
            "file_count": total_count,
            "size_bytes": total_size,
            "size_human": _human(total_size),
            "years": [
                {
                    "year": label,
                    "count": value["count"],
                    "size_bytes": value["size_bytes"],
                    "size_human": _human(value["size_bytes"]),
                }
                for label, value in sorted(years.items())
            ],
            "quota_gb": 10,
        }
    except Exception as exc:
        return {**empty, "reason": _safe(exc)}


def _dynamodb_usage() -> Dict[str, Any]:
    empty = {"ok": False, "quota_gb": 25}
    if not config.AWS_ACCESS_KEY_ID or not config.AWS_SECRET_ACCESS_KEY:
        return {**empty, "reason": "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY 未配置"}
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "dynamodb",
            region_name=config.AWS_REGION,
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            config=Config(
                connect_timeout=8,
                read_timeout=12,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
        table = client.describe_table(TableName="fuli_resources")["Table"]
        size_bytes = int(table.get("TableSizeBytes", 0))
        return {
            "ok": True,
            "item_count": int(table.get("ItemCount", 0)),
            "size_bytes": size_bytes,
            "size_human": _human(size_bytes),
            "status": str(table.get("TableStatus", "")),
            "created_at": str(table.get("CreationDateTime", "")),
            "quota_gb": 25,
        }
    except Exception as exc:
        return {**empty, "reason": _safe(exc)}


def usage_report(params: Dict[str, Any]) -> Dict[str, Any]:
    """只读汇总多云用量，返回结构化指标与明细（供前端表格直接渲染）。"""
    started = time.perf_counter()
    report: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "supabase": {"ok": False, "reason": "未勾选", "sources": []},
        "r2": {"ok": False, "reason": "未勾选", "years": []},
        "dynamodb": {"ok": False, "reason": "未勾选"},
    }
    if params.get("supabase", True):
        report["supabase"] = _supabase_usage()
    if params.get("r2", True):
        report["r2"] = _r2_usage(str(params.get("year") or "").strip())
    if params.get("dynamodb", True):
        report["dynamodb"] = _dynamodb_usage()
    report["errors"] = [
        f"{name}: {report[name].get('reason')}"
        for name in ("supabase", "r2", "dynamodb")
        if not report[name].get("ok") and report[name].get("reason") != "未勾选"
    ]
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return report


def _r2_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=config.R2_ENDPOINT_URL,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            s3={"addressing_style": "path"},
            connect_timeout=8,
            read_timeout=20,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )


def cleanup_preview(prefix: str = "pdf/", local_dir: str = "") -> Dict[str, Any]:
    """只读列出待清理对象，并按本地是否有副本分类。

    复用 sync/r2_sync.classify_delete_candidates，保证工作台预检与 CLI 正式
    删除的判定完全一致；本函数绝不调用任何删除接口。
    """
    started = time.perf_counter()
    result: Dict[str, Any] = {
        "ok": False,
        "prefix": prefix or "pdf/",
        "local_dir": "",
        "total": 0,
        "have_count": 0,
        "missing_count": 0,
        "missing_size_human": "0 B",
        "missing_preview": [],
        "elapsed_ms": 0.0,
    }
    missing = [
        name for name in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET_NAME")
        if not getattr(config, name, "")
    ]
    if missing:
        result["reason"] = f"缺少 {', '.join(missing)}"
        return result

    out_dir = os.path.abspath(local_dir or config.PDF_BASE_DIR)
    result["local_dir"] = out_dir
    try:
        from sync.r2_sync import classify_delete_candidates

        candidates: List[Dict[str, Any]] = []
        paginator = _r2_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=config.R2_BUCKET_NAME, Prefix=result["prefix"]):
            for obj in page.get("Contents", []):
                key = str(obj.get("Key", ""))
                if key.lower().endswith(".pdf"):
                    candidates.append({"key": key, "size": int(obj.get("Size", 0))})

        have, absent = classify_delete_candidates(
            candidates, out_dir, [config.PDF_BASE_DIR],
        )
        result.update({
            "ok": True,
            "total": len(candidates),
            "have_count": len(have),
            "missing_count": len(absent),
            "missing_size_human": _human(sum(obj["size"] for obj in absent)),
            "missing_preview": [
                {"key": obj["key"], "size_human": _human(obj["size"])}
                for obj in absent[:200]
            ],
        })
    except Exception as exc:
        result["reason"] = _safe(exc)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result
