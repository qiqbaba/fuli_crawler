# -*- coding: utf-8 -*-
"""工作台产物文件（备份 / 导出库 / 审计表 / 报告）的清单与下载白名单。

旧版 Streamlit 工作台的导出结果只把落盘路径打印在日志里，用户需要自己去
目录里找。新工作台提供清单、下载与「打开所在目录」，同时把可访问路径严格
限定在产物目录 + 允许的扩展名内，避免 HTTP 接口变成任意本地文件读取。
"""
from __future__ import annotations

import glob
import os
from typing import Any, Dict, List, Optional

from config import PROJECT_ROOT

from . import db

_SUFFIXES = ("*.bak", "*.db", "*.sqlite", "*.csv", "*.md", "*.json", "*.log")
_KINDS = {
    ".bak": "备份",
    ".db": "导出库",
    ".sqlite": "导出库",
    ".csv": "审计表",
    ".md": "报告",
    ".json": "结果",
    ".log": "日志",
}
_ALLOWED_EXT = frozenset(_KINDS)
MAX_ITEMS = 60


def _export_dir() -> str:
    """与 fixes 侧所有导出任务共用同一个目录解析规则。"""
    try:
        from fixes.db_utils import get_export_dir

        return get_export_dir()
    except Exception:
        return os.path.join(PROJECT_ROOT, "temp_profiles")


def allowed_roots() -> List[str]:
    roots = [
        os.path.dirname(os.path.abspath(db.get_db_path())),
        _export_dir(),
        os.path.join(PROJECT_ROOT, "cache"),
        os.path.join(PROJECT_ROOT, "temp_profiles"),
    ]
    unique: List[str] = []
    for root in roots:
        normalized = os.path.normcase(os.path.realpath(root))
        if normalized not in unique:
            unique.append(normalized)
    return unique


def _under(root: str, path: str) -> bool:
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def list_artifacts(limit: int = MAX_ITEMS) -> List[Dict[str, Any]]:
    """按修改时间倒序列出备份 / 导出产物，供前端直接下载或定位。"""
    seen = set()
    items: List[Dict[str, Any]] = []
    for root in allowed_roots():
        if not os.path.isdir(root):
            continue
        for pattern in _SUFFIXES:
            for path in glob.glob(os.path.join(root, pattern)):
                if not os.path.isfile(path):
                    continue
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_size == 0:
                    continue
                key = os.path.normcase(os.path.realpath(path))
                if key in seen:
                    continue
                seen.add(key)
                real = os.path.realpath(path)
                items.append({
                    "name": os.path.basename(real),
                    "path": real,
                    "dir": os.path.dirname(real),
                    "kind": _KINDS.get(os.path.splitext(real)[1].lower(), "其他"),
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "mtime": int(stat.st_mtime),
                })
    return sorted(items, key=lambda item: item["mtime"], reverse=True)[:limit]


def resolve_for_request(raw: str) -> Optional[str]:
    """只接受产物目录内的白名单后缀文件，或产物目录下的子目录。"""
    if not raw:
        return None
    real = os.path.normcase(os.path.realpath(raw))
    if not any(_under(root, real) for root in allowed_roots()):
        return None
    if os.path.isfile(raw):
        if os.path.splitext(real)[1].lower() not in _ALLOWED_EXT:
            return None
        return os.path.realpath(raw)
    if os.path.isdir(raw):
        # 只允许打开产物根目录或清单中文件所在的目录，避免借「打开目录」
        # 访问导出盘下的任意子目录
        known = {os.path.normcase(os.path.realpath(item["dir"])) for item in list_artifacts()}
        if real in known or real in allowed_roots():
            return os.path.realpath(raw)
    return None
