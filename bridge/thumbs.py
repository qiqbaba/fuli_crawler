# -*- coding: utf-8 -*-
"""PDF 封面页按需光栅化与磁盘缓存。

复用 viewer.py 中 PDFRequestHandler 的思路，但对请求路径做白名单校验，
避免 HTTP 接口被用作任意本地文件读取。
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

from config import PROJECT_ROOT, PDF_BASE_DIR

from .db import resolve_pdf_path

THUMB_CACHE_DIR = os.path.join(PROJECT_ROOT, "cache", "pdf_thumbs")

_ALLOWED_ROOTS = tuple(
    os.path.normcase(os.path.realpath(root)) for root in (PDF_BASE_DIR, PROJECT_ROOT)
)


def resolve_for_request(raw_path: str) -> Optional[str]:
    """把数据库中的 pdf_path 解析为受允许的本地 PDF 绝对路径。"""
    resolved = resolve_pdf_path(raw_path)
    if not resolved:
        return None
    real = os.path.normcase(os.path.realpath(resolved))
    if not real.endswith(".pdf"):
        return None
    for root in _ALLOWED_ROOTS:
        if real == root or real.startswith(root + os.sep):
            return resolved
    return None


def render_page(real_path: str, page: int, dpi: int, quality: int) -> Optional[bytes]:
    """缓存键与 fixes/warmup_pdf_cache.render_single_pdf 完全一致：

    key = md5(路径_修改时间_DPI_质量)，单页文件名 {key}_{页码}.jpg，
    这样「缓存预热」任务写出的 {key}_done.flag 与全部页图能被浏览页直接命中。
    """
    os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
    mtime = os.path.getmtime(real_path)
    key = hashlib.md5(f"{real_path}_{mtime}_{dpi}_{quality}".encode("utf-8")).hexdigest()
    cache_path = os.path.join(THUMB_CACHE_DIR, f"{key}_{page}.jpg")

    if os.path.isfile(cache_path):
        with open(cache_path, "rb") as f:
            return f.read()

    import pymupdf

    with pymupdf.open(real_path) as doc:
        if not 0 <= page < len(doc):
            return None
        pix = doc[page].get_pixmap(dpi=dpi)
        try:
            data = pix.tobytes("jpeg", jpg_quality=quality)
        except TypeError:
            data = pix.tobytes("jpeg", quality)

    tmp_path = f"{cache_path}.{os.getpid()}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, cache_path)
    return data
