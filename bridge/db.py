# -*- coding: utf-8 -*-
"""工作台 API 的独立 SQLite 查询层。

不能 import viewer.py：该模块顶层就是一份 Streamlit 脚本，导入即会触发
st.set_page_config、数据库查询与 UI 注入等副作用。此处按 DBReader 的查询
语义实现最小必要子集，使新版 API 与旧版 Streamlit 工作台彼此独立。
"""
from __future__ import annotations

import functools
import glob
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from config import PROJECT_ROOT, PDF_BASE_DIR, get_db_path

SORT_CLAUSES: Dict[str, str] = {
    "最新入库 (ID)": "r.id DESC",
    "最早入库 (ID)": "r.id ASC",
    "发布时间 (新到旧)": "CASE WHEN r.publish_time IS NULL OR r.publish_time = '' THEN 1 ELSE 0 END, r.publish_time DESC, r.id DESC",
    "发布时间 (旧到新)": "CASE WHEN r.publish_time IS NULL OR r.publish_time = '' THEN 1 ELSE 0 END, r.publish_time ASC, r.id ASC",
    "标题名称 (A到Z)": "r.title ASC, r.id ASC",
    "标题名称 (Z到A)": "r.title DESC, r.id DESC",
    "标题分组/重复相邻": "r.title ASC, r.id ASC",
    "磁力链接分组/重复相邻": "CASE WHEN r.resource_link IS NULL OR r.resource_link = '' THEN 1 ELSE 0 END, r.resource_link ASC, r.id ASC",
    "PDF路径分组/重复相邻": "CASE WHEN r.pdf_path IS NULL OR r.pdf_path = '' THEN 1 ELSE 0 END, r.pdf_path ASC, r.id ASC",
}

PAGE_SIZES: Tuple[int, ...] = (10, 20, 30, 40, 50)

# 与 viewer.py 的 DEFAULT_PDF_OPTIONS 保持一致
PDF_FILTERS: Tuple[str, ...] = (
    "全部",
    "数据库中没有的 PDF (磁盘孤儿)",
    "本地 PDF 正常存在",
    "本地物理 PDF 缺失",
    "数据库未关联 PDF",
    "未知年份/日期",
    "损坏/微小 PDF (<20KB)",
)

# 与 viewer.py 的 DEFAULT_FIX_OPTIONS 保持一致（质检 / 维护维度）
FIX_FILTERS: Tuple[str, ...] = (
    "全部",
    "日本番号资源",
    "标题完全重复",
    "PDF路径重复",
    "磁力链接重复",
    "URL 地址重复",
    "资源链接为空",
    "文件大小缺失",
    "含推广广告噪声",
)

DUP_FIELD_BY_FIX: Dict[str, str] = {
    "标题完全重复": "title",
    "PDF路径重复": "pdf_path",
    "磁力链接重复": "resource_link",
    "URL 地址重复": "url",
}

ORPHAN_PDF_FILTER = "数据库中没有的 PDF (磁盘孤儿)"

_ID_SCAN_TTL = 300.0
_ID_SCAN_CACHE: Dict[str, Tuple[float, Any]] = {}

_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_res_title ON resources(title)",
    "CREATE INDEX IF NOT EXISTS idx_res_reslink ON resources(resource_link)",
    "CREATE INDEX IF NOT EXISTS idx_res_pdf_path ON resources(pdf_path)",
    "CREATE INDEX IF NOT EXISTS idx_res_source ON resources(source)",
    "CREATE INDEX IF NOT EXISTS idx_res_category ON resources(category)",
    "CREATE INDEX IF NOT EXISTS idx_res_src_cat ON resources(source, category)",
    "CREATE INDEX IF NOT EXISTS idx_res_cat_src ON resources(category, source)",
)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


@functools.lru_cache(maxsize=1)
def _ensure_indexes() -> None:
    if not os.path.exists(get_db_path()):
        return
    try:
        with connect() as conn:
            for stmt in _INDEX_STATEMENTS:
                conn.execute(stmt)
            conn.commit()
    except sqlite3.Error:
        pass


