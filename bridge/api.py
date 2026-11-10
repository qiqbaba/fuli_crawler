# -*- coding: utf-8 -*-
"""资源库工作台 FastAPI 服务。

开发期由 Vite 代理 /api 到本服务；构建完成后由本服务直接托管 desktop/dist。
启动：python -m uvicorn bridge.api:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import PROJECT_ROOT

from . import db, jobs, thumbs
from .registry import tool_catalog

app = FastAPI(title="资源库工作台 API", version="0.1.0")


@app.get("/api/health")
def health() -> Dict[str, Any]:
    db_path = db.get_db_path()
    return {"status": "ok", "db_exists": os.path.exists(db_path), "db_path": db_path}


@app.get("/api/stats")
def stats() -> Dict[str, Any]:
    return db.get_stats()


@app.get("/api/analytics")
def analytics() -> Dict[str, Any]:
    return db.get_analytics()


@app.get("/api/filter-options")
def filter_options(
    keyword: str = "",
    source: str = "全部",
    category: str = "全部",
    pdf_filter: str = "全部",
    fix_filter: str = "全部",
) -> Dict[str, Any]:
    """按当前画廊筛选条件返回级联有效的来源/分类，避免选中无结果的组合。"""
    return db.get_filter_options(
        keyword=keyword,
        source=source,
        category=category,
        pdf_filter=pdf_filter,
        fix_filter=fix_filter,
    )


@app.get("/api/records")
def records(
    keyword: str = "",
    source: str = "全部",
    category: str = "全部",
    pdf_filter: str = "全部",
    fix_filter: str = "全部",
    sort_by: str = "最新入库 (ID)",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    return db.query_records(
        keyword=keyword,
        source=source,
        category=category,
        pdf_filter=pdf_filter,
        fix_filter=fix_filter,
        sort_by=sort_by,
        page=page,
        page_size=page_size,
    )


@app.delete("/api/records/{record_id}")
def delete_record(record_id: str, pdf_path: str = "") -> Dict[str, Any]:
    return db.delete_record(record_id, pdf_path)


@app.get("/api/records/{record_id}/pdf-refs")
def pdf_references(record_id: str, pdf_path: str = "") -> Dict[str, Any]:
    """删除前查询共享 PDF 引用数，用于展示与旧版一致的危险提示。"""
    return {
        "record_id": record_id,
        "other_refs": db.count_other_pdf_references(
            record_id, pdf_path, db.resolve_pdf_path(pdf_path)
        ),
    }


class RevealRequest(BaseModel):
    path: str


@app.post("/api/reveal")
def reveal(request: RevealRequest) -> Dict[str, Any]:
    """在系统资源管理器中定位本地 PDF；路径必须落在允许的根目录内。"""
    real_path = thumbs.resolve_for_request(request.path)
    if not real_path:
        raise HTTPException(status_code=404, detail="文件不存在或路径不被允许")
    import subprocess
    import sys

    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(real_path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", real_path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(real_path)])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"无法打开文件管理器: {exc}") from exc
    return {"ok": True, "path": real_path}


@app.get("/api/backups")
def backups() -> Dict[str, Any]:
    return {"backups": db.list_backups()}


@app.get("/api/cache-overview")
def cache_overview() -> Dict[str, Any]:
    return db.get_cache_overview()


@app.get("/api/pdf-page")
def pdf_page(
    path: str,
    page: int = Query(0, ge=0, le=999),
    dpi: int = Query(75, ge=30, le=200),
    quality: int = Query(72, ge=30, le=95),
) -> Response:
    real_path = thumbs.resolve_for_request(path)
    if not real_path:
        raise HTTPException(status_code=404, detail="PDF 不可用或路径不被允许")
    try:
        data = thumbs.render_page(real_path, page, dpi, quality)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF 渲染失败: {exc}") from exc
    if not data:
        raise HTTPException(status_code=404, detail="PDF 页面不存在")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@app.get("/api/pdf-file")
def pdf_file(path: str) -> Response:
    """返回原始 PDF 供浏览器内置查看器预览。

    与旧版 Streamlit 工作台的 base64 iframe 兜底等价：页图光栅化失败时
    仍可查看原文，同时路径必须经过 thumbs 的白名单校验。
    """
    real_path = thumbs.resolve_for_request(path)
    if not real_path:
        raise HTTPException(status_code=404, detail="PDF 不可用或路径不被允许")
    return FileResponse(
        real_path,
        media_type="application/pdf",
        headers={"Cache-Control": "private, max-age=300"},
    )


class JobRequest(BaseModel):
    tool_id: str
    params: Dict[str, Any] = Field(default_factory=dict)


@app.get("/api/tools")
def tools() -> Dict[str, Any]:
    return {"tools": tool_catalog(), "max_concurrent": jobs.MAX_CONCURRENT}


@app.post("/api/jobs", status_code=201)
def create_job(request: JobRequest) -> Dict[str, Any]:
    try:
        job = jobs.create_job(request.tool_id, request.params)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.to_dict(include_logs=True)


@app.get("/api/jobs")
def job_list() -> Dict[str, Any]:
    return {"jobs": jobs.list_jobs()}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str) -> Dict[str, Any]:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.to_dict(include_logs=True)


@app.get("/api/jobs/{job_id}/stream")
def job_stream(job_id: str) -> StreamingResponse:
    if jobs.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return StreamingResponse(
        jobs.stream_events(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> Dict[str, Any]:
    job = jobs.cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.to_dict()


_DIST = os.path.join(PROJECT_ROOT, "desktop", "dist")

if os.path.isfile(os.path.join(_DIST, "index.html")):
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="spa")
else:
    @app.get("/", include_in_schema=False)
    def root() -> Dict[str, str]:
        return {
            "message": "前端尚未构建",
            "dev": "开发模式请访问 http://127.0.0.1:4173",
            "build": "在 desktop/ 目录执行 npm run build 后访问 http://127.0.0.1:8000",
        }
