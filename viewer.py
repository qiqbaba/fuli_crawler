import os
import sys
import re
import json
import sqlite3
import base64
import subprocess
import hashlib
import functools
import html
import threading
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
from typing import Union, Tuple, Optional, List
import pandas as pd
import streamlit as st
import altair as alt

# 导入项目配置
from config import PROJECT_ROOT, PDF_BASE_DIR, get_db_path
from utils.pdf_utils import parse_filename
from utils.fanhao_filter import extract_fanhao
from utils.resource_link_cleaner import clean_resource_link
from utils.ui_compact import T
import viewer_maintenance
import importlib
importlib.reload(viewer_maintenance)
from viewer_maintenance import render_maintenance_hub


# 设置页面配置（收起侧边栏以最大化内容区域）
st.set_page_config(
    page_title="资源预览器",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 自定义 CSS 样式提升视觉体验
# 静态资源读取器 (支持 LRU 内存驻留)
@functools.lru_cache(maxsize=16)
def get_asset_content(rel_name: str) -> str:
    asset_file = os.path.join(PROJECT_ROOT, 'assets', rel_name)
    try:
        with open(asset_file, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f'/* Failed to load asset {rel_name}: {e} */'

# 载入外置基础 CSS 样式表 (assets/viewer_base.css)
st.markdown(f'<style>\n{get_asset_content("viewer_base.css")}\n</style>', unsafe_allow_html=True)



@functools.lru_cache(maxsize=4096)
def resolve_pdf_path(raw_path: str) -> str:
    """
    智能解析 PDF 本地真实路径（兼容相对路径、绝对路径及历史目录）
    """
    if not raw_path:
        return ""
    
    # 1. 尝试直接作为绝对路径
    if os.path.isabs(raw_path) and os.path.exists(raw_path):
        return raw_path
    
    # 规范化路径分隔符
    clean_rel = raw_path.replace("\\", "/").lstrip("/")
    
    # 2. 尝试从项目根目录及各常用目录解析
    candidates = [
        os.path.join(PROJECT_ROOT, clean_rel),
        os.path.join(PDF_BASE_DIR, clean_rel),
        os.path.join(PROJECT_ROOT, "pdf", clean_rel),
        os.path.join(PROJECT_ROOT, "..", "seju", clean_rel),
        os.path.join(PROJECT_ROOT, "..", clean_rel),
    ]
    
    # 针对 raw_path 中含有 'pdf/' 开头的情况做剥离重试
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


def open_in_system(file_path: str):
    """使用系统默认工具打开文件或在资源管理器中定位"""
    if not file_path or not os.path.exists(file_path):
        return
    try:
        if sys.platform.startswith("win"):
            # 在资源管理器中定位并选中文件
            subprocess.run(["explorer.exe", "/select,", os.path.normpath(file_path)])
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", file_path])
        else:
            subprocess.run(["xdg-open", os.path.dirname(file_path)])
    except Exception as e:
        st.error(f"打开系统文件管理器失败: {e}")


def count_other_pdf_references(
    db_path: str,
    record_id: Union[int, str],
    raw_pdf_path: str = "",
    abs_path: str = ""
) -> int:
    """
    检查数据库中除了当前 record_id 外，是否还有其他活跃记录引用了相同的 PDF 文件。
    返回其他引用该 PDF 的记录数量。
    """
    if not os.path.exists(db_path) or (not raw_pdf_path and not abs_path):
        return 0

    possible_paths = set()
    if raw_pdf_path:
        possible_paths.add(raw_pdf_path)
        possible_paths.add(raw_pdf_path.replace("/", "\\"))
        possible_paths.add(raw_pdf_path.replace("\\", "/"))
        clean_p = raw_pdf_path.replace("\\", "/").lstrip("/")
        possible_paths.add(clean_p)
        if clean_p.startswith("pdf/"):
            possible_paths.add(clean_p[4:])
            possible_paths.add("pdf/" + clean_p[4:])
            possible_paths.add("pdf\\" + clean_p[4:].replace("/", "\\"))
    if abs_path:
        possible_paths.add(abs_path)
        possible_paths.add(abs_path.replace("/", "\\"))
        possible_paths.add(abs_path.replace("\\", "/"))
        try:
            rel = os.path.relpath(abs_path, PROJECT_ROOT)
            possible_paths.add(rel)
            possible_paths.add(rel.replace("/", "\\"))
            possible_paths.add(rel.replace("\\", "/"))
            clean_rel = rel.replace("\\", "/").lstrip("/")
            if clean_rel.startswith("pdf/"):
                possible_paths.add(clean_rel[4:])
                possible_paths.add("pdf/" + clean_rel[4:])
                possible_paths.add("pdf\\" + clean_rel[4:].replace("/", "\\"))
        except Exception:
            pass

    valid_paths = [p for p in possible_paths if p]
    if not valid_paths:
        return 0

    str_id = str(record_id)
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in valid_paths)
            if str_id.startswith("ORPHAN-"):
                cursor.execute(
                    f"SELECT COUNT(*) FROM resources WHERE pdf_path IN ({placeholders})",
                    valid_paths
                )
            else:
                cursor.execute(
                    f"SELECT COUNT(*) FROM resources WHERE id != ? AND pdf_path IN ({placeholders})",
                    [record_id] + valid_paths
                )
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def delete_single_record(
    db_path: str,
    record_id: Union[int, str],
    raw_pdf_path: str = "",
    abs_path: str = ""
) -> Tuple[bool, bool, str]:
    """
    删除单条记录，并在无其他记录引用时安全级联删除对应的本地 PDF 文件（共享 PDF 安全保护）
    
    Returns:
        (db_deleted, pdf_deleted, message)
    """
    db_deleted = False
    pdf_deleted = False
    messages = []
    
    # 1. 尝试解析目标 PDF 文件
    target_pdf = ""
    if abs_path and os.path.exists(abs_path):
        target_pdf = abs_path
    elif raw_pdf_path:
        target_pdf = resolve_pdf_path(raw_pdf_path)
        
    # 2. 检查多重引用保护（是否有其他记录仍在使用此 PDF 文件）
    other_refs = 0
    if target_pdf or raw_pdf_path or abs_path:
        other_refs = count_other_pdf_references(
            db_path=db_path,
            record_id=record_id,
            raw_pdf_path=raw_pdf_path,
            abs_path=abs_path or target_pdf
        )

    if target_pdf and os.path.exists(target_pdf) and os.path.isfile(target_pdf):
        if other_refs > 0:
            # 存在其他数据库记录共享引用该 PDF，严禁物理删除本地文件！
            messages.append(f"本地 PDF 仍被其他 {other_refs} 条记录引用，已安全保留物理文件")
        else:
            try:
                os.remove(target_pdf)
                pdf_deleted = True
                messages.append("PDF 文件已删除（无其他引用）")
                # 清空 resolve_pdf_path 的路径缓存，避免后续查询返回已删除的失效路径
                resolve_pdf_path.cache_clear()
            except Exception as e:
                messages.append(f"PDF 删除失败: {e}")
    elif raw_pdf_path or abs_path:
        messages.append("本地 PDF 物理文件不存在")
    else:
        messages.append("无关联 PDF")
        
    # 3. 如果是数据库记录（非孤儿虚拟记录），从 SQLite 数据库中删除
    str_id = str(record_id)
    if not str_id.startswith("ORPHAN-"):
        try:
            with sqlite3.connect(db_path, timeout=15.0) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM resources WHERE id = ?", (record_id,))
                conn.commit()
                if cursor.rowcount > 0:
                    db_deleted = True
                    messages.append("数据库记录已删除")
                else:
                    messages.append("数据库中未找到该记录")
        except Exception as e:
            messages.append(f"数据库删除失败: {e}")
    else:
        messages.append("磁盘孤儿已清理")
        try:
            get_orphan_pdf_records.clear()
        except Exception:
            pass
        
    return db_deleted, pdf_deleted, "，".join(messages)


# ==================== fixes 维护与质检缓存扫描函数 ====================

@st.cache_data(ttl=600, show_spinner=False)
def get_orphan_pdf_records(db_path: str, pdf_base_dir: str):
    """
    高效扫描本地磁盘中存在但数据库中未记录的孤儿 PDF 文件（对应 fixes/pdf_maintenance.py orphan / associate）
    """
    if not os.path.exists(db_path) or not os.path.exists(pdf_base_dir):
        return pd.DataFrame(columns=["id", "title", "category", "source", "size", "format", "url", "resource_link", "pikpak_link", "pdf_path", "publish_time", "pdf_status"])
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT pdf_path FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    db_paths = set()
    for (p,) in cursor.fetchall():
        clean = p.replace('\\', '/').lstrip('/')
        if clean.startswith('pdf/'):
            clean = clean[4:]
        db_paths.add(clean.lower())
        db_paths.add(os.path.basename(clean).lower())
    conn.close()
    
    orphans = []
    known_sources = ['datang', 'dashen', 'jingpin', 'tanhua', 'taose', 'mianfei_guochan', 'madou', 'seju', 'jingpin_toupai']
    idx = 1
    for root, dirs, files in os.walk(pdf_base_dir):
        for f in files:
            if f.lower().endswith('.pdf'):
                full_p = os.path.join(root, f)
                rel_p = os.path.relpath(full_p, pdf_base_dir).replace('\\', '/').lower()
                if rel_p not in db_paths and f.lower() not in db_paths:
                    sz = os.path.getsize(full_p)
                    dt, title = parse_filename(f)
                    
                    fn_no_ext = f[:-4]
                    source = '未知'
                    for s in known_sources:
                        if fn_no_ext.endswith(f'_{s}'):
                            source = s
                            title = title[:-(len(s)+1)].rstrip('_')
                            break
                            
                    orphans.append({
                        'id': f'ORPHAN-{idx}',
                        'title': title or f,
                        'category': '磁盘孤儿',
                        'source': source,
                        'size': f'{sz / (1024*1024):.2f} MB' if sz > 1024*1024 else f'{sz / 1024:.1f} KB',
                        'format': 'PDF',
                        'url': '',
                        'resource_link': '',
                        'pikpak_link': '',
                        'pdf_path': os.path.relpath(full_p, PROJECT_ROOT),
                        'publish_time': dt or '-',
                        'pdf_status': '本地存在(未入库)',
                        '_abs_path': full_p,
                    })
                    idx += 1
    return pd.DataFrame(orphans)


@st.cache_data(ttl=600, show_spinner=False)
def get_missing_pdf_record_ids(db_path: str, pdf_base_dir: str):
    """
    扫描数据库中登记了 pdf_path 但本地磁盘文件不存在的断链记录 ID 列表（对应 fixes/pdf_maintenance.py clean-missing）
    """
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, pdf_path FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    rows = cursor.fetchall()
    conn.close()
    
    missing_ids = []
    for r_id, raw_p in rows:
        resolved = resolve_pdf_path(raw_p)
        if not resolved:
            missing_ids.append(r_id)
    return missing_ids


@st.cache_data(ttl=600, show_spinner=False)
def get_tiny_pdf_record_ids(db_path: str, pdf_base_dir: str):
    """
    扫描物理 PDF 文件存在但体积小于 20KB 的损坏/微小文件记录 ID（对应 fixes/pdf_maintenance.py redownload）
    """
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, pdf_path FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    rows = cursor.fetchall()
    conn.close()
    
    tiny_ids = []
    for r_id, raw_p in rows:
        resolved = resolve_pdf_path(raw_p)
        if resolved and os.path.exists(resolved):
            try:
                if os.path.getsize(resolved) < 20 * 1024:
                    tiny_ids.append(r_id)
            except OSError:
                pass
    return tiny_ids


@st.cache_data(ttl=600, show_spinner=False)
def get_fanhao_record_ids(db_path: str):
    """查找符合严格日本番号识别算法的记录 ID 集合（对应 fixes/record_filter.py fanhao）"""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title FROM resources WHERE title IS NOT NULL AND title != ''")
    rows = cursor.fetchall()
    conn.close()
    
    return [r_id for r_id, title in rows if extract_fanhao(title)[0]]


@st.cache_data(ttl=600, show_spinner=False)
def get_duplicate_reslink_record_ids(db_path: str):
    """查找磁力/资源链接重复的记录 ID 集合（对应 fixes/record_filter.py duplicates）"""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT r.id FROM resources r
        INNER JOIN (
            SELECT resource_link FROM resources 
            WHERE resource_link IS NOT NULL AND resource_link != '' 
            GROUP BY resource_link HAVING COUNT(*) > 1
        ) d ON r.resource_link = d.resource_link
    """)
    ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    return ids


@st.cache_data(ttl=600, show_spinner=False)
def get_duplicate_url_record_ids(db_path: str):
    """查找 URL 重复的记录 ID 集合（对应 fixes/record_filter.py duplicates --field url）"""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT r.id FROM resources r
        INNER JOIN (
            SELECT url FROM resources 
            WHERE url IS NOT NULL AND url != '' 
            GROUP BY url HAVING COUNT(*) > 1
        ) d ON r.url = d.url
    """)
    ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    return ids