@functools.lru_cache(maxsize=8192)
def resolve_pdf_path(raw_path: str) -> str:
    """按 viewer.py 的历史路径规则解析出本地 PDF 真实路径。"""
    if not raw_path or not isinstance(raw_path, str):
        return ""
    if os.path.isabs(raw_path) and os.path.isfile(raw_path):
        return os.path.abspath(raw_path)

    clean_rel = raw_path.replace("\\", "/").lstrip("/")
    stripped = clean_rel[4:] if clean_rel.startswith("pdf/") else None
    candidates = [
        os.path.join(PDF_BASE_DIR, stripped or clean_rel),
        os.path.join(PROJECT_ROOT, clean_rel),
        os.path.join(PDF_BASE_DIR, clean_rel),
        os.path.join(PROJECT_ROOT, "pdf", clean_rel),
    ]
    if stripped is not None:
        candidates.append(os.path.join(PDF_BASE_DIR, stripped))

    for path in candidates:
        abs_p = os.path.abspath(path)
        if os.path.isfile(abs_p):
            return abs_p
    return ""


def invalidate_scans() -> None:
    """删除记录、清理 PDF 或执行维护任务后丢弃扫描缓存。"""
    _ID_SCAN_CACHE.clear()
    resolve_pdf_path.cache_clear()


def _cached_scan(key: str, producer) -> Any:
    now = time.monotonic()
    hit = _ID_SCAN_CACHE.get(key)
    if hit and now - hit[0] < _ID_SCAN_TTL:
        return hit[1]
    value = producer()
    _ID_SCAN_CACHE[key] = (now, value)
    return value


def _scan_pdf_states() -> Dict[str, List[int]]:
    """扫描磁盘，得到「路径记录存在但文件不存在」与「损坏小文件」两类记录 ID。"""

    def produce() -> Dict[str, List[int]]:
        states: Dict[str, List[int]] = {"missing": [], "tiny": []}
        if not os.path.exists(get_db_path()):
            return states
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, pdf_path FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''"
            ).fetchall()
        for row in rows:
            resolved = resolve_pdf_path(row["pdf_path"])
            if not resolved:
                states["missing"].append(row["id"])
                continue
            try:
                if os.path.getsize(resolved) < 20 * 1024:
                    states["tiny"].append(row["id"])
            except OSError:
                pass
        return states

    return _cached_scan("pdf_states", produce)


def _fanhao_ids() -> List[int]:
    """严格日本番号识别算法命中的记录 ID（对应 fixes/record_filter.py fanhao）。"""

    def produce() -> List[int]:
        if not os.path.exists(get_db_path()):
            return []
        from utils.fanhao_filter import extract_fanhao

        with connect() as conn:
            rows = conn.execute(
                "SELECT id, title FROM resources WHERE title IS NOT NULL AND title != ''"
            ).fetchall()
        return [row["id"] for row in rows if extract_fanhao(row["title"])[0]]

    return _cached_scan("fanhao", produce)


def _noisy_link_ids() -> List[int]:
    """resource_link 含广告推广噪声的记录 ID（对应 fixes/data_cleaner.py clean-noise）。"""

    def produce() -> List[int]:
        if not os.path.exists(get_db_path()):
            return []
        from utils.resource_link_cleaner import clean_resource_link

        with connect() as conn:
            rows = conn.execute(
                "SELECT id, resource_link FROM resources "
                "WHERE resource_link IS NOT NULL AND resource_link != ''"
            ).fetchall()
        return [row["id"] for row in rows if clean_resource_link(row["resource_link"]) != row["resource_link"]]

    return _cached_scan("noisy", produce)


