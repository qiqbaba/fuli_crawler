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