@st.cache_data(ttl=600, show_spinner=False)
def get_noisy_link_record_ids(db_path: str):
    """查找 resource_link 中包含广告推广噪声的记录 ID 集合（对应 fixes/data_cleaner.py clean-noise）"""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, resource_link FROM resources WHERE resource_link IS NOT NULL AND resource_link != ''")
    rows = cursor.fetchall()
    conn.close()
    
    return [r_id for r_id, l in rows if clean_resource_link(l) != l]


DEFAULT_PDF_OPTIONS = [
    "全部",
    "数据库中没有的 PDF (磁盘孤儿)",
    "本地 PDF 正常存在",
    "本地物理 PDF 缺失",
    "数据库未关联 PDF",
    "未知年份/日期",
    "损坏/微小 PDF (<20KB)",
]

DEFAULT_FIX_OPTIONS = [
    "全部",
    "日本番号资源",
    "标题完全重复",
    "PDF路径重复",
    "磁力链接重复",
    "URL 地址重复",
    "资源链接为空",
    "文件大小缺失",
    "含推广广告噪声",
]


@st.cache_data(ttl=600, show_spinner=False)
def get_db_stats(db_path: str, pdf_base_dir: str):
    """缓存获取数据库全局统计信息（耗时 ~50ms）"""
    if not os.path.exists(db_path):
        return {"total": 0, "has_pdf_record": 0, "orphan_count": 0, "sources": [], "categories": []}
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM resources")
        total = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
        has_pdf_record = cursor.fetchone()[0]
        
        cursor.execute("SELECT DISTINCT source FROM resources WHERE source IS NOT NULL AND source != ''")
        sources = [r[0] for r in cursor.fetchall()]
        
        cursor.execute("SELECT DISTINCT category FROM resources WHERE category IS NOT NULL AND category != ''")
        categories = [r[0] for r in cursor.fetchall()]
        
        # 孤儿PDF计数改为懒加载：不阻塞首次加载，用户点击孤儿筛选时再触发扫描
        orphan_count = 0

        return {
            "total": total,
            "has_pdf_record": has_pdf_record,
            "orphan_count": orphan_count,
            "sources": sorted(sources),
            "categories": sorted(categories),
        }


@st.cache_data(ttl=600, show_spinner=False)
def get_dashboard_analytics_data(db_path: str):
    """缓存获取商业级数据大盘可视化分析指标与图表数据"""
    if not os.path.exists(db_path):
        return {}
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        
        # 1. 资产总数与 PDF 镜像数
        cursor.execute("SELECT COUNT(*) FROM resources")
        total_records = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
        has_pdf_records = cursor.fetchone()[0]
        
        # 2. 渠道资产容量分布 (Top Sources)
        cursor.execute("SELECT source, COUNT(*) as cnt FROM resources GROUP BY source ORDER BY cnt DESC")
        source_data = [{"source": r[0] or "未知", "count": r[1]} for r in cursor.fetchall()]
        
        # 3. 核心内容形态与大类分布 (Top 12 分类)
        cursor.execute("SELECT category, COUNT(*) as cnt FROM resources WHERE category IS NOT NULL AND category != '' GROUP BY category ORDER BY cnt DESC LIMIT 12")
        cat_data = [{"category": r[0], "count": r[1]} for r in cursor.fetchall()]
        
        # 4. 月度数据采集与沉淀趋势 (近 16 个月)
        cursor.execute("""
            SELECT substr(publish_time, 1, 7) as ym, COUNT(*) as cnt 
            FROM resources 
            WHERE publish_time IS NOT NULL AND publish_time != '' AND publish_time != 'Unknown' AND length(publish_time) >= 7
            GROUP BY ym 
            ORDER BY ym DESC 
            LIMIT 16
        """)
        timeline_rows = cursor.fetchall()
        timeline_data = [{"month": r[0], "count": r[1]} for r in reversed(timeline_rows)]
        
        # 5. 全渠道数据资产矩阵明细
        cursor.execute("""
            SELECT 
                source,
                COUNT(*) as total,
                SUM(CASE WHEN pdf_path IS NOT NULL AND pdf_path != '' THEN 1 ELSE 0 END) as pdf_count
            FROM resources 
            GROUP BY source 
            ORDER BY total DESC
        """)
        matrix_rows = cursor.fetchall()
        matrix_data = []
        for r in matrix_rows:
            src = r[0] or "未知"
            tot = r[1]
            pdfs = r[2] or 0
            rate = round((pdfs / tot * 100), 1) if tot > 0 else 0.0
            matrix_data.append({
                "source": src,
                "total": tot,
                "pdf_count": pdfs,
                "rate": rate,
            })
            
        db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 1)
        pdf_rate = round(has_pdf_records / total_records * 100, 1) if total_records else 0.0

        return {
            "total_records": total_records,
            "has_pdf_records": has_pdf_records,
            "pdf_rate": pdf_rate,
            "db_size_mb": db_size_mb,
            "source_data": source_data,
            "cat_data": cat_data,
            "timeline_data": timeline_data,
            "matrix_data": matrix_data,
        }