def _orphan_records() -> List[Dict[str, Any]]:
    """扫描磁盘上存在但数据库未登记的孤儿 PDF，生成 ORPHAN-n 虚拟记录。"""

    def produce() -> List[Dict[str, Any]]:
        if not os.path.isdir(PDF_BASE_DIR):
            return []
        from utils.pdf_utils import parse_filename

        known_sources = [
            "datang", "dashen", "jingpin", "tanhua", "taose",
            "mianfei_guochan", "madou", "seju", "jingpin_toupai",
        ]
        db_paths = set()
        if os.path.exists(get_db_path()):
            with connect() as conn:
                for row in conn.execute(
                    "SELECT pdf_path FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''"
                ):
                    clean = str(row["pdf_path"]).replace("\\", "/").lstrip("/")
                    if clean.startswith("pdf/"):
                        clean = clean[4:]
                    db_paths.add(clean.lower())
                    db_paths.add(os.path.basename(clean).lower())

        records: List[Dict[str, Any]] = []
        index = 1
        for root, _dirs, files in os.walk(PDF_BASE_DIR):
            for name in files:
                if not name.lower().endswith(".pdf"):
                    continue
                full_path = os.path.join(root, name)
                rel = os.path.relpath(full_path, PDF_BASE_DIR).replace("\\", "/").lower()
                if rel in db_paths or name.lower() in db_paths:
                    continue
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                pub_date, title = parse_filename(name)
                stem = name[:-4]
                source = "未知"
                for known in known_sources:
                    if stem.endswith(f"_{known}"):
                        source = known
                        title = (title or "")[: -(len(known) + 1)].rstrip("_")
                        break
                try:
                    stored_path = os.path.relpath(full_path, PROJECT_ROOT)
                except ValueError:
                    # PDF 根目录与项目不在同一盘符时 relpath 会抛错，退回绝对路径
                    stored_path = full_path
                records.append({
                    "id": f"ORPHAN-{index}",
                    "title": title or name,
                    "category": "磁盘孤儿",
                    "source": source,
                    "size": f"{size / (1024 * 1024):.2f} MB" if size > 1024 * 1024 else f"{size / 1024:.1f} KB",
                    "format": "PDF",
                    "url": "",
                    "resource_link": "",
                    "pikpak_link": "",
                    "pdf_path": stored_path,
                    "resolved_path": full_path,
                    "publish_time": pub_date or "-",
                    "has_pdf": True,
                    "pdf_status": "本地存在(未入库)",
                    "dup_cnt": 1,
                    "_abs_path": full_path,
                })
                index += 1
        return records

    return _cached_scan("orphans", produce)


def _build_filters(
    *,
    keyword: str = "",
    source: str = "全部",
    category: str = "全部",
    pdf_filter: str = "全部",
    fix_filter: str = "全部",
    exclude: Optional[str] = None,
) -> Tuple[str, str, List[Any], str]:
    """构建 JOIN / WHERE / 参数 / 重复分组字段，语义对齐旧版 DBReader。"""
    conditions: List[str] = []
    params: List[Any] = []
    join = ""
    dup_field = ""

    kw = "" if exclude == "keyword" else keyword.strip()
    src = "全部" if exclude == "source" else source
    cat = "全部" if exclude == "category" else category
    pdf = "全部" if exclude == "pdf_filter" else pdf_filter
    fix = "全部" if exclude == "fix_filter" else fix_filter

    if kw:
        like = f"%{kw}%"
        conditions.append(
            "(r.title LIKE ? OR r.url LIKE ? OR r.resource_link LIKE ? OR r.pikpak_link LIKE ?)"
        )
        params.extend([like, like, like, like])

    if src != "全部":
        conditions.append("r.source = ?")
        params.append(src)

    if cat != "全部":
        conditions.append("r.category = ?")
        params.append(cat)

    if pdf in ("本地 PDF 正常存在", "本地物理 PDF 缺失", "损坏/微小 PDF (<20KB)"):
        states = _scan_pdf_states()
        key = "tiny" if pdf == "损坏/微小 PDF (<20KB)" else "missing"
        ids = states[key]
        if pdf == "本地 PDF 正常存在":
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conditions.append(
                    f"(r.pdf_path IS NOT NULL AND r.pdf_path != '' AND r.id NOT IN ({placeholders}))"
                )
                params.extend(ids)
            else:
                conditions.append("(r.pdf_path IS NOT NULL AND r.pdf_path != '')")
        elif ids:
            placeholders = ",".join("?" for _ in ids)
            conditions.append(f"r.id IN ({placeholders})")
            params.extend(ids)
        else:
            conditions.append("1 = 0")
    elif pdf == "数据库未关联 PDF":
        conditions.append("(r.pdf_path IS NULL OR r.pdf_path = '')")
    elif pdf == "未知年份/日期":
        conditions.append("(r.pdf_path LIKE '%Unknown_Year%' OR r.pdf_path LIKE '%Unknown_Date%')")

    if fix in DUP_FIELD_BY_FIX:
        dup_field = DUP_FIELD_BY_FIX[fix]
        join = f"""
            INNER JOIN (
                SELECT {dup_field} AS dup_value, MAX(id) AS max_id, COUNT(*) AS dup_cnt
                FROM resources
                WHERE {dup_field} IS NOT NULL AND {dup_field} != ''
                GROUP BY {dup_field} HAVING COUNT(*) > 1
            ) d ON r.{dup_field} = d.dup_value
        """
    elif fix == "日本番号资源":
        ids = _fanhao_ids()
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conditions.append(f"r.id IN ({placeholders})")
            params.extend(ids)
        else:
            conditions.append("1 = 0")
    elif fix == "资源链接为空":
        conditions.append("(r.resource_link IS NULL OR r.resource_link = '')")
    elif fix == "文件大小缺失":
        conditions.append("(r.size IS NULL OR r.size = '' OR r.size = '-')")
    elif fix == "含推广广告噪声":
        ids = _noisy_link_ids()
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conditions.append(f"r.id IN ({placeholders})")
            params.extend(ids)
        else:
            conditions.append("1 = 0")

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return join, where, params, dup_field


def _order_clause(sort_by: str, dup_field: str) -> str:
    """重复分组模式下强制同组相邻排布（对齐旧版行为）。"""
    if not dup_field:
        return SORT_CLAUSES.get(sort_by, "r.id DESC")
    if sort_by == "最早入库 (ID)":
        return f"d.max_id ASC, r.{dup_field} ASC, r.id ASC"
    if sort_by == "发布时间 (新到旧)":
        return (
            "CASE WHEN r.publish_time IS NULL OR r.publish_time = '' THEN 1 ELSE 0 END, "
            f"r.publish_time DESC, r.{dup_field} ASC, r.id ASC"
        )
    if sort_by == "发布时间 (旧到新)":
        return (
            "CASE WHEN r.publish_time IS NULL OR r.publish_time = '' THEN 1 ELSE 0 END, "
            f"r.publish_time ASC, r.{dup_field} ASC, r.id ASC"
        )
    if sort_by == "标题名称 (Z到A)":
        return "r.title DESC, r.id DESC"
    if sort_by in ("标题名称 (A到Z)", "标题分组/重复相邻"):
        return "r.title ASC, r.id ASC"
    if sort_by == "磁力链接分组/重复相邻":
        return (
            "CASE WHEN r.resource_link IS NULL OR r.resource_link = '' THEN 1 ELSE 0 END, "
            "r.resource_link ASC, r.id ASC"
        )
    if sort_by == "PDF路径分组/重复相邻":
        return (
            "CASE WHEN r.pdf_path IS NULL OR r.pdf_path = '' THEN 1 ELSE 0 END, "
            "r.pdf_path ASC, r.id ASC"
        )
    return f"d.max_id DESC, r.{dup_field} ASC, r.id ASC"


def get_stats() -> Dict[str, Any]:
    db_path = get_db_path()
    if not os.path.exists(db_path):
        return {
            "total": 0, "has_pdf_record": 0, "sources": [], "categories": [],
            "db_path": db_path, "db_size_mb": 0.0, "pdf_dir": PDF_BASE_DIR,
        }
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        has_pdf = conn.execute(
            "SELECT COUNT(*) FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''"
        ).fetchone()[0]
        sources = [r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM resources WHERE source IS NOT NULL AND source != ''"
        )]
        categories = [r[0] for r in conn.execute(
            "SELECT DISTINCT category FROM resources WHERE category IS NOT NULL AND category != ''"
        )]
    return {
        "total": total,
        "has_pdf_record": has_pdf,
        "sources": sorted(sources),
        "categories": sorted(categories),
        "db_path": db_path,
        "db_size_mb": round(os.path.getsize(db_path) / (1024 * 1024), 2),
        "pdf_dir": PDF_BASE_DIR,
    }