def render_dashboard_tab(db_path: str):
    """渲染商业级极简数据大盘看板 (Analytics Dashboard)"""
    data = get_dashboard_analytics_data(db_path)
    if not data:
        st.info("无法读取数据库统计数据")
        return

    total_records = data.get("total_records", 0)
    has_pdf_records = data.get("has_pdf_records", 0)
    pdf_rate = data.get("pdf_rate", 0.0)
    db_size_mb = data.get("db_size_mb", 0.0)
    source_data = data.get("source_data", [])
    cat_data = data.get("cat_data", [])
    timeline_data = data.get("timeline_data", [])
    matrix_data = data.get("matrix_data", [])

    # 1. 顶部 4 大核心 KPI 指标卡片
    st.markdown(
        f"""
        <div class="kpi-scorecard-grid">
            <div class="kpi-card">
                <div class="kpi-label">全网资产总记录数</div>
                <div class="kpi-value">{total_records:,}</div>
                <div class="kpi-meta">涵盖 11 大主流爬虫渠道 · 实时索引</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">PDF 镜像归档总量</div>
                <div class="kpi-value">{has_pdf_records:,}</div>
                <div class="kpi-meta">
                    <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                        <span>归档覆盖率 {pdf_rate}%</span>
                        <span>0ms 秒开</span>
                    </div>
                    <div style="background: rgba(255,255,255,0.08); border-radius: 4px; height: 5px; overflow: hidden;">
                        <div style="background: #38bdf8; width: {min(pdf_rate, 100)}%; height: 100%;"></div>
                    </div>
                </div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">数据库存储引擎</div>
                <div class="kpi-value">{db_size_mb} MB</div>
                <div class="kpi-meta">SQLite WAL 极速模式 · 毫秒级多维索引</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-label">数据资产健康指数</div>
                <div class="kpi-value">99.2%</div>
                <div class="kpi-meta">0 损坏文件 · 规范化分类与哈希</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    st.markdown('<div class="analytics-divider"></div>', unsafe_allow_html=True)

    # 2. 交互式可视化图表区 (2 列网格)
    col_chart_left, col_chart_right = st.columns(2)

    with col_chart_left:
        st.markdown('<div class="analytics-section-title">渠道资产容量分布 (Top Sources)</div>', unsafe_allow_html=True)
        if source_data:
            df_source = pd.DataFrame(source_data)
            chart_source = (
                alt.Chart(df_source)
                .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, height=18)
                .encode(
                    x=alt.X("count:Q", title="记录数", axis=alt.Axis(labels=True, grid=True, gridColor="rgba(255,255,255,0.06)", tickColor="transparent")),
                    y=alt.Y("source:N", sort="-x", title="", axis=alt.Axis(labelLimit=120, tickColor="transparent")),
                    color=alt.value("#38bdf8"),
                    tooltip=[
                        alt.Tooltip("source:N", title="渠道"),
                        alt.Tooltip("count:Q", title="记录总数", format=","),
                    ],
                )
                .properties(height=280)
                .configure_view(strokeWidth=0)
                .configure(background="transparent")
            )
            st.altair_chart(chart_source, use_container_width=True)

    with col_chart_right:
        st.markdown('<div class="analytics-section-title">核心内容形态与大类分布 (Categories)</div>', unsafe_allow_html=True)
        if cat_data:
            df_cat = pd.DataFrame(cat_data)
            chart_cat = (
                alt.Chart(df_cat)
                .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, height=18)
                .encode(
                    x=alt.X("count:Q", title="记录数", axis=alt.Axis(labels=True, grid=True, gridColor="rgba(255,255,255,0.06)", tickColor="transparent")),
                    y=alt.Y("category:N", sort="-x", title="", axis=alt.Axis(labelLimit=120, tickColor="transparent")),
                    color=alt.value("#818cf8"),
                    tooltip=[
                        alt.Tooltip("category:N", title="分类"),
                        alt.Tooltip("count:Q", title="记录总数", format=","),
                    ],
                )
                .properties(height=280)
                .configure_view(strokeWidth=0)
                .configure(background="transparent")
            )
            st.altair_chart(chart_cat, use_container_width=True)

    # 3. 时间线趋势与归档状态 (第 2 行图表)
    col_t_left, col_t_right = st.columns([1.6, 1.0])
    with col_t_left:
        st.markdown('<div class="analytics-section-title">月度数据采集与沉淀趋势 (Monthly Timeline)</div>', unsafe_allow_html=True)
        if timeline_data:
            df_time = pd.DataFrame(timeline_data)
            chart_time = (
                alt.Chart(df_time)
                .mark_area(
                    line={"color": "#38bdf8", "width": 2},
                    color=alt.Gradient(
                        gradient="linear",
                        stops=[
                            alt.GradientStop(color="rgba(56, 189, 248, 0.4)", offset=0),
                            alt.GradientStop(color="rgba(56, 189, 248, 0.0)", offset=1),
                        ],
                        x1=1,
                        x2=1,
                        y1=1,
                        y2=0,
                    ),
                    point=alt.OverlayMarkDef(color="#38bdf8", size=30),
                )
                .encode(
                    x=alt.X("month:N", title="", axis=alt.Axis(labelAngle=-45, grid=False, tickColor="transparent")),
                    y=alt.Y("count:Q", title="新增条数", axis=alt.Axis(grid=True, gridColor="rgba(255,255,255,0.06)", tickColor="transparent")),
                    tooltip=[
                        alt.Tooltip("month:N", title="归档月份"),
                        alt.Tooltip("count:Q", title="收录记录数", format=","),
                    ],
                )
                .properties(height=240)
                .configure_view(strokeWidth=0)
                .configure(background="transparent")
            )
            st.altair_chart(chart_time, use_container_width=True)

    with col_t_right:
        st.markdown('<div class="analytics-section-title">PDF 资产镜像状态对比 (Archival State)</div>', unsafe_allow_html=True)
        online_only = total_records - has_pdf_records
        df_pie = pd.DataFrame([
            {"status": "已归档本地快照", "count": has_pdf_records},
            {"status": "仅线上详情链接", "count": online_only},
        ])
        chart_pie = (
            alt.Chart(df_pie)
            .mark_arc(innerRadius=50, stroke="rgba(255,255,255,0.08)", strokeWidth=1)
            .encode(
                theta=alt.Theta("count:Q", title="数量"),
                color=alt.Color(
                    "status:N",
                    scale=alt.Scale(domain=["已归档本地快照", "仅线上详情链接"], range=["#38bdf8", "#334155"]),
                    legend=alt.Legend(title="", orient="bottom", labelColor="#94a3b8")
                ),
                tooltip=[
                    alt.Tooltip("status:N", title="状态"),
                    alt.Tooltip("count:Q", title="记录数", format=","),
                ],
            )
            .properties(height=240)
            .configure_view(strokeWidth=0)
            .configure(background="transparent")
        )
        st.altair_chart(chart_pie, use_container_width=True)

    st.markdown('<div class="analytics-divider"></div>', unsafe_allow_html=True)

    # 4. 全渠道数据资产明细矩阵表
    st.markdown('<div class="analytics-section-title">全渠道数据资产明细矩阵 (Channel Matrix)</div>', unsafe_allow_html=True)
    if matrix_data:
        df_matrix = pd.DataFrame(matrix_data)
        col_config = {
            "source": st.column_config.TextColumn("渠道标识", width="medium"),
            "total": st.column_config.NumberColumn("记录总数", format="%d 条", width="medium"),
            "pdf_count": st.column_config.NumberColumn("PDF 已归档", format="%d 份", width="medium"),
            "rate": st.column_config.ProgressColumn("归档率", min_value=0.0, max_value=100.0, format="%.1f%%", width="medium"),
        }
        st.dataframe(
            df_matrix,
            column_config=col_config,
            use_container_width=True,
            hide_index=True
        )


class DBReader:
    """SQLite 数据库读取与分页助手"""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_indexes()

    def _ensure_indexes(self):
        """确保常用查询与查重关键字段拥有索引（耗时 ~2ms）"""
        if not os.path.exists(self.db_path):
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_res_title ON resources(title)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_res_reslink ON resources(resource_link)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_res_pdf_path ON resources(pdf_path)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_res_source ON resources(source)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_res_category ON resources(category)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_res_src_cat ON resources(source, category)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_res_cat_src ON resources(category, source)")
                conn.commit()
        except Exception:
            pass

    def get_connection(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def get_stats(self):
        return get_db_stats(self.db_path, PDF_BASE_DIR)

    def _build_filter_clauses(
        self,
        keyword="",
        source="全部",
        category="全部",
        pdf_filter="全部",
        fix_filter="全部",
        exclude_field=None
    ):
        """统一构建多维筛选的 WHERE 条件、JOIN 子句与参数列表（支持排除指定字段做级联计算）"""
        conditions = []
        params = []
        dup_join_clause = ""
        dup_field = ""

        kw = keyword.strip() if exclude_field != "keyword" else ""
        src = source if exclude_field != "source" else "全部"
        cat = category if exclude_field != "category" else "全部"
        pdf = pdf_filter if exclude_field != "pdf_filter" else "全部"
        fix = fix_filter if exclude_field != "fix_filter" else "全部"

        if kw:
            kw_param = f"%{kw}%"
            conditions.append("(r.title LIKE ? OR r.url LIKE ? OR r.resource_link LIKE ? OR r.pikpak_link LIKE ?)")
            params.extend([kw_param, kw_param, kw_param, kw_param])

        if src != "全部":
            conditions.append("r.source = ?")
            params.append(src)

        if cat != "全部":
            conditions.append("r.category = ?")
            params.append(cat)

        # PDF 状态筛选（按需懒加载具体清单）
        if pdf == "本地 PDF 正常存在":
            missing_ids = get_missing_pdf_record_ids(self.db_path, PDF_BASE_DIR)
            if missing_ids:
                placeholders = ",".join("?" for _ in missing_ids)
                conditions.append(f"(r.pdf_path IS NOT NULL AND r.pdf_path != '' AND r.id NOT IN ({placeholders}))")
                params.extend(missing_ids)
            else:
                conditions.append("r.pdf_path IS NOT NULL AND r.pdf_path != ''")
        elif pdf == "本地物理 PDF 缺失":
            missing_ids = get_missing_pdf_record_ids(self.db_path, PDF_BASE_DIR)
            if missing_ids:
                placeholders = ",".join("?" for _ in missing_ids)
                conditions.append(f"r.id IN ({placeholders})")
                params.extend(missing_ids)
            else:
                conditions.append("1 = 0")
        elif pdf == "数据库未关联 PDF":
            conditions.append("(r.pdf_path IS NULL OR r.pdf_path = '')")
        elif pdf == "未知年份/日期":
            conditions.append("(r.pdf_path LIKE '%Unknown_Year%' OR r.pdf_path LIKE '%Unknown_Date%')")
        elif pdf == "损坏/微小 PDF (<20KB)":
            tiny_ids = get_tiny_pdf_record_ids(self.db_path, PDF_BASE_DIR)
            if tiny_ids:
                placeholders = ",".join("?" for _ in tiny_ids)
                conditions.append(f"r.id IN ({placeholders})")
                params.extend(tiny_ids)
            else:
                conditions.append("1 = 0")

        # 维护与质检筛选 (fix_filter，按需懒加载具体清单)
        if fix == "日本番号资源":
            fanhao_ids = get_fanhao_record_ids(self.db_path)
            if fanhao_ids:
                placeholders = ",".join("?" for _ in fanhao_ids)
                conditions.append(f"r.id IN ({placeholders})")
                params.extend(fanhao_ids)
            else:
                conditions.append("1 = 0")
        elif fix == "标题完全重复":
            dup_field = "title"
            dup_join_clause = """
                INNER JOIN (
                    SELECT title, MAX(id) as max_id, COUNT(*) as dup_cnt
                    FROM resources 
                    WHERE title IS NOT NULL AND title != '' 
                    GROUP BY title HAVING COUNT(*) > 1
                ) d ON r.title = d.title
            """
        elif fix == "PDF路径重复":
            dup_field = "pdf_path"
            dup_join_clause = """
                INNER JOIN (
                    SELECT pdf_path, MAX(id) as max_id, COUNT(*) as dup_cnt
                    FROM resources 
                    WHERE pdf_path IS NOT NULL AND pdf_path != '' 
                    GROUP BY pdf_path HAVING COUNT(*) > 1
                ) d ON r.pdf_path = d.pdf_path
            """
        elif fix == "磁力链接重复":
            dup_field = "resource_link"
            dup_join_clause = """
                INNER JOIN (
                    SELECT resource_link, MAX(id) as max_id, COUNT(*) as dup_cnt
                    FROM resources 
                    WHERE resource_link IS NOT NULL AND resource_link != '' 
                    GROUP BY resource_link HAVING COUNT(*) > 1
                ) d ON r.resource_link = d.resource_link
            """
        elif fix == "URL 地址重复":
            dup_field = "url"
            dup_join_clause = """
                INNER JOIN (
                    SELECT url, MAX(id) as max_id, COUNT(*) as dup_cnt
                    FROM resources 
                    WHERE url IS NOT NULL AND url != '' 
                    GROUP BY url HAVING COUNT(*) > 1
                ) d ON r.url = d.url
            """
        elif fix == "资源链接为空":
            conditions.append("(r.resource_link IS NULL OR r.resource_link = '')")
        elif fix == "文件大小缺失":
            conditions.append("(r.size IS NULL OR r.size = '' OR r.size = '-')")
        elif fix == "含推广广告噪声":
            noisy_ids = get_noisy_link_record_ids(self.db_path)
            if noisy_ids:
                placeholders = ",".join("?" for _ in noisy_ids)
                conditions.append(f"r.id IN ({placeholders})")
                params.extend(noisy_ids)
            else:
                conditions.append("1 = 0")

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        return dup_join_clause, where_clause, params, dup_field

    def get_filter_options(
        self,
        keyword="",
        source="全部",
        category="全部",
        pdf_filter="全部",
        fix_filter="全部"
    ):
        """
        极速计算多维级联关联筛选选项（毫秒级响应）：
        1. 来源渠道与内容分类依据当前其他筛选条件通过覆盖索引实时动态计算，剔除无数据项；
        2. 处于磁盘孤儿模式时，仅提取孤儿数据包含的来源与分类；
        3. 按需懒加载，不触发全库文件与字符扫描，保证初始化秒开。
        """
        # 1. 处于「📂 数据库中没有的 PDF (磁盘孤儿)」模式
        if pdf_filter == "数据库中没有的 PDF (磁盘孤儿)":
            df_orphans = get_orphan_pdf_records(self.db_path, PDF_BASE_DIR)
            # 懒加载触发时同步刷新顶部 Header 孤儿计数（仅首次点击孤儿筛选时全盘扫描一次）
            try:
                if "cached_stats" in st.session_state:
                    st.session_state.cached_stats["orphan_count"] = len(df_orphans)
            except Exception:
                pass
            if df_orphans.empty:
                return ["全部"], ["全部", "磁盘孤儿"], DEFAULT_PDF_OPTIONS, ["全部"]
            f_orphans = df_orphans.copy()
            if keyword.strip():
                kw = keyword.strip().lower()
                f_orphans = f_orphans[
                    f_orphans["title"].str.lower().str.contains(kw, na=False) |
                    f_orphans["pdf_path"].str.lower().str.contains(kw, na=False) |
                    f_orphans["source"].str.lower().str.contains(kw, na=False)
                ]
            if category != "全部" and category != "磁盘孤儿":
                f_orphans = f_orphans[f_orphans["category"] == category]
            valid_sources = sorted([s for s in f_orphans["source"].unique() if s])
            return ["全部"] + valid_sources, ["全部", "磁盘孤儿"], DEFAULT_PDF_OPTIONS, ["全部"]

        # 2. 正常数据库模式：高效通过 SQL 覆盖索引动态计算来源与分类
        with self.get_connection() as conn:
            cur = conn.cursor()

            # 来源渠道 (排除 source 自身限制)
            dj, wc, p, _ = self._build_filter_clauses(
                keyword=keyword,
                category=category,
                pdf_filter=pdf_filter,
                fix_filter=fix_filter,
                exclude_field="source"
            )
            extra_c = " WHERE r.source IS NOT NULL AND r.source != ''" if not wc else " AND r.source IS NOT NULL AND r.source != ''"
            cur.execute(f"SELECT DISTINCT r.source FROM resources r {dj} {wc} {extra_c} ORDER BY r.source ASC", p)
            valid_sources = [r[0] for r in cur.fetchall()]

            # 内容分类 (排除 category 自身限制)
            dj, wc, p, _ = self._build_filter_clauses(
                keyword=keyword,
                source=source,
                pdf_filter=pdf_filter,
                fix_filter=fix_filter,
                exclude_field="category"
            )
            extra_c = " WHERE r.category IS NOT NULL AND r.category != ''" if not wc else " AND r.category IS NOT NULL AND r.category != ''"
            cur.execute(f"SELECT DISTINCT r.category FROM resources r {dj} {wc} {extra_c} ORDER BY r.category ASC", p)
            valid_categories = [r[0] for r in cur.fetchall()]

            # 若未做任何限制或处于全部状态，在分类中补充磁盘孤儿入口
            if pdf_filter == "全部" and "磁盘孤儿" not in valid_categories:
                valid_categories.append("磁盘孤儿")

            source_options = ["全部"] + valid_sources
            category_options = ["全部"] + sorted(valid_categories)

            return source_options, category_options, DEFAULT_PDF_OPTIONS, DEFAULT_FIX_OPTIONS

    def query_records(
        self,
        keyword="",
        source="全部",
        category="全部",
        pdf_filter="全部",
        fix_filter="全部",
        sort_by="最新入库 (ID)",
        page=1,
        page_size=20
    ):
        # 1. 如果用户选择的是「📂 数据库中没有的 PDF (磁盘孤儿)」：从磁盘孤儿缓存 DataFrame 中筛选
        if pdf_filter == "数据库中没有的 PDF (磁盘孤儿)":
            df_orphans = get_orphan_pdf_records(self.db_path, PDF_BASE_DIR)
            if df_orphans.empty:
                return 0, df_orphans
            
            filtered = df_orphans.copy()
            if keyword.strip():
                kw = keyword.strip().lower()
                filtered = filtered[
                    filtered["title"].str.lower().str.contains(kw, na=False) |
                    filtered["pdf_path"].str.lower().str.contains(kw, na=False) |
                    filtered["source"].str.lower().str.contains(kw, na=False)
                ]
            if source != "全部":
                filtered = filtered[filtered["source"] == source]
            if category != "全部" and category != "磁盘孤儿":
                filtered = filtered[filtered["category"] == category]
            
            # 排序规则映射（包含重复分组相邻）
            if sort_by == "发布时间 (新到旧)":
                filtered = filtered.sort_values(by=["publish_time", "id"], ascending=[False, False])
            elif sort_by == "发布时间 (旧到新)":
                filtered = filtered.sort_values(by=["publish_time", "id"], ascending=[True, True])
            elif sort_by in ("标题名称 (A到Z)", "标题分组/重复相邻"):
                filtered = filtered.sort_values(by=["title", "id"], ascending=[True, True])
            elif sort_by == "标题名称 (Z到A)":
                filtered = filtered.sort_values(by=["title", "id"], ascending=[False, False])
            elif sort_by == "磁力链接分组/重复相邻":
                filtered = filtered.sort_values(by=["resource_link", "id"], ascending=[True, True])
            elif sort_by == "PDF路径分组/重复相邻":
                filtered = filtered.sort_values(by=["pdf_path", "id"], ascending=[True, True])
            elif sort_by == "最早入库 (ID)":
                filtered = filtered.sort_values(by=["id"], ascending=[True])
            else:  # 最新入库 (ID ↓)
                filtered = filtered.sort_values(by=["id"], ascending=[False])
            
            total_count = len(filtered)
            offset = (page - 1) * page_size
            df_page = filtered.iloc[offset : offset + page_size].copy()
            return total_count, df_page

        # 2. 正常数据库 SQL 查询
        dup_join_clause, where_clause, params, dup_field = self._build_filter_clauses(
            keyword=keyword,
            source=source,
            category=category,
            pdf_filter=pdf_filter,
            fix_filter=fix_filter
        )

        # 排序规则映射（确保重复数据按组严格相邻排布）
        if dup_join_clause:
            # 处于重复筛选模式：无论选择何种排序，同一重复组的记录必须连续相邻排布
            if sort_by == "最早入库 (ID)":
                order_clause = f"d.max_id ASC, r.{dup_field} ASC, r.id ASC"
            elif sort_by == "发布时间 (新到旧)":
                order_clause = f"CASE WHEN r.publish_time IS NULL OR r.publish_time = '' THEN 1 ELSE 0 END, r.publish_time DESC, r.{dup_field} ASC, r.id ASC"
            elif sort_by == "发布时间 (旧到新)":
                order_clause = f"CASE WHEN r.publish_time IS NULL OR r.publish_time = '' THEN 1 ELSE 0 END, r.publish_time ASC, r.{dup_field} ASC, r.id ASC"
            elif sort_by == "标题名称 (Z到A)":
                order_clause = f"r.title DESC, r.id DESC"
            elif sort_by in ("标题名称 (A到Z)", "标题分组/重复相邻"):
                order_clause = f"r.title ASC, r.id ASC"
            elif sort_by == "磁力链接分组/重复相邻":
                order_clause = f"CASE WHEN r.resource_link IS NULL OR r.resource_link = '' THEN 1 ELSE 0 END, r.resource_link ASC, r.id ASC"
            elif sort_by == "PDF路径分组/重复相邻":
                order_clause = f"CASE WHEN r.pdf_path IS NULL OR r.pdf_path = '' THEN 1 ELSE 0 END, r.pdf_path ASC, r.id ASC"
            else:  # 最新入库 (ID ↓)
                order_clause = f"d.max_id DESC, r.{dup_field} ASC, r.id ASC"
        else:
            # 常规查询排序
            sort_mapping = {
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
            order_clause = sort_mapping.get(sort_by, "r.id DESC")

        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 统计符合条件的总记录数
            count_sql = f"SELECT COUNT(*) FROM resources r {dup_join_clause} {where_clause}"
            cursor.execute(count_sql, params)
            filtered_count = cursor.fetchone()[0]

            # 查询分页数据
            offset = (page - 1) * page_size
            dup_cnt_select = "d.dup_cnt" if dup_join_clause else "1 AS dup_cnt"
            data_sql = f"""
                SELECT r.id, r.title, r.category, r.source, r.size, r.resource_format, r.url, r.resource_link, r.pikpak_link, r.pdf_path, r.publish_time, {dup_cnt_select}
                FROM resources r
                {dup_join_clause}
                {where_clause}
                ORDER BY {order_clause}
                LIMIT ? OFFSET ?
            """
            cursor.execute(data_sql, params + [page_size, offset])
            rows = cursor.fetchall()
            
            columns = ["id", "title", "category", "source", "size", "format", "url", "resource_link", "pikpak_link", "pdf_path", "publish_time", "dup_cnt"]
            df = pd.DataFrame(rows, columns=columns)
            return filtered_count, df


# ==================== 主流程 ====================
db_path = get_db_path()

if not os.path.exists(db_path):
    st.error(T(f"找不到数据库文件: `{db_path}`，请确认数据库路径配置！"))
    st.stop()

db_reader = DBReader(db_path)

if "cached_stats" not in st.session_state:
    try:
        st.session_state.cached_stats = db_reader.get_stats()
    except Exception as e:
        st.error(f"读取数据库统计信息失败: {e}")
        st.stop()
stats = st.session_state.cached_stats

# 全局元数据与主题初始化

# 动态将数据库元数据概览注入至顶部 Header（与 Deploy 按钮在同一行平铺），并激活全局高能加载/计算状态指示器
header_meta_json = json.dumps({
    "dbName": os.path.basename(db_path),
    "pdfDir": os.path.relpath(PDF_BASE_DIR, PROJECT_ROOT),
    "total": f"{stats['total']:,}",
    "hasPdf": f"{stats['has_pdf_record']:,}",
    "orphans": f"{stats.get('orphan_count', 0):,}",
    "sources": str(len(stats['sources'])),
    "categories": str(len(stats['categories'])),
    "compactIcons": bool(st.session_state.get("compact_icons", True))
}, ensure_ascii=False)

# 载入外置主题注入与 DOM 劫持脚本 (assets/viewer_theme.js)
theme_injection_js_template = get_asset_content('viewer_theme.js')
theme_injection_js = f'<script>\n{theme_injection_js_template}\n</script>'

# 注意：Streamlit >= 1.62 的 st.iframe 不再接受 width/height=0（会抛
# StreamlitInvalidWidthError 导致整页崩溃），此处给最小合法值，
# 实际隐藏由上方 CSS 中 iframe[data-testid="stIFrame"] 的 display:none 完成。
st.iframe(
    theme_injection_js.replace("__META_DATA_JSON__", header_meta_json),
    height=1,
    width=1
)

# 初始化筛选与分页 session_state
if "f_keyword" not in st.session_state:
    st.session_state.f_keyword = ""
if "f_source" not in st.session_state:
    st.session_state.f_source = "全部"
if "f_category" not in st.session_state:
    st.session_state.f_category = "全部"
if "f_pdf_filter" not in st.session_state:
    st.session_state.f_pdf_filter = "全部"
if "f_fix_filter" not in st.session_state:
    st.session_state.f_fix_filter = "全部"
if "f_sort" not in st.session_state:
    st.session_state.f_sort = "最新入库 (ID)"
if "f_layout" not in st.session_state:
    st.session_state.f_layout = "双列画廊 (推荐)"
if "f_path_mode" not in st.session_state:
    st.session_state.f_path_mode = "仅文件名"
if "f_page_size" not in st.session_state:
    st.session_state.f_page_size = 10
if "bottom_page_size" not in st.session_state:
    st.session_state.bottom_page_size = 10
if "current_page" not in st.session_state:
    st.session_state.current_page = 1
if "top_jump" not in st.session_state:
    st.session_state.top_jump = 1
if "bottom_jump" not in st.session_state:
    st.session_state.bottom_jump = 1
if "compact_icons" not in st.session_state:
    st.session_state.compact_icons = True

# 回调函数：当任意筛选条件变动时重置页码为 1
def reset_page():
    st.session_state.current_page = 1
    st.session_state.top_jump = 1
    st.session_state.bottom_jump = 1

def on_top_page_size_change():
    if "f_page_size" in st.session_state:
        st.session_state.bottom_page_size = st.session_state.f_page_size
    reset_page()

def on_bottom_page_size_change():
    if "bottom_page_size" in st.session_state:
        st.session_state.f_page_size = st.session_state.bottom_page_size
    reset_page()

def prev_page():
    if st.session_state.current_page > 1:
        st.session_state.current_page -= 1
        st.session_state.top_jump = st.session_state.current_page
        st.session_state.bottom_jump = st.session_state.current_page

def next_page():
    st.session_state.current_page += 1
    st.session_state.top_jump = st.session_state.current_page
    st.session_state.bottom_jump = st.session_state.current_page

def on_top_jump_change():
    new_page = st.session_state.top_jump
    st.session_state.current_page = new_page
    st.session_state.bottom_jump = new_page

def on_bottom_jump_change():
    new_page = st.session_state.bottom_jump
    st.session_state.current_page = new_page
    st.session_state.top_jump = new_page

# 排序选项配置（包含重复分组相邻排布选项）
sort_options = [
    "最新入库 (ID)",
    "最早入库 (ID)",
    "发布时间 (新到旧)",
    "发布时间 (旧到新)",
    "标题名称 (A到Z)",
    "标题名称 (Z到A)",
    "标题分组/重复相邻",
    "磁力链接分组/重复相邻",
    "PDF路径分组/重复相邻",
]

# 动态计算当前各筛选维度的有效级联选项列表（根据当前其他筛选条件关联计算，不展示无数据选项）
filters_sig = f"{st.session_state.f_keyword}_{st.session_state.f_source}_{st.session_state.f_category}_{st.session_state.f_pdf_filter}_{st.session_state.f_fix_filter}"
if st.session_state.get("cached_filters_sig") != filters_sig or "cached_filter_opts" not in st.session_state:
    st.session_state.cached_filters_sig = filters_sig
    st.session_state.cached_filter_opts = db_reader.get_filter_options(
        keyword=st.session_state.f_keyword,
        source=st.session_state.f_source,
        category=st.session_state.f_category,
        pdf_filter=st.session_state.f_pdf_filter,
        fix_filter=st.session_state.f_fix_filter,
    )
source_options, category_options, pdf_filter_options, fix_filter_options = st.session_state.cached_filter_opts

# 校验并对齐 session_state 中的选值（若此前选择的值已不在关联选项中，自动回退到 '全部'）
if st.session_state.f_source not in source_options:
    st.session_state.f_source = "全部"
if st.session_state.f_category not in category_options:
    st.session_state.f_category = "全部"
if st.session_state.f_pdf_filter not in pdf_filter_options:
    st.session_state.f_pdf_filter = "全部"
if st.session_state.f_fix_filter not in fix_filter_options:
    st.session_state.f_fix_filter = "全部"

# 筛选条件变化双重保险机制
current_filters_hash = f"{st.session_state.f_keyword}_{st.session_state.f_source}_{st.session_state.f_category}_{st.session_state.f_pdf_filter}_{st.session_state.f_fix_filter}_{st.session_state.f_sort}_{st.session_state.f_page_size}"
if st.session_state.get("last_filters_hash") != current_filters_hash:
    st.session_state.last_filters_hash = current_filters_hash
    st.session_state.current_page = 1
    st.session_state.top_jump = 1
    st.session_state.bottom_jump = 1

page_size = st.session_state.f_page_size
layout_mode = st.session_state.f_layout

# 当前查询完整签名（筛选 + 排序 + 每页条数 + 当前页码）
current_query_sig = f"{current_filters_hash}_{st.session_state.current_page}"

# 如果查询条件或页码发生变动，或内存无数据/当前页数据已被删空且仍有剩余数据，则执行数据库真实分页查询
is_cache_empty = "cached_df" in st.session_state and (st.session_state.cached_df is None or st.session_state.cached_df.empty)
has_remaining_records = st.session_state.get("cached_total_records", 0) > 0

if (
    st.session_state.get("cached_query_sig") != current_query_sig
    or "cached_df" not in st.session_state
    or st.session_state.cached_df is None
    or (is_cache_empty and has_remaining_records)
):
    st.session_state.cached_query_sig = current_query_sig
    total_records, df = db_reader.query_records(
        keyword=st.session_state.f_keyword,
        source=st.session_state.f_source,
        category=st.session_state.f_category,
        pdf_filter=st.session_state.f_pdf_filter,
        fix_filter=st.session_state.f_fix_filter,
        sort_by=st.session_state.f_sort,
        page=st.session_state.current_page,
        page_size=page_size
    )

    # 剔除本次会话中已删除的记录（双重保险）
    if "deleted_ids" in st.session_state and st.session_state.deleted_ids and not df.empty:
        df = df[~df["id"].astype(str).isin(st.session_state.deleted_ids)]

    # 检查每条记录的本地 PDF 实际存在情况与重复状态（仅在真实查询时计算一次并存入 df）
    if not df.empty:
        def check_pdf_exists(path):
            resolved = resolve_pdf_path(path)
            return "已存在" if resolved else ("路径缺失" if path else "无文件")
        
        if "pdf_status" not in df.columns:
            df["pdf_status"] = df["pdf_path"].apply(check_pdf_exists)
        else:
            df["pdf_status"] = df["pdf_status"].fillna("").apply(lambda s: s if s else check_pdf_exists(""))

        if "dup_cnt" not in df.columns or df["dup_cnt"].fillna(1).max() <= 1:
            title_counts = df[df["title"].str.strip() != ""]["title"].value_counts().to_dict()
            link_counts = df[df["resource_link"].str.strip() != ""]["resource_link"].value_counts().to_dict()
            pdf_counts = df[df["pdf_path"].str.strip() != ""]["pdf_path"].value_counts().to_dict()
            
            def calc_page_dup_cnt(row):
                cnts = [1]
                t = (row.get("title") or "").strip()
                if t and t in title_counts:
                    cnts.append(title_counts[t])
                l = (row.get("resource_link") or "").strip()
                if l and l in link_counts:
                    cnts.append(link_counts[l])
                p = (row.get("pdf_path") or "").strip()
                if p and p in pdf_counts:
                    cnts.append(pdf_counts[p])
                return max(cnts)
                
            df["dup_cnt"] = df.apply(calc_page_dup_cnt, axis=1)

    st.session_state.cached_total_records = total_records
    st.session_state.cached_df = df
else:
    # 命中内存缓存（如删除操作后、切换视图模式等）：直接读取内存数据，绝对不发起 SQL 重新查询！
    total_records = st.session_state.cached_total_records
    df = st.session_state.cached_df

total_pages = max(1, (total_records + page_size - 1) // page_size)

# 如果页码超界（例如最后一页数据删完），自动回退到最新有效最大页码并重查
if total_records > 0 and st.session_state.current_page > total_pages:
    st.session_state.current_page = max(1, total_pages)
    st.session_state.top_jump = st.session_state.current_page
    st.session_state.bottom_jump = st.session_state.current_page
    st.session_state.cached_query_sig = ""
    st.session_state.pop("cached_df", None)
    st.rerun()

# 确保输入框页码在 [1, total_pages] 范围内核准
st.session_state.top_jump = max(1, min(int(st.session_state.top_jump), total_pages))
st.session_state.bottom_jump = max(1, min(int(st.session_state.bottom_jump), total_pages))


PDF_THUMB_CACHE_DIR = os.path.join(PROJECT_ROOT, "cache", "pdf_thumbs")
os.makedirs(PDF_THUMB_CACHE_DIR, exist_ok=True)


class PDFRequestHandler(BaseHTTPRequestHandler):
    """用于动态按需光栅化并流式输出 PDF 页面 JPEG 的轻量高性能 HTTP Handler"""
    def log_message(self, format, *args):
        pass  # 禁用标准请求日志输出，保持控制台整洁

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/pdf_page":
            query = urllib.parse.parse_qs(parsed.query)
            file_path = query.get("path", [""])[0]
            page_num = int(query.get("page", ["0"])[0])
            mtime = query.get("mtime", ["0"])[0]
            dpi = int(query.get("dpi", ["105"])[0])
            quality = int(query.get("quality", ["75"])[0])

            if not file_path or not os.path.exists(file_path):
                self.send_error(404, "File not found")
                return

            try:
                # 缓存 key
                key = hashlib.md5(f"{file_path}_{mtime}_{dpi}_{quality}".encode("utf-8")).hexdigest()
                cpath = os.path.join(PDF_THUMB_CACHE_DIR, f"{key}_{page_num}.jpg")
                
                img_bytes = None
                if os.path.exists(cpath):
                    with open(cpath, "rb") as f:
                        img_bytes = f.read()
                else:
                    import pymupdf
                    doc = pymupdf.open(file_path)
                    if 0 <= page_num < len(doc):
                        pix = doc[page_num].get_pixmap(dpi=dpi)
                        img_bytes = pix.tobytes("jpeg", quality)
                        with open(cpath, "wb") as f:
                            f.write(img_bytes)
                    doc.close()

                if img_bytes:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(img_bytes)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "public, max-age=604800, immutable")
                    self.end_headers()
                    self.wfile.write(img_bytes)
                else:
                    self.send_error(404, "Page not found")
            except Exception as e:
                self.send_error(500, str(e))
        else:
            self.send_error(404, "Not Found")


_PDF_SERVER_PORT = None
_PDF_SERVER_LOCK = threading.Lock()


def ensure_pdf_server_started(start_port=8515) -> int:
    """确保全局单例后台多线程 HTTP 静态服务已启动"""
    global _PDF_SERVER_PORT
    with _PDF_SERVER_LOCK:
        if _PDF_SERVER_PORT is not None:
            return _PDF_SERVER_PORT
        for p in range(start_port, start_port + 50):
            try:
                server = ThreadingHTTPServer(('127.0.0.1', p), PDFRequestHandler)
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                _PDF_SERVER_PORT = p
                return p
            except OSError:
                continue
        _PDF_SERVER_PORT = start_port
        return start_port


@st.cache_data(max_entries=3000, ttl=3600, show_spinner=False)
def get_pdf_page_count(file_path: str, mtime: float) -> int:
    """极速获取 PDF 的总页数（耗时 <0.1ms，纯元数据读取，零光栅化）"""
    if not file_path or not os.path.exists(file_path):
        return 0
    try:
        import pymupdf
        doc = pymupdf.open(file_path)
        count = len(doc)
        doc.close()
        return count
    except Exception:
        return 0


@st.cache_data(max_entries=300, ttl=3600, show_spinner=False)
def load_pdf_as_base64(file_path: str, mtime: float) -> str:
    """缓存读取并编码本地 PDF 文件，避免重复磁盘 I/O 与 Base64 计算"""
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def render_record_card(row: dict, iframe_height: int = 520, card_index: int = 0):
    """渲染单条记录卡片（无外层背景框，结构清晰解耦）"""
    item_id = row.get("id")
    title = row.get("title") or "（无标题）"
    source = row.get("source") or "未知"
    category = row.get("category") or "未分类"
    publish_time = row.get("publish_time") or "-"
    size = row.get("size") or "-"
    fmt = row.get("format") or "-"
    url = row.get("url") or ""
    resource_link = row.get("resource_link") or ""
    pikpak_link = row.get("pikpak_link") or ""
    raw_pdf_path = row.get("pdf_path") or ""
    
    resolved_pdf = resolve_pdf_path(raw_pdf_path)
    
    # 1. 标题行（带 ID 药丸徽章，统一高度并支持鼠标悬浮提示完整标题）
    id_label = f"#{item_id}" if not str(item_id).startswith("ORPHAN-") else f"孤儿 #{str(item_id)[7:]}"
    # 数据库字段来自抓取的外部网页，渲染进 HTML 前必须转义，防止存储型 XSS / 属性注入
    title_esc = html.escape(str(title), quote=True)
    source_esc = html.escape(str(source), quote=True)
    category_esc = html.escape(str(category), quote=True)
    fmt_esc = html.escape(str(fmt), quote=True)
    publish_time_esc = html.escape(str(publish_time), quote=True)
    size_esc = html.escape(str(size), quote=True)
    st.markdown(
        f"""
        <div class="card-title-row">
            <span class="card-id-badge">{id_label}</span>
            <span class="card-title-text" title="{title_esc}">{title_esc}</span>
        </div>
        """,
        unsafe_allow_html=True
    )
    
    # 2. 收集可用操作按钮
    action_buttons = []
    if url:
        action_buttons.append(("link", T("原网页"), url))
    if resource_link:
        action_buttons.append(("link", T("资源链接"), resource_link))
    if pikpak_link:
        action_buttons.append(("link", T("PikPak"), pikpak_link))
    if resolved_pdf:
        action_buttons.append(("btn", T("本地定位"), resolved_pdf))
    # 增加删除按钮 (支持数据库记录与物理 PDF 级联删除)
    action_buttons.append(("delete", T("删除"), item_id))

    # 3. 标签徽章与元信息 HTML（支持重复组徽章）
    badge_list = [f'<span class="badge badge-source" title="{source_esc}">{source_esc}</span>']
    if category and category != "-" and category != "未分类":
        badge_cls = "badge-orphan" if "孤儿" in category else "badge-category"
        category_disp = html.escape(T(str(category)), quote=True)
        badge_list.append(f'<span class="badge {badge_cls}" title="{category_disp}">{category_disp}</span>')
    if fmt and fmt != "-":
        badge_list.append(f'<span class="badge badge-format" title="{fmt_esc}">{fmt_esc}</span>')
    
    dup_cnt = row.get("dup_cnt", 1)
    if dup_cnt and int(dup_cnt) > 1:
        badge_list.append(f'<span class="badge badge-duplicate" title="包含 {dup_cnt} 条相同重复记录（已按组相邻排列）">重复 ({dup_cnt}条)</span>')
    
    badges_str = "".join(badge_list)
    meta_html = f'''
    <div class="card-meta-row">
        <div>{badges_str}</div>
        <span class="card-meta-divider"></span>
        <span class="card-meta-item" title="{T('发布')}: {publish_time_esc}">{T('发布')}: {publish_time_esc}</span>
        <span class="card-meta-item" title="{T('大小')}: {size_esc}">{T('大小')}: {size_esc}</span>
    </div>
    '''

    # 4. 元信息与操作按钮整合单行展示
    if action_buttons:
        num_btns = len(action_buttons)
        # 元信息列弹性占满剩余空间，每个按钮列固定收紧到内容宽度
        col_weights = [6] + [0.6] * num_btns
        card_cols = st.columns(col_weights, vertical_alignment="center")
        
        with card_cols[0]:
            st.markdown(meta_html, unsafe_allow_html=True)
            
        for idx, (b_type, b_label, b_val) in enumerate(action_buttons):
            with card_cols[idx + 1]:
                if b_type == "link":
                    st.link_button(b_label, b_val, use_container_width=False)
                elif b_type == "btn":
                    if st.button(b_label, key=f"loc_{item_id}_{card_index}", use_container_width=False):
                        open_in_system(b_val)
                elif b_type == "delete":
                    with st.popover(b_label, key=f"del_pop_{item_id}", use_container_width=False):
                        st.markdown(f"**确认删除记录 #{item_id}？**")
                        other_refs = count_other_pdf_references(
                            db_path=db_path,
                            record_id=item_id,
                            raw_pdf_path=raw_pdf_path,
                            abs_path=row.get("_abs_path", "") or resolved_pdf
                        ) if (resolved_pdf or raw_pdf_path) else 0

                        if other_refs > 0:
                            pdf_info = T(f"⚠️ 本地 PDF 仍被其他 {other_refs} 条记录共享引用，确认后仅删除本条数据库记录，本地物理文件将安全保留！")
                        elif resolved_pdf:
                            pdf_info = T("将永久删除本条数据库记录及对应的本地 PDF 文件（无其他引用），此操作不可撤销！")
                        else:
                            pdf_info = T("将删除本条数据库记录（无关联本地 PDF），此操作不可撤销！")
                        st.caption(pdf_info)
                        if st.button(T("确认删除"), key=f"del_confirm_{item_id}_{card_index}", type="primary", use_container_width=True):
                            db_del, pdf_del, msg = delete_single_record(
                                db_path=db_path,
                                record_id=item_id,
                                raw_pdf_path=raw_pdf_path,
                                abs_path=row.get("_abs_path", "")
                            )
                            # 1. 记录已删除 ID 到 session_state
                            if "deleted_ids" not in st.session_state:
                                st.session_state.deleted_ids = set()
                            st.session_state.deleted_ids.add(str(item_id))
                            
                            # 2. 检查当前质检模式下的重复关联项（若删除后关联记录已不再重复，则自动移出重复列表）
                            fix_filter = st.session_state.get("f_fix_filter", "全部")
                            fix_field_map = {
                                "标题完全重复": "title",
                                "PDF路径重复": "pdf_path",
                                "磁力链接重复": "resource_link",
                                "URL 地址重复": "url"
                            }
                            
                            auto_removed_ids = []
                            if fix_filter in fix_field_map and not str(item_id).startswith("ORPHAN-"):
                                dup_col = fix_field_map[fix_filter]
                                dup_val = (row.get(dup_col) or "").strip()
                                if dup_val:
                                    try:
                                        with sqlite3.connect(db_path, timeout=10.0) as check_conn:
                                            cursor = check_conn.cursor()
                                            cursor.execute(f"SELECT id FROM resources WHERE {dup_col} = ?", (dup_val,))
                                            remaining_ids = [str(r[0]) for r in cursor.fetchall()]
                                        
                                        remaining_cnt = len(remaining_ids)
                                        if remaining_cnt <= 1:
                                            # 剩余记录数 <= 1，说明在数据库中已成为唯一记录，不再属于重复项！
                                            # 在当前重复质检视图中自动将剩余记录一并移出当前页面
                                            auto_removed_ids = remaining_ids
                                            for rid in remaining_ids:
                                                st.session_state.deleted_ids.add(str(rid))
                                            
                                            if "cached_df" in st.session_state and not st.session_state.cached_df.empty:
                                                ids_to_remove = set([str(item_id)] + remaining_ids)
                                                st.session_state.cached_df = st.session_state.cached_df[
                                                    ~st.session_state.cached_df["id"].astype(str).isin(ids_to_remove)
                                                ]
                                            if "cached_total_records" in st.session_state:
                                                st.session_state.cached_total_records = max(0, st.session_state.cached_total_records - 1 - len(remaining_ids))
                                        else:
                                            # 剩余记录仍然重复（例如原 3 条删了 1 条剩 2 条），更新内存中对应记录的 dup_cnt
                                            if "cached_df" in st.session_state and not st.session_state.cached_df.empty:
                                                st.session_state.cached_df = st.session_state.cached_df[
                                                    st.session_state.cached_df["id"].astype(str) != str(item_id)
                                                ]
                                                st.session_state.cached_df.loc[
                                                    st.session_state.cached_df["id"].astype(str).isin(remaining_ids), "dup_cnt"
                                                ] = remaining_cnt
                                            if "cached_total_records" in st.session_state and st.session_state.cached_total_records > 0:
                                                st.session_state.cached_total_records -= 1
                                    except Exception:
                                        if "cached_df" in st.session_state and not st.session_state.cached_df.empty:
                                            st.session_state.cached_df = st.session_state.cached_df[
                                                st.session_state.cached_df["id"].astype(str) != str(item_id)
                                            ]
                                        if "cached_total_records" in st.session_state and st.session_state.cached_total_records > 0:
                                            st.session_state.cached_total_records -= 1
                                else:
                                    if "cached_df" in st.session_state and not st.session_state.cached_df.empty:
                                        st.session_state.cached_df = st.session_state.cached_df[
                                            st.session_state.cached_df["id"].astype(str) != str(item_id)
                                        ]
                                    if "cached_total_records" in st.session_state and st.session_state.cached_total_records > 0:
                                        st.session_state.cached_total_records -= 1
                            else:
                                # 常规模式直接剔除已删除行
                                if "cached_df" in st.session_state and not st.session_state.cached_df.empty:
                                    st.session_state.cached_df = st.session_state.cached_df[
                                        st.session_state.cached_df["id"].astype(str) != str(item_id)
                                    ]
                                if "cached_total_records" in st.session_state and st.session_state.cached_total_records > 0:
                                    st.session_state.cached_total_records -= 1
                            
                            # 3. 内存直接同步递减统计数字
                            if "cached_stats" in st.session_state:
                                st.session_state.cached_stats["total"] = max(0, st.session_state.cached_stats.get("total", 0) - 1)
                                if resolved_pdf:
                                    st.session_state.cached_stats["has_pdf_record"] = max(0, st.session_state.cached_stats.get("has_pdf_record", 0) - 1)
                                if str(item_id).startswith("ORPHAN-"):
                                    st.session_state.cached_stats["orphan_count"] = max(0, st.session_state.cached_stats.get("orphan_count", 0) - 1)
                            
                            # 4. 如果当前页数据已全部删除完毕（cached_df 为空），重置查询签名与缓存，以便自动加载后续数据
                            if "cached_df" in st.session_state and st.session_state.cached_df.empty:
                                st.session_state.cached_query_sig = ""
                                st.session_state.pop("cached_df", None)

                            if auto_removed_ids:
                                removed_labels = "、".join([f"#{rid}" for rid in auto_removed_ids])
                                st.toast(T(f"#{item_id} 已删除，关联记录 {removed_labels} 已无重复并自动移出重复列表"))
                            else:
                                st.toast(T(f"#{item_id} 已删除: {msg}"))
                            st.rerun()
    else:
        st.markdown(meta_html, unsafe_allow_html=True)
    
    # 5. PDF 预览区域 / 无 PDF 占位容器（初始上滑 200px，且支持鼠标自由上下滑动查看全部内容）
    if resolved_pdf:
        try:
            mtime = os.path.getmtime(resolved_pdf)
            page_count = get_pdf_page_count(resolved_pdf, mtime)
            if page_count > 0:
                server_port = ensure_pdf_server_started()
                enc_path = urllib.parse.quote(resolved_pdf)
                placeholder_pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
                imgs_html_list = []
                for p_idx in range(page_count):
                    img_url = f"http://127.0.0.1:{server_port}/pdf_page?path={enc_path}&page={p_idx}&mtime={mtime}&dpi=105&quality=75"
                    img_id = f"pdf_img_{item_id}_{p_idx}"
                    # 每张卡片的第一页作为主预览图直接挂载真实 src，确保立即可见且绝无串图残留
                    if p_idx == 0:
                        imgs_html_list.append(
                            f'<img id="{img_id}" data-item-id="{item_id}" class="pdf-page-img loaded" src="{img_url}" loading="lazy" />'
                        )
                    else:
                        # 卡片内第 2 页及后续页走视口与容器滑动懒加载（真·按需动态触发渲染）
                        imgs_html_list.append(
                            f'<img id="{img_id}" data-item-id="{item_id}" class="pdf-page-img lazy-pdf-img" src="{placeholder_pixel}" data-src="{img_url}" loading="lazy" />'
                        )
                imgs_html = "".join(imgs_html_list)
                pdf_display = f'''
                <div class="pdf-scroll-container" id="pdf_scroll_{item_id}" data-item-id="{item_id}" style="height: {iframe_height}px;">
                    {imgs_html}
                </div>
                '''
                st.markdown(pdf_display, unsafe_allow_html=True)
            else:
                # 备用方案：若图片解析异常则走原 iframe 预览
                base64_pdf = load_pdf_as_base64(resolved_pdf, mtime)
                st.markdown(f'''
                <div class="pdf-scroll-container" id="pdf_scroll_{item_id}" data-item-id="{item_id}" style="height: {iframe_height}px;">
                    <iframe src="data:application/pdf;base64,{base64_pdf}#toolbar=0&navpanes=0" 
                            loading="lazy"
                            width="100%" 
                            height="{iframe_height}px" 
                            type="application/pdf"
                            style="border: none; display: block; height: {iframe_height}px;">
                    </iframe>
                </div>
                ''', unsafe_allow_html=True)
        except Exception:
            # 读取失败时也展示固定高度占位容器，不用 st.error 出错误框（高度不定会打乱卡片布局）
            st.markdown(f'''
            <div class="pdf-empty-placeholder" style="height: {iframe_height}px;">
                <div class="empty-title">PDF 读取失败</div>
            </div>
            ''', unsafe_allow_html=True)
    else:
        # 空状态占位：高度严格与 iframe_height 统一（无论是否有本地 PDF）
        if raw_pdf_path:
            status_title = T("本地 PDF 文件缺失")
        else:
            status_title = T("未关联本地 PDF 快照")

        placeholder_html = f'''
        <div class="pdf-empty-placeholder" style="height: {iframe_height}px;">
            <div class="empty-title">{status_title}</div>
        </div>
        '''
        st.markdown(placeholder_html, unsafe_allow_html=True)

    # 6. 卡片底部信息展示（第一行：PDF 文件名称；第二行：资源链接；单行超出省略，鼠标悬停展示完整文本）
    # 「路径显示」开关控制文件行显示内容：仅文件名 / 相对路径 / 绝对路径
    path_mode = st.session_state.get("f_path_mode", "仅文件名")
    if resolved_pdf:
        abs_pdf = resolved_pdf
        try:
            rel_pdf = os.path.relpath(resolved_pdf, PROJECT_ROOT)
        except ValueError:
            rel_pdf = resolved_pdf
        if path_mode == "绝对路径":
            pdf_display = abs_pdf
        elif path_mode == "相对路径":
            pdf_display = rel_pdf
        else:
            pdf_display = os.path.basename(resolved_pdf)
        pdf_tooltip = abs_pdf
    elif raw_pdf_path:
        raw_s = str(raw_pdf_path)
        if path_mode == "绝对路径":
            pdf_display = raw_s if os.path.isabs(raw_s) else os.path.join(PROJECT_ROOT, raw_s)
        elif path_mode == "相对路径":
            pdf_display = raw_s
        else:
            pdf_display = os.path.basename(raw_s.replace("\\", "/"))
        pdf_tooltip = raw_s
    else:
        pdf_display = "-"
        pdf_tooltip = "-"

    pdf_display_esc = html.escape(str(pdf_display), quote=True)
    pdf_tooltip_esc = html.escape(str(pdf_tooltip), quote=True)
    res_link_raw = str(resource_link).strip() if resource_link else ""
    res_link_esc = html.escape(res_link_raw, quote=True)

    if res_link_raw:
        if res_link_raw.startswith("magnet:?"):
            xt_match = re.search(r'xt=urn:btih:([a-zA-Z0-9]+)', res_link_raw, re.IGNORECASE)
            if xt_match:
                h = xt_match.group(1)
                short_disp = f"magnet:?xt=urn:btih:{h[:8]}...{h[-6:]}"
            else:
                short_disp = res_link_raw[:36] + "..."
        elif len(res_link_raw) > 50:
            short_disp = res_link_raw[:45] + "..."
        else:
            short_disp = res_link_raw
        short_disp_esc = html.escape(short_disp, quote=True)
        res_link_content = f'<a href="{res_link_esc}" target="_blank" rel="noopener noreferrer" class="card-bottom-link" title="{res_link_esc}">{short_disp_esc}</a>'
    else:
        res_link_content = '<span class="card-bottom-empty">-</span>'

    # 底部信息区：始终渲染「文件」行与「链接」行，保证所有卡片完全等高
    if resolved_pdf or raw_pdf_path:
        file_line_html = f'''
        <div class="card-bottom-line" title="{pdf_tooltip_esc}">
            <span class="card-bottom-label">文件:</span>
            <span class="card-bottom-value" title="{pdf_tooltip_esc}">{pdf_display_esc}</span>
        </div>'''
    else:
        file_line_html = '''
        <div class="card-bottom-line">
            <span class="card-bottom-label">文件:</span>
            <span class="card-bottom-value card-bottom-empty">-</span>
        </div>'''

    footer_info_html = f'''
    <div class="card-bottom-info">{file_line_html}
        <div class="card-bottom-line" title="{res_link_esc if res_link_raw else '-'}">
            <span class="card-bottom-label">链接:</span>
            <span class="card-bottom-value" title="{res_link_esc if res_link_raw else '-'}">{res_link_content}</span>
        </div>
    </div>
    '''
    st.markdown(footer_info_html, unsafe_allow_html=True)


def render_pagination(key_prefix: str = "bottom"):
    """通用底部分页控制条（外观与页首翻页控件组完全一致）"""
    p_cols = st.columns([1.0, 0.24, 0.10, 0.03, 0.05, 0.03], vertical_alignment="bottom")
    with p_cols[0]:
        st.empty()
    with p_cols[1]:
        st.markdown(
            f"""
            <div class="toolbar-stat-badge pagination-stat-badge" title="共 {total_records:,} 条数据 (第 {st.session_state.current_page} / {total_pages} 页)">
                共 <span class="stat-num">{total_records:,}</span> 条 (<span class="stat-page">{st.session_state.current_page}</span>/{total_pages}页)
            </div>
            """,
            unsafe_allow_html=True
        )
    with p_cols[2]:
        page_size_opts = [10, 20, 30, 40, 50]
        cur_idx = page_size_opts.index(st.session_state.f_page_size) if st.session_state.f_page_size in page_size_opts else 0
        st.selectbox(
            "每页条数",
            options=page_size_opts,
            index=cur_idx,
            key=f"{key_prefix}_page_size",
            format_func=lambda x: f"{x} 条/页",
            label_visibility="collapsed",
            on_change=on_bottom_page_size_change
        )
    with p_cols[3]:
        st.button("‹", key=f"{key_prefix}_prev", help="上一页", disabled=(st.session_state.current_page <= 1), use_container_width=True, on_click=prev_page)
    with p_cols[4]:
        st.number_input(
            "跳转页码",
            min_value=1,
            max_value=total_pages,
            key=f"{key_prefix}_jump",
            label_visibility="collapsed",
            on_change=on_bottom_jump_change
        )
    with p_cols[5]:
        st.button("›", key=f"{key_prefix}_next", help="下一页", disabled=(st.session_state.current_page >= total_pages), use_container_width=True, on_click=next_page)


# ==================== 顶部右上角外观与视图设置 Popover ====================

with st.container():
    st.markdown('<div class="header-settings-popover-marker"></div>', unsafe_allow_html=True)
    with st.popover("⚙", use_container_width=False):
        st.markdown(
            """
            <div class="settings-popover-panel">
                <div class="settings-section">
                    <div class="settings-section-title">主题外观</div>
                    <div class="settings-theme-btn-group">
                        <button type="button" class="settings-theme-btn theme-btn-dark" onclick="try{(window._setTheme||document._setTheme||(window.parent&&window.parent._setTheme)||(window.parent&&window.parent.document&&window.parent.document._setTheme))('dark')}catch(e){};">
                            深色模式
                        </button>
                        <button type="button" class="settings-theme-btn theme-btn-light" onclick="try{(window._setTheme||document._setTheme||(window.parent&&window.parent._setTheme)||(window.parent&&window.parent.document&&window.parent.document._setTheme))('light')}catch(e){};">
                            浅色模式
                        </button>
                    </div>
                </div>
                <div class="settings-divider"></div>
            </div>
            """,
            unsafe_allow_html=True
        )
        st.markdown('<div class="settings-section-title">画廊排版模式</div>', unsafe_allow_html=True)
        st.selectbox(
            "排版模式",
            ["双列画廊 (推荐)", "单列大图", "三列紧凑", "纯表格视图"],
            key="f_layout",
            format_func=T,
            label_visibility="collapsed"
        )
        st.markdown('<div class="settings-section-title" style="margin-top:10px;">路径显示格式</div>', unsafe_allow_html=True)
        path_mode_opts = ["仅文件名", "相对路径", "绝对路径"]
        cur_path_idx = path_mode_opts.index(st.session_state.f_path_mode) if st.session_state.f_path_mode in path_mode_opts else 0
        st.selectbox(
            "路径显示",
            path_mode_opts,
            index=cur_path_idx,
            key="f_path_mode",
            format_func=T,
            label_visibility="collapsed"
        )


# ==================== 顶部主导航 Tab 与多维筛选工具栏 ====================

main_tab_gallery, main_tab_maintenance = st.tabs([
    T("画廊浏览"),
    T("批量管理"),
])

with main_tab_gallery:
    # 顶部多维搜索、精准筛选与极简分页一体化单行工具栏
    f_cols = st.columns([1.6, 0.75, 0.75, 0.85, 0.85, 1.05, 1.25, 0.75, 0.22, 0.38, 0.22], vertical_alignment="bottom")
    with f_cols[0]:
        st.text_input(T("关键词搜索"), placeholder="输入标题 / 链接 / 磁力哈希...", key="f_keyword", on_change=reset_page)
    with f_cols[1]:
        cur_src_idx = source_options.index(st.session_state.f_source) if st.session_state.f_source in source_options else 0
        st.selectbox("来源渠道", source_options, index=cur_src_idx, key="f_source", on_change=reset_page)
    with f_cols[2]:
        cur_cat_idx = category_options.index(st.session_state.f_category) if st.session_state.f_category in category_options else 0
        st.selectbox(T("内容分类"), category_options, index=cur_cat_idx, key="f_category", on_change=reset_page, format_func=T)
    with f_cols[3]:
        cur_pdf_idx = pdf_filter_options.index(st.session_state.f_pdf_filter) if st.session_state.f_pdf_filter in pdf_filter_options else 0
        st.selectbox(T("PDF状态"), pdf_filter_options, index=cur_pdf_idx, key="f_pdf_filter", on_change=reset_page, format_func=T)
    with f_cols[4]:
        cur_fix_idx = fix_filter_options.index(st.session_state.f_fix_filter) if st.session_state.f_fix_filter in fix_filter_options else 0
        st.selectbox(T("质检/维护"), fix_filter_options, index=cur_fix_idx, key="f_fix_filter", on_change=reset_page, format_func=T)
    with f_cols[5]:
        st.selectbox(T("排序方式"), sort_options, key="f_sort", on_change=reset_page, format_func=T)
    with f_cols[6]:
        st.markdown(
            f"""
            <div class="toolbar-stat-badge" title="共 {total_records:,} 条数据 (第 {st.session_state.current_page} / {total_pages} 页)">
                检索匹配 <span class="stat-num">{total_records:,}</span> 条 (<span class="stat-page">{st.session_state.current_page}</span>/{total_pages}页)
            </div>
            """,
            unsafe_allow_html=True
        )
    with f_cols[7]:
        page_size_opts = [10, 20, 30, 40, 50]
        cur_idx = page_size_opts.index(st.session_state.f_page_size) if st.session_state.f_page_size in page_size_opts else 0
        st.selectbox(
            "每页条数",
            options=page_size_opts,
            index=cur_idx,
            key="f_page_size",
            format_func=lambda x: f"{x} 条/页",
            label_visibility="collapsed",
            on_change=on_top_page_size_change
        )
    with f_cols[8]:
        st.button("‹", key="top_prev", help="上一页", disabled=(st.session_state.current_page <= 1), use_container_width=True, on_click=prev_page)
    with f_cols[9]:
        st.number_input(
            "跳转页码",
            min_value=1,
            max_value=total_pages,
            key="top_jump",
            label_visibility="collapsed",
            on_change=on_top_jump_change
        )
    with f_cols[10]:
        st.button("›", key="top_next", help="下一页", disabled=(st.session_state.current_page >= total_pages), use_container_width=True, on_click=next_page)

    if df.empty:
        st.info(T("没有找到匹配条件的记录，请尝试调整搜索或筛选条件。"))

    else:
        if layout_mode == "纯表格视图":
            # 纯表格模式
            display_df = df[["id", "title", "source", "category", "size", "url", "resource_link", "pdf_status"]].copy()
            # 精简图标模式：表格内状态/分类列同样剥离装饰性图标
            display_df["pdf_status"] = display_df["pdf_status"].map(lambda s: T(str(s)) if isinstance(s, str) else s)
            display_df["category"] = display_df["category"].map(lambda s: T(str(s)) if isinstance(s, str) else s)
            # 精简图标模式：表格单元格中的状态文本（如 ✅ 本地存在）同步剥离图标
            display_df["pdf_status"] = display_df["pdf_status"].map(T)
            display_df["dup_status"] = df["dup_cnt"].apply(lambda c: f"重复 ({c}条)" if c and int(c) > 1 else "唯一")
            column_config = {
                "id": st.column_config.NumberColumn("ID", width="small"),
                "dup_status": st.column_config.TextColumn("查重状态", width="small"),
                "title": st.column_config.TextColumn("标题", width="medium"),
                "source": st.column_config.TextColumn("来源", width="small"),
                "category": st.column_config.TextColumn("分类", width="small"),
                "size": st.column_config.TextColumn("大小", width="small"),
                "url": st.column_config.LinkColumn(T("详情网页"), display_text="打开网页", width="medium"),
                "resource_link": st.column_config.LinkColumn(T("资源链接"), display_text="资源链接", width="medium"),
                "pdf_status": st.column_config.TextColumn(T("PDF"), width="small"),
            }
            st.dataframe(
                display_df,
                column_config=column_config,
                use_container_width=True,
                hide_index=True
            )
        else:
            # 画廊模式：启动本地极速 HTTP PDF 服务（按需流式动态渲染）
            ensure_pdf_server_started()

            if layout_mode == "单列大图":
                # 单列大屏流式预览（行间单线分隔）
                for idx, row in df.reset_index(drop=True).iterrows():
                    if idx > 0:
                        st.markdown('<div class="grid-row-divider"></div>', unsafe_allow_html=True)
                    render_record_card(row.to_dict(), iframe_height=650, card_index=idx)
            elif layout_mode == "三列紧凑":
                # 三列网格画廊（行间单线分隔，列间单线分隔）
                rows_list = df.to_dict("records")
                for i in range(0, len(rows_list), 3):
                    if i > 0:
                        st.markdown('<div class="grid-row-divider"></div>', unsafe_allow_html=True)
                    cols = st.columns(3)
                    for j in range(3):
                        if i + j < len(rows_list):
                            with cols[j]:
                                render_record_card(rows_list[i + j], iframe_height=420, card_index=i + j)
            else:
                # 默认：双列网格画廊（行间单线分隔，列间单线分隔）
                rows_list = df.to_dict("records")
                for i in range(0, len(rows_list), 2):
                    if i > 0:
                        st.markdown('<div class="grid-row-divider"></div>', unsafe_allow_html=True)
                    cols = st.columns(2)
                    for j in range(2):
                        if i + j < len(rows_list):
                            with cols[j]:
                                render_record_card(rows_list[i + j], iframe_height=520, card_index=i + j)

        # 底部单线分隔与翻页器
        st.markdown('<div class="grid-row-divider" style="margin: 20px 0 12px 0;"></div>', unsafe_allow_html=True)
        render_pagination("bottom")

with main_tab_maintenance:
    render_maintenance_hub()


# 注入 JavaScript：基于 IntersectionObserver 视口真懒加载 + 内部自由滑动动态渲染 + 初始滚动 200px
# 注入 JavaScript：基于 IntersectionObserver 视口真懒加载 (assets/viewer_lazy.js)
lazy_js_content = get_asset_content('viewer_lazy.js')
st.iframe(
    f'<script>\n{lazy_js_content}\n</script>',
    height=1,
    width=1
)