def _distinct_values(
    column: str,
    *,
    keyword: str,
    source: str,
    category: str,
    pdf_filter: str,
    fix_filter: str,
    exclude: str,
) -> List[str]:
    """按「排除自身筛选条件」的方式计算某一列的有效取值（旧版级联语义）。"""
    join, where, params, _ = _build_filters(
        keyword=keyword,
        source=source,
        category=category,
        pdf_filter=pdf_filter,
        fix_filter=fix_filter,
        exclude=exclude,
    )
    extra = f"r.{column} IS NOT NULL AND r.{column} != ''"
    clause = f"{where} AND {extra}" if where else f" WHERE {extra}"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT r.{column} FROM resources r {join} {clause} ORDER BY r.{column}",
            params,
        ).fetchall()
    return [str(row[0]) for row in rows]


def _orphan_filter_options(keyword: str, category: str) -> Tuple[List[str], List[str], int]:
    """磁盘孤儿模式下，来源/分类只能来自孤儿文件本身（对齐旧版）。"""
    kw = keyword.strip().lower()
    sources = set()
    records = _orphan_records()
    for record in records:
        if kw and not any(
            kw in str(record.get(field) or "").lower()
            for field in ("title", "pdf_path", "source")
        ):
            continue
        if category not in ("全部", "磁盘孤儿") and record.get("category") != category:
            continue
        value = str(record.get("source") or "").strip()
        if value:
            sources.add(value)
    return ["全部", *sorted(sources)], ["全部", "磁盘孤儿"], len(records)


def get_filter_options(
    *,
    keyword: str = "",
    source: str = "全部",
    category: str = "全部",
    pdf_filter: str = "全部",
    fix_filter: str = "全部",
) -> Dict[str, Any]:
    """按当前筛选条件实时计算级联来源/分类，剔除无结果的选项。"""
    _ensure_indexes()
    base = {
        "pdf_filters": list(PDF_FILTERS),
        "fix_filters": list(FIX_FILTERS),
        "sorts": list(SORT_CLAUSES),
        "page_sizes": list(PAGE_SIZES),
        "orphan_count": 0,
    }
    if not os.path.exists(get_db_path()):
        return {**base, "sources": ["全部"], "categories": ["全部"]}

    if pdf_filter == ORPHAN_PDF_FILTER:
        sources, categories, count = _orphan_filter_options(keyword, category)
        return {
            **base,
            "sources": sources,
            "categories": categories,
            "fix_filters": ["全部"],
            "orphan_count": count,
        }

    sources = _distinct_values(
        "source", keyword=keyword, source=source, category=category,
        pdf_filter=pdf_filter, fix_filter=fix_filter, exclude="source",
    )
    categories = _distinct_values(
        "category", keyword=keyword, source=source, category=category,
        pdf_filter=pdf_filter, fix_filter=fix_filter, exclude="category",
    )
    if pdf_filter == "全部" and "磁盘孤儿" not in categories:
        categories.append("磁盘孤儿")
    return {
        **base,
        "sources": ["全部", *sources],
        "categories": ["全部", *sorted(categories)],
    }


def _to_item(row: sqlite3.Row) -> Dict[str, Any]:
    def text(key: str) -> str:
        value = row[key]
        return "" if value is None else str(value)

    raw_pdf = text("pdf_path")
    resolved = resolve_pdf_path(raw_pdf)
    try:
        dup_cnt = int(row["dup_cnt"])
    except (IndexError, KeyError, TypeError, ValueError):
        dup_cnt = 1
    return {
        "id": row["id"],
        "title": text("title"),
        "category": text("category"),
        "source": text("source"),
        "size": text("size"),
        "format": text("resource_format"),
        "url": text("url"),
        "resource_link": text("resource_link"),
        "pikpak_link": text("pikpak_link"),
        "pdf_path": raw_pdf,
        "resolved_path": resolved,
        "publish_time": text("publish_time"),
        "has_pdf": bool(resolved),
        "pdf_status": "已存在" if resolved else ("路径缺失" if raw_pdf else "无文件"),
        "dup_cnt": dup_cnt,
    }


def _query_orphans(
    *,
    keyword: str,
    source: str,
    category: str,
    sort_by: str,
    page: int,
    page_size: int,
) -> Dict[str, Any]:
    records = _orphan_records()
    kw = keyword.strip().lower()
    filtered = []
    for record in records:
        if kw and not any(
            kw in str(record[field]).lower()
            for field in ("title", "pdf_path", "source")
        ):
            continue
        if source != "全部" and record["source"] != source:
            continue
        if category not in ("全部", "磁盘孤儿") and record["category"] != category:
            continue
        filtered.append(record)

    def key(value: Dict[str, Any], field: str) -> str:
        return str(value.get(field) or "").lower()

    if sort_by == "最早入库 (ID)":
        filtered.sort(key=lambda r: r["id"])
    elif sort_by == "发布时间 (新到旧)":
        filtered.sort(key=lambda r: (key(r, "publish_time"), r["id"]), reverse=True)
    elif sort_by == "发布时间 (旧到新)":
        filtered.sort(key=lambda r: (key(r, "publish_time"), r["id"]))
    elif sort_by in ("标题名称 (A到Z)", "标题分组/重复相邻"):
        filtered.sort(key=lambda r: (key(r, "title"), r["id"]))
    elif sort_by == "标题名称 (Z到A)":
        filtered.sort(key=lambda r: (key(r, "title"), r["id"]), reverse=True)
    elif sort_by == "磁力链接分组/重复相邻":
        filtered.sort(key=lambda r: (key(r, "resource_link"), r["id"]))
    elif sort_by == "PDF路径分组/重复相邻":
        filtered.sort(key=lambda r: (key(r, "pdf_path"), r["id"]))
    else:
        filtered.sort(key=lambda r: int(str(r["id"]).split("-")[-1]), reverse=True)

    total = len(filtered)
    offset = (page - 1) * page_size
    items = []
    for record in filtered[offset: offset + page_size]:
        item = {k: v for k, v in record.items() if not k.startswith("_")}
        items.append(item)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "items": items,
    }


def query_records(
    *,
    keyword: str = "",
    source: str = "全部",
    category: str = "全部",
    pdf_filter: str = "全部",
    fix_filter: str = "全部",
    sort_by: str = "最新入库 (ID)",
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    _ensure_indexes()
    if pdf_filter == ORPHAN_PDF_FILTER:
        return _query_orphans(
            keyword=keyword,
            source=source,
            category=category,
            sort_by=sort_by,
            page=page,
            page_size=page_size,
        )

    join, where, params, dup_field = _build_filters(
        keyword=keyword,
        source=source,
        category=category,
        pdf_filter=pdf_filter,
        fix_filter=fix_filter,
    )
    order = _order_clause(sort_by, dup_field)
    offset = (page - 1) * page_size

    db_path = get_db_path()
    if not os.path.exists(db_path):
        return {"total": 0, "page": page, "page_size": page_size, "total_pages": 1, "items": []}

    dup_cnt_select = "d.dup_cnt AS dup_cnt" if dup_field else "1 AS dup_cnt"
    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM resources r {join} {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT r.id, r.title, r.category, r.source, r.size, r.resource_format,
                   r.url, r.resource_link, r.pikpak_link, r.pdf_path, r.publish_time,
                   {dup_cnt_select}
            FROM resources r
            {join}
            {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            params + [page_size, offset],
        ).fetchall()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "items": [_to_item(row) for row in rows],
    }


def _path_variants(raw_pdf_path: str, abs_path: str = "") -> List[str]:
    """列出同一 PDF 文件在数据库里可能出现的各种路径写法。"""
    variants = set()

    def add(value: str) -> None:
        if not value:
            return
        variants.add(value)
        variants.add(value.replace("/", "\\"))
        variants.add(value.replace("\\", "/"))
        clean = value.replace("\\", "/").lstrip("/")
        variants.add(clean)
        if clean.startswith("pdf/"):
            tail = clean[4:]
            variants.add(tail)
            variants.add("pdf/" + tail)
            variants.add("pdf\\" + tail.replace("/", "\\"))

    add(raw_pdf_path)
    add(abs_path)
    if abs_path:
        try:
            add(os.path.relpath(abs_path, PROJECT_ROOT))
        except ValueError:
            pass
    return [v for v in variants if v]


def count_other_pdf_references(record_id: Any, raw_pdf_path: str = "", abs_path: str = "") -> int:
    """统计除当前记录外，还有多少条记录引用了同一个 PDF 文件。"""
    if not os.path.exists(get_db_path()):
        return 0
    variants = _path_variants(raw_pdf_path, abs_path)
    if not variants:
        return 0
    placeholders = ",".join("?" for _ in variants)
    with connect() as conn:
        if str(record_id).startswith("ORPHAN-"):
            return int(conn.execute(
                f"SELECT COUNT(*) FROM resources WHERE pdf_path IN ({placeholders})", variants
            ).fetchone()[0])
        return int(conn.execute(
            f"SELECT COUNT(*) FROM resources WHERE id != ? AND pdf_path IN ({placeholders})",
            [record_id, *variants],
        ).fetchone()[0])


def delete_record(record_id: Any, pdf_path: str = "") -> Dict[str, Any]:
    """删除单条记录，并在无其他引用时安全级联删除本地 PDF（对齐旧版行为）。"""
    db_path = get_db_path()
    is_orphan = str(record_id).startswith("ORPHAN-")
    messages: List[str] = []
    db_deleted = False
    pdf_deleted = False

    target_pdf = ""
    if is_orphan:
        for record in _orphan_records():
            if record["id"] == str(record_id):
                target_pdf = record.get("_abs_path") or resolve_pdf_path(record["pdf_path"])
                pdf_path = record["pdf_path"]
                break
        if not target_pdf:
            target_pdf = resolve_pdf_path(pdf_path)
    else:
        target_pdf = resolve_pdf_path(pdf_path)

    other_refs = 0
    if target_pdf or pdf_path:
        other_refs = count_other_pdf_references(record_id, pdf_path, target_pdf)

    if target_pdf and os.path.isfile(target_pdf):
        if other_refs > 0:
            messages.append(f"本地 PDF 仍被其他 {other_refs} 条记录引用，已安全保留物理文件")
        else:
            try:
                os.remove(target_pdf)
                pdf_deleted = True
                messages.append("PDF 文件已删除（无其他引用）")
            except OSError as exc:
                messages.append(f"PDF 删除失败: {exc}")
    elif pdf_path:
        messages.append("本地 PDF 物理文件不存在")
    else:
        messages.append("无关联 PDF")

    if is_orphan:
        messages.append("磁盘孤儿已清理")
    elif os.path.exists(db_path):
        try:
            with connect() as conn:
                cursor = conn.execute("DELETE FROM resources WHERE id = ?", (record_id,))
                conn.commit()
                if cursor.rowcount > 0:
                    db_deleted = True
                    messages.append("数据库记录已删除")
                else:
                    messages.append("数据库中未找到该记录")
        except sqlite3.Error as exc:
            messages.append(f"数据库删除失败: {exc}")

    invalidate_scans()
    return {
        "ok": db_deleted or pdf_deleted,
        "record_id": str(record_id),
        "db_deleted": db_deleted,
        "pdf_deleted": pdf_deleted,
        "other_refs": other_refs,
        "message": "，".join(messages),
    }


def list_backups() -> List[Dict[str, Any]]:
    """列出与主库同目录的历史 .bak 备份（对齐旧版维护中心的备份清单）。"""
    db_path = get_db_path()
    folder = os.path.dirname(os.path.abspath(db_path))
    items: List[Dict[str, Any]] = []
    for path in glob.glob(os.path.join(folder, "*.bak")):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        items.append({
            "name": os.path.basename(path),
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "mtime": int(stat.st_mtime),
            "path": path,
        })
    return sorted(items, key=lambda item: item["mtime"], reverse=True)


def get_cache_overview() -> Dict[str, Any]:
    """PDF 缩略缓存预热覆盖率（对齐旧版「缓存预热」页签的只读统计）。"""
    thumb_dir = os.path.join(PROJECT_ROOT, "cache", "pdf_thumbs")
    done_flags = 0
    page_images = 0
    if os.path.isdir(thumb_dir):
        for name in os.listdir(thumb_dir):
            if name.endswith("_done.flag"):
                done_flags += 1
            elif name.endswith(".jpg"):
                page_images += 1

    pdf_records = 0
    if os.path.exists(get_db_path()):
        with connect() as conn:
            pdf_records = conn.execute(
                "SELECT COUNT(*) FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''"
            ).fetchone()[0]
    coverage = round(done_flags / pdf_records * 100, 1) if pdf_records else 0.0
    return {
        "pdf_records": pdf_records,
        "warm_units": done_flags,
        "page_images": page_images,
        "coverage": coverage,
        "cache_dir": thumb_dir,
    }


def get_analytics() -> Dict[str, Any]:
    """统计大盘数据，复刻 viewer.py 的 get_dashboard_analytics_data 查询语义。"""
    db_path = get_db_path()
    if not os.path.exists(db_path):
        return {}
    with connect() as conn:
        total_records = conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        has_pdf_records = conn.execute(
            "SELECT COUNT(*) FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''"
        ).fetchone()[0]

        source_data = [
            {"source": row[0] or "未知", "count": row[1]}
            for row in conn.execute(
                "SELECT source, COUNT(*) AS cnt FROM resources GROUP BY source ORDER BY cnt DESC"
            )
        ]
        cat_data = [
            {"category": row[0], "count": row[1]}
            for row in conn.execute(
                "SELECT category, COUNT(*) AS cnt FROM resources "
                "WHERE category IS NOT NULL AND category != '' "
                "GROUP BY category ORDER BY cnt DESC LIMIT 12"
            )
        ]
        timeline_rows = conn.execute(
            "SELECT substr(publish_time, 1, 7) AS ym, COUNT(*) AS cnt "
            "FROM resources "
            "WHERE publish_time IS NOT NULL AND publish_time != '' "
            "AND publish_time != 'Unknown' AND length(publish_time) >= 7 "
            "GROUP BY ym ORDER BY ym DESC LIMIT 16"
        ).fetchall()
        timeline_data = [
            {"month": row[0], "count": row[1]} for row in reversed(timeline_rows)
        ]
        matrix_data = []
        for row in conn.execute(
            "SELECT source, COUNT(*) AS total, "
            "SUM(CASE WHEN pdf_path IS NOT NULL AND pdf_path != '' THEN 1 ELSE 0 END) AS pdf_count "
            "FROM resources GROUP BY source ORDER BY total DESC"
        ):
            src = row[0] or "未知"
            total = row[1]
            pdfs = row[2] or 0
            matrix_data.append({
                "source": src,
                "total": total,
                "pdf_count": pdfs,
                "rate": round(pdfs / total * 100, 1) if total else 0.0,
            })

        categories_count = conn.execute(
            "SELECT COUNT(DISTINCT category) FROM resources "
            "WHERE category IS NOT NULL AND category != ''"
        ).fetchone()[0]

    db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 1)
    return {
        "total_records": total_records,
        "has_pdf_records": has_pdf_records,
        "pdf_rate": round(has_pdf_records / total_records * 100, 1) if total_records else 0.0,
        "db_size_mb": db_size_mb,
        "sources_count": len(source_data),
        "categories_count": categories_count,
        "source_data": source_data,
        "cat_data": cat_data,
        "timeline_data": timeline_data,
        "matrix_data": matrix_data,
    }
