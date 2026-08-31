import os
import sys
import sqlite3
import base64
import subprocess
import hashlib
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

# 导入项目配置
from config import PROJECT_ROOT, PDF_BASE_DIR, get_db_path

# 设置页面配置（收起侧边栏以最大化内容区域）
st.set_page_config(
    page_title="资源数据库与 PDF 增强预览器",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 自定义 CSS 样式提升视觉体验
st.markdown("""
<style>
    /* 优化全局间距与顶部留白 */
    .block-container {
        padding-top: 0.6rem !important;
        padding-bottom: 2.0rem !important;
        padding-left: 1.5rem !important;
        padding-right: 1.5rem !important;
    }
    
    /* 缩减 Streamlit 内部默认的垂直大间隙 */
    div[data-testid="stVerticalBlock"] {
        gap: 0.45rem !important;
    }

    /* Streamlit 原生顶部 Header：静态嵌入在顶部状态栏中，随页面正常滚动 */
    header[data-testid="stHeader"],
    .stAppHeader {
        position: static !important;
        height: auto !important;
        min-height: 0 !important;
        background: transparent !important;
        display: flex !important;
        align-items: center !important;
        margin: 0 !important;
        padding: 0 !important;
        z-index: 1 !important;
    }
    div[data-testid="stToolbar"] {
        position: static !important;
        display: flex !important;
        align-items: center !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    #MainMenu {
        visibility: visible !important;
        display: block !important;
    }
    
    /* 隐藏注入脚本的空组件容器 */
    iframe[data-testid="stCustomComponentV1"],
    div[data-testid="stCustomComponentV1"] {
        display: none !important;
        height: 0 !important;
        width: 0 !important;
        position: absolute !important;
    }

    /* 顶部状态栏（通栏布局，右侧容纳原生 Header，随页面共同滚动） */
    .top-status-bar {
        position: relative !important;
        top: auto !important;
        left: auto !important;
        right: auto !important;
        width: 100% !important;
        height: 1.9rem !important;
        min-height: 1.9rem !important;
        z-index: 1 !important;
        display: flex;
        align-items: center;
        flex-wrap: nowrap;
        gap: 8px 12px;
        background: transparent !important;
        border: none !important;
        border-radius: 0 !important;
        padding: 0 !important;
        font-size: 11px;
        margin: 0 0 6px 0 !important;
    }
    .status-item {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        white-space: nowrap;
    }
    .status-label {
        color: #90a4ae;
        font-size: 11px;
    }
    .status-val {
        color: #4caf50;
        font-weight: 700;
        font-size: 11.5px;
    }
    .status-divider {
        width: 1px;
        height: 10px;
        background: rgba(255, 255, 255, 0.15);
    }
    .status-spacer {
        flex: 1;
    }
    .status-meta {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        color: #78909c;
        font-size: 10.5px;
        white-space: nowrap;
    }
    .status-meta code {
        background: rgba(255, 255, 255, 0.08);
        padding: 1px 5px;
        border-radius: 3px;
        font-size: 10.5px;
        color: #cfd8dc;
    }
    
    /* 工具栏面板与输入框微调 */
    div[data-testid="stWidgetLabel"] {
        min-height: auto !important;
        margin-bottom: -3px !important;
        padding-bottom: 0 !important;
    }
    div[data-testid="stWidgetLabel"] p {
        font-size: 10.5px !important;
        font-weight: 500 !important;
        color: #90a4ae !important;
        white-space: nowrap !important;
        line-height: 14px !important;
        margin: 0 !important;
    }

    /* 区域交界单线分隔体系（无背景框极简设计） */
    .toolbar-divider {
        width: 100%;
        height: 1px;
        background: rgba(255, 255, 255, 0.12);
        margin: 6px 0 14px 0;
    }
    .grid-row-divider {
        width: 100%;
        height: 1px;
        background: rgba(255, 255, 255, 0.12);
        margin: 14px 0 16px 0;
    }

    /* 双列/三列画廊：强制每列严格等宽（flex: 1 1 0px），彻底杜绝长文本或占位符撑大单列 */
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .card-title-row) {
        display: flex !important;
        width: 100% !important;
    }
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .card-title-row) > div[data-testid="stColumn"] {
        flex: 1 1 0px !important;
        min-width: 0 !important;
        width: 0 !important;
        box-sizing: border-box !important;
    }
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .card-title-row) > div[data-testid="stColumn"]:not(:last-child) {
        border-right: 1px solid rgba(255, 255, 255, 0.12) !important;
        padding-right: 18px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .card-title-row) > div[data-testid="stColumn"]:not(:first-child) {
        padding-left: 18px !important;
    }
    
    /* 清除单行工具栏内所有 element-container 与 markdown 间距偏差 */
    div[data-testid="stHorizontalBlock"] div[data-testid="element-container"] {
        margin: 0 !important;
        padding: 0 !important;
    }
    div[data-testid="stHorizontalBlock"] div[data-testid="stMarkdownContainer"] {
        margin: 0 !important;
        padding: 0 !important;
    }
    div[data-testid="stHorizontalBlock"] div[data-testid="stMarkdownContainer"] > p {
        margin: 0 !important;
        padding: 0 !important;
        line-height: normal !important;
    }
    
    /* 顶部单行工具栏统计药丸徽章（高度减小 1/4，从 38px 至 28.5px，与输入框/按钮严格对齐） */
    .toolbar-stat-badge {
        display: flex;
        align-items: center;
        justify-content: center;
        height: 28.5px !important;
        min-height: 28.5px !important;
        max-height: 28.5px !important;
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 4px;
        font-size: 11px;
        color: #cfd8dc;
        white-space: nowrap !important;
        padding: 0 6px;
        box-sizing: border-box !important;
        width: 100%;
        margin: 0 !important;
        line-height: 26.5px !important;
    }
    .toolbar-stat-badge .stat-num {
        color: #4caf50;
        font-weight: 700;
        margin: 0 2px;
    }
    .toolbar-stat-badge .stat-page {
        color: #42a5f5;
        font-weight: 700;
        margin: 0 2px;
    }

    /* 顶部单行工具栏内所有控件的尺寸与底部基线严格强制对齐（高度统一为 28.5px） */
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) button,
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stTextInputRootElement"],
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stNumberInputContainer"],
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stTextInput"] > div,
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stNumberInput"] > div,
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stTextInput"] input,
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stNumberInput"] input,
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stSelectbox"] > div > div,
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-baseweb="select"] > div,
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-baseweb="input"],
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-baseweb="base-input"] {
        height: 28.5px !important;
        min-height: 28.5px !important;
        max-height: 28.5px !important;
        box-sizing: border-box !important;
        font-size: 11.5px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stTextInputRootElement"],
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stNumberInputContainer"] {
        padding-top: 0 !important;
        padding-bottom: 0 !important;
        display: flex !important;
        align-items: center !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stSelectbox"] > div > div,
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-baseweb="select"] > div {
        padding-top: 0 !important;
        padding-bottom: 0 !important;
        padding-left: 6px !important;
        padding-right: 6px !important;
        min-height: 28.5px !important;
        height: 28.5px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stTextInput"] input,
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stNumberInput"] input {
        padding: 0 6px !important;
        height: 28.5px !important;
        min-height: 28.5px !important;
        max-height: 28.5px !important;
        line-height: 26.5px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) button {
        border-radius: 4px !important;
        padding: 0 4px !important;
        height: 28.5px !important;
        min-height: 28.5px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) button p {
        font-size: 11px !important;
        line-height: normal !important;
        font-weight: 600 !important;
        margin: 0 !important;
        white-space: nowrap !important;
    }

    /* 分页条文字 */
    .pagination-text {
        display: flex;
        align-items: center;
        height: 38px !important;
        min-height: 38px !important;
        max-height: 38px !important;
        font-size: 13.5px;
        color: #cfd8dc;
        line-height: 38px;
        margin: 0 !important;
        padding: 0 !important;
    }

    /* 卡片内部紧凑垂直间距体系与宽度严格约束 */
    div[data-testid="stColumn"]:has(.card-title-row) > div[data-testid="stVerticalBlock"] {
        gap: 3px !important;
        width: 100% !important;
        min-width: 0 !important;
        max-width: 100% !important;
    }
    div[data-testid="stColumn"]:has(.card-title-row) div[data-testid="element-container"] {
        margin: 0 !important;
        padding: 0 !important;
        width: 100% !important;
        min-width: 0 !important;
    }

    /* 卡片标题行（单行展示，高度统一确保下方内容基线完全对齐） */
    .card-title-row {
        display: flex;
        align-items: center;
        gap: 6px;
        margin: 0 0 1px 0 !important;
        padding: 0 !important;
        line-height: 19px;
        min-height: 19px !important;
        max-height: 19px !important;
        width: 100% !important;
        min-width: 0 !important;
        overflow: hidden;
    }
    .card-id-badge {
        display: inline-block;
        background: rgba(33, 150, 243, 0.15);
        color: #64b5f6;
        border: 1px solid rgba(33, 150, 243, 0.3);
        padding: 1px 6px;
        border-radius: 4px;
        font-size: 11.5px;
        font-weight: 700;
        white-space: nowrap;
        flex-shrink: 0;
        line-height: 15px;
    }
    .card-title-text {
        font-size: 13.5px;
        font-weight: 600;
        color: #eceff1;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        line-height: 19px;
        flex: 1 1 auto;
        min-width: 0;
    }

    /* 标签与徽章样式 */
    .badge {
        display: inline-block;
        padding: 1px 6px;
        border-radius: 3px;
        font-size: 11px;
        font-weight: 600;
        line-height: 16px;
        margin-right: 3px;
    }
    .badge-source { background-color: rgba(30, 136, 229, 0.2); color: #90caf9; border: 1px solid rgba(30, 136, 229, 0.4); }
    .badge-category { background-color: rgba(245, 124, 0, 0.2); color: #ffb74d; border: 1px solid rgba(245, 124, 0, 0.4); }
    .badge-format { background-color: rgba(106, 27, 154, 0.2); color: #ce93d8; border: 1px solid rgba(106, 27, 154, 0.4); }

    /* 卡片单行整合元信息与操作栏（左侧元信息自适应，右侧按钮紧凑自适应，拒绝拉伸变形） */
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) {
        display: flex !important;
        align-items: center !important;
        margin: 0 0 3px 0 !important;
        padding: 0 !important;
        flex-wrap: nowrap !important;
        gap: 6px !important;
        min-height: 22px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) > div[data-testid="stColumn"]:first-child {
        flex: 1 1 auto !important;
        min-width: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) > div[data-testid="stColumn"]:not(:first-child) {
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: unset !important;
    }

    .card-meta-row {
        display: flex;
        align-items: center;
        flex-wrap: nowrap;
        gap: 4px 6px;
        font-size: 11px;
        line-height: 22px;
        min-height: 22px;
        margin: 0 !important;
        padding: 0 !important;
        white-space: nowrap;
        overflow: hidden;
    }
    .card-meta-item {
        display: inline-flex;
        align-items: center;
        gap: 2px;
        color: #b0bec5;
        font-size: 11px;
        white-space: nowrap;
    }
    .card-meta-divider {
        display: inline-block;
        width: 1px;
        height: 10px;
        background: rgba(255, 255, 255, 0.15);
    }

    /* 覆盖卡片内部操作按钮：精致小巧胶囊尺寸 (22px 高度，按文字紧凑自适应宽度，黄金宽高比) */
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a {
        min-height: 22px !important;
        height: 22px !important;
        max-height: 22px !important;
        width: auto !important;
        min-width: unset !important;
        max-width: fit-content !important;
        padding: 0px 8px !important;
        font-size: 11px !important;
        line-height: 20px !important;
        border-radius: 4px !important;
        white-space: nowrap !important;
        word-break: keep-all !important;
        font-weight: 500 !important;
        margin: 0 !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        box-sizing: border-box !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button p,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a p,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button p,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a p {
        font-size: 11px !important;
        line-height: 20px !important;
        margin: 0 !important;
        font-weight: 500 !important;
        white-space: nowrap !important;
        word-break: keep-all !important;
    }

    /* PDF 预览滚动容器：初始自动上滑 200px，且支持鼠标自由上下滑动查看完整内容 */
    .pdf-scroll-container {
        position: relative !important;
        width: 100% !important;
        min-width: 0 !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
        border-radius: 6px;
        overflow-y: auto !important;
        overflow-x: hidden !important;
        border: 1px solid rgba(255, 255, 255, 0.12);
        background: #1e1e1e;
        scroll-behavior: auto;
    }
    .pdf-scroll-container::-webkit-scrollbar {
        width: 6px;
    }
    .pdf-scroll-container::-webkit-scrollbar-track {
        background: rgba(0, 0, 0, 0.2);
    }
    .pdf-scroll-container::-webkit-scrollbar-thumb {
        background: rgba(255, 255, 255, 0.25);
        border-radius: 3px;
    }
    .pdf-scroll-container::-webkit-scrollbar-thumb:hover {
        background: rgba(255, 255, 255, 0.45);
    }
    .pdf-page-img {
        width: 100% !important;
        max-width: 100% !important;
        height: auto !important;
        display: block !important;
        user-select: none;
        transition: opacity 0.2s ease-in-out;
    }
    .pdf-page-img.lazy-pdf-img {
        opacity: 0;
        min-height: 280px;
        background: rgba(255, 255, 255, 0.02);
    }
    .pdf-page-img.loaded {
        opacity: 1;
        min-height: unset;
        background: transparent;
    }

    /* 无 PDF 记录的空状态占位卡片（维持视觉平衡与严格等宽） */
    .pdf-empty-placeholder {
        width: 100% !important;
        min-width: 0 !important;
        max-width: 100% !important;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        background: rgba(255, 255, 255, 0.02);
        border: 1px dashed rgba(255, 255, 255, 0.12);
        border-radius: 6px;
        color: #78909c;
        text-align: center;
        padding: 24px;
        box-sizing: border-box !important;
    }
    .pdf-empty-placeholder .empty-icon {
        font-size: 36px;
        margin-bottom: 8px;
        opacity: 0.6;
    }
    .pdf-empty-placeholder .empty-title {
        font-size: 13.5px;
        font-weight: 600;
        color: #90a4ae;
        margin-bottom: 4px;
    }
    .pdf-empty-placeholder .empty-desc {
        font-size: 12px;
        color: #607d8b;
    }

    /* 底部分页控制栏：所有元素（文字、下拉框、上一页、数字输入框、下一页）尺寸高度严格统一为 38px，垂直基线完美居中对齐 */
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) {
        display: flex !important;
        align-items: center !important;
        margin-top: 4px !important;
        margin-bottom: 8px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) > div[data-testid="stColumn"] {
        display: flex !important;
        flex-direction: column !important;
        justify-content: center !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stWidgetLabel"] {
        display: none !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) button,
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stNumberInputContainer"],
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stNumberInput"] > div,
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stNumberInput"] input,
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stSelectbox"] > div > div,
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-baseweb="select"] > div {
        height: 38px !important;
        min-height: 38px !important;
        max-height: 38px !important;
        box-sizing: border-box !important;
        font-size: 13px !important;
        border-radius: 6px !important;
        display: flex !important;
        align-items: center !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) button {
        padding: 0 8px !important;
        justify-content: center !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) button p {
        font-size: 13px !important;
        line-height: normal !important;
        font-weight: 600 !important;
        margin: 0 !important;
    }
</style>
""", unsafe_allow_html=True)


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


class DBReader:
    """SQLite 数据库读取与分页助手"""
    def __init__(self, db_path: str):
        self.db_path = db_path

    def get_connection(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def get_stats(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM resources")
            total = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
            has_pdf_record = cursor.fetchone()[0]
            
            cursor.execute("SELECT DISTINCT source FROM resources WHERE source IS NOT NULL AND source != ''")
            sources = [r[0] for r in cursor.fetchall()]
            
            cursor.execute("SELECT DISTINCT category FROM resources WHERE category IS NOT NULL AND category != ''")
            categories = [r[0] for r in cursor.fetchall()]
            
            return {
                "total": total,
                "has_pdf_record": has_pdf_record,
                "sources": sorted(sources),
                "categories": sorted(categories),
            }

    def query_records(self, keyword="", source="全部", category="全部", pdf_filter="全部", sort_by="最新入库 (ID ↓)", page=1, page_size=20):
        conditions = []
        params = []

        if keyword.strip():
            kw = f"%{keyword.strip()}%"
            conditions.append("(title LIKE ? OR url LIKE ? OR resource_link LIKE ? OR pikpak_link LIKE ?)")
            params.extend([kw, kw, kw, kw])

        if source != "全部":
            conditions.append("source = ?")
            params.append(source)

        if category != "全部":
            conditions.append("category = ?")
            params.append(category)

        if pdf_filter == "仅有本地 PDF 路径":
            conditions.append("pdf_path IS NOT NULL AND pdf_path != ''")
        elif pdf_filter == "无 PDF 路径":
            conditions.append("(pdf_path IS NULL OR pdf_path = '')")

        where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        # 排序规则映射
        sort_mapping = {
            "最新入库 (ID ↓)": "id DESC",
            "最早入库 (ID ↑)": "id ASC",
            "发布时间 (新→旧)": "CASE WHEN publish_time IS NULL OR publish_time = '' THEN 1 ELSE 0 END, publish_time DESC, id DESC",
            "发布时间 (旧→新)": "CASE WHEN publish_time IS NULL OR publish_time = '' THEN 1 ELSE 0 END, publish_time ASC, id ASC",
            "标题名称 (A→Z)": "title ASC",
            "标题名称 (Z→A)": "title DESC",
        }
        order_clause = sort_mapping.get(sort_by, "id DESC")

        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 统计符合条件的总记录数
            count_sql = f"SELECT COUNT(*) FROM resources {where_clause}"
            cursor.execute(count_sql, params)
            filtered_count = cursor.fetchone()[0]

            # 查询分页数据
            offset = (page - 1) * page_size
            data_sql = f"""
                SELECT id, title, category, source, size, resource_format, url, resource_link, pikpak_link, pdf_path, publish_time
                FROM resources
                {where_clause}
                ORDER BY {order_clause}
                LIMIT ? OFFSET ?
            """
            cursor.execute(data_sql, params + [page_size, offset])
            rows = cursor.fetchall()
            
            columns = ["id", "title", "category", "source", "size", "format", "url", "resource_link", "pikpak_link", "pdf_path", "publish_time"]
            df = pd.DataFrame(rows, columns=columns)
            return filtered_count, df


# ==================== 主流程 ====================
db_path = get_db_path()

if not os.path.exists(db_path):
    st.error(f"❌ 找不到数据库文件: `{db_path}`，请确认数据库路径配置！")
    st.stop()

db_reader = DBReader(db_path)

try:
    stats = db_reader.get_stats()
except Exception as e:
    st.error(f"读取数据库统计信息失败: {e}")
    st.stop()

# 顶部单行状态栏（指标与数据库信息合并展示，右侧嵌入原生 Deploy/菜单）
st.markdown(
    f"""
    <div class="top-status-bar">
        <div class="status-meta">
            <span>📁 数据库: <code>{os.path.basename(db_path)}</code></span>
            <span>📦 PDF: <code>{os.path.relpath(PDF_BASE_DIR, PROJECT_ROOT)}</code></span>
        </div>
        <div class="status-divider"></div>
        <div class="status-item">
            <span class="status-label">📊 总记录数</span>
            <span class="status-val">{stats['total']:,}</span>
        </div>
        <div class="status-divider"></div>
        <div class="status-item">
            <span class="status-label">📁 含 PDF</span>
            <span class="status-val">{stats['has_pdf_record']:,}</span>
        </div>
        <div class="status-divider"></div>
        <div class="status-item">
            <span class="status-label">🌐 来源渠道</span>
            <span class="status-val">{len(stats["sources"])}</span>
        </div>
        <div class="status-divider"></div>
        <div class="status-item">
            <span class="status-label">📑 内容分类</span>
            <span class="status-val">{len(stats["categories"])}</span>
        </div>
        <div class="status-spacer"></div>
    </div>
    """,
    unsafe_allow_html=True
)

# 动态将原生 Header 挂载至顶部状态栏，彻底消除独立悬浮
components.html(
    """
    <script>
    function placeHeaderInToolbar() {
        try {
            const pDoc = window.parent.document;
            const header = pDoc.querySelector('header[data-testid="stHeader"], .stAppHeader');
            const target = pDoc.querySelector('.top-status-bar');
            if (header && target && !target.contains(header)) {
                header.style.position = 'static';
                header.style.height = 'auto';
                header.style.minHeight = '0';
                header.style.margin = '0';
                header.style.padding = '0';
                header.style.background = 'transparent';
                header.style.display = 'flex';
                header.style.alignItems = 'center';
                target.appendChild(header);
            }
        } catch (e) {}
    }
    placeHeaderInToolbar();
    setTimeout(placeHeaderInToolbar, 50);
    setTimeout(placeHeaderInToolbar, 200);
    setTimeout(placeHeaderInToolbar, 600);
    </script>
    """,
    height=0,
    width=0
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
if "f_sort" not in st.session_state:
    st.session_state.f_sort = "最新入库 (ID ↓)"
if "f_layout" not in st.session_state:
    st.session_state.f_layout = "双列画廊 (推荐)"
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

# 排序选项配置
sort_options = ["最新入库 (ID ↓)", "最早入库 (ID ↑)", "发布时间 (新→旧)", "发布时间 (旧→新)", "标题名称 (A→Z)", "标题名称 (Z→A)"]

# 筛选条件变化双重保险机制
current_filters_hash = f"{st.session_state.f_keyword}_{st.session_state.f_source}_{st.session_state.f_category}_{st.session_state.f_pdf_filter}_{st.session_state.f_sort}_{st.session_state.f_page_size}"
if st.session_state.get("last_filters_hash") != current_filters_hash:
    st.session_state.last_filters_hash = current_filters_hash
    st.session_state.current_page = 1
    st.session_state.top_jump = 1

# 查询数据
total_records, df = db_reader.query_records(
    keyword=st.session_state.f_keyword,
    source=st.session_state.f_source,
    category=st.session_state.f_category,
    pdf_filter=st.session_state.f_pdf_filter,
    sort_by=st.session_state.f_sort,
    page=st.session_state.current_page,
    page_size=st.session_state.f_page_size
)

page_size = st.session_state.f_page_size
layout_mode = st.session_state.f_layout
total_pages = max(1, (total_records + page_size - 1) // page_size)

# 如果页码超界，重查第 1 页
if total_records > 0 and st.session_state.current_page > total_pages:
    st.session_state.current_page = 1
    st.session_state.top_jump = 1
    total_records, df = db_reader.query_records(
        keyword=st.session_state.f_keyword,
        source=st.session_state.f_source,
        category=st.session_state.f_category,
        pdf_filter=st.session_state.f_pdf_filter,
        sort_by=st.session_state.f_sort,
        page=1,
        page_size=page_size
    )

# 检查每条记录的本地 PDF 实际存在情况
if not df.empty:
    def check_pdf_exists(path):
        resolved = resolve_pdf_path(path)
        return "✅ 已存在" if resolved else ("⚠️ 路径缺失" if path else "❌ 无文件")
    
    df["pdf_status"] = df["pdf_path"].apply(check_pdf_exists)


PDF_THUMB_CACHE_DIR = os.path.join(PROJECT_ROOT, "cache", "pdf_thumbs")
os.makedirs(PDF_THUMB_CACHE_DIR, exist_ok=True)


@st.cache_data(max_entries=600, ttl=3600, show_spinner=False)
def load_pdf_pages_as_base64(file_path: str, mtime: float, dpi: int = 105, quality: int = 75) -> list:
    """使用多级持久化磁盘缓存与 PyMuPDF 高性能将 PDF 渲染为高清图片"""
    try:
        key = hashlib.md5(f"{file_path}_{mtime}_{dpi}_{quality}".encode("utf-8")).hexdigest()
        
        # 1. 优先尝试从本地磁盘缩略图缓存快速读取
        images = []
        i = 0
        while True:
            cpath = os.path.join(PDF_THUMB_CACHE_DIR, f"{key}_{i}.jpg")
            if os.path.exists(cpath):
                with open(cpath, "rb") as f:
                    images.append(base64.b64encode(f.read()).decode("utf-8"))
                i += 1
            else:
                break
                
        if images:
            return images

        # 2. 缓存未命中：使用 PyMuPDF 光栅化并写入持久化磁盘缓存
        import pymupdf
        doc = pymupdf.open(file_path)
        images = []
        for i, page in enumerate(doc):
            cpath = os.path.join(PDF_THUMB_CACHE_DIR, f"{key}_{i}.jpg")
            pix = page.get_pixmap(dpi=dpi)
            pix.save(cpath, "jpeg", quality)
            with open(cpath, "rb") as f:
                images.append(base64.b64encode(f.read()).decode("utf-8"))
        doc.close()
        return images
    except Exception:
        return []


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
    st.markdown(
        f"""
        <div class="card-title-row">
            <span class="card-id-badge">#{item_id}</span>
            <span class="card-title-text" title="{title}">{title}</span>
        </div>
        """,
        unsafe_allow_html=True
    )
    
    # 2. 收集可用操作按钮
    action_buttons = []
    if url:
        action_buttons.append(("link", "🌐 原网页", url))
    if resource_link:
        action_buttons.append(("link", "🧲 资源链接", resource_link))
    if pikpak_link:
        action_buttons.append(("link", "📦 PikPak", pikpak_link))
    if resolved_pdf:
        action_buttons.append(("btn", "📁 本地定位", resolved_pdf))

    # 3. 标签徽章与元信息 HTML
    badge_list = [f'<span class="badge badge-source">{source}</span>']
    if category and category != "-" and category != "未分类":
        badge_list.append(f'<span class="badge badge-category">{category}</span>')
    if fmt and fmt != "-":
        badge_list.append(f'<span class="badge badge-format">{fmt}</span>')
    
    badges_str = "".join(badge_list)
    meta_html = f'''
    <div class="card-meta-row">
        <div>{badges_str}</div>
        <span class="card-meta-divider"></span>
        <span class="card-meta-item">📅 发布: {publish_time}</span>
        <span class="card-meta-item">💾 大小: {size}</span>
    </div>
    '''

    # 4. 元信息与操作按钮整合单行展示
    if action_buttons:
        num_btns = len(action_buttons)
        col_weights = [1] + [1] * num_btns
        card_cols = st.columns(col_weights, vertical_alignment="center")
        
        with card_cols[0]:
            st.markdown(meta_html, unsafe_allow_html=True)
            
        for idx, (b_type, b_label, b_val) in enumerate(action_buttons):
            with card_cols[idx + 1]:
                if b_type == "link":
                    st.link_button(b_label, b_val, use_container_width=False)
                elif b_type == "btn":
                    if st.button(b_label, key=f"loc_{item_id}", use_container_width=False):
                        open_in_system(b_val)
    else:
        st.markdown(meta_html, unsafe_allow_html=True)
    
    # 5. PDF 预览区域 / 无 PDF 占位容器（初始上滑 200px，且支持鼠标自由上下滑动查看全部内容）
    if resolved_pdf:
        try:
            mtime = os.path.getmtime(resolved_pdf)
            page_images = load_pdf_pages_as_base64(resolved_pdf, mtime)
            if page_images:
                imgs_html_list = []
                placeholder_pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
                for p_idx, b64_img in enumerate(page_images):
                    data_uri = f"data:image/jpeg;base64,{b64_img}"
                    # 前 2 个卡片的第一页直接立即挂载，其余卡片与后续页面走视口 IntersectionObserver 懒加载
                    if card_index < 2 and p_idx == 0:
                        imgs_html_list.append(
                            f'<img class="pdf-page-img loaded" src="{data_uri}" loading="lazy" />'
                        )
                    else:
                        imgs_html_list.append(
                            f'<img class="pdf-page-img lazy-pdf-img" src="{placeholder_pixel}" data-src="{data_uri}" loading="lazy" />'
                        )
                imgs_html = "".join(imgs_html_list)
                pdf_display = f'''
                <div class="pdf-scroll-container" id="pdf_scroll_{item_id}" style="height: {iframe_height}px;">
                    {imgs_html}
                </div>
                '''
                st.markdown(pdf_display, unsafe_allow_html=True)
            else:
                # 备用方案：若图片解析异常则走原 iframe 预览
                base64_pdf = load_pdf_as_base64(resolved_pdf, mtime)
                st.markdown(f'''
                <div class="pdf-scroll-container" style="height: {iframe_height}px;">
                    <iframe src="data:application/pdf;base64,{base64_pdf}#toolbar=0&navpanes=0" 
                            loading="lazy"
                            width="100%" 
                            height="{iframe_height}px" 
                            type="application/pdf"
                            style="border: none; display: block; height: {iframe_height}px;">
                    </iframe>
                </div>
                ''', unsafe_allow_html=True)
        except Exception as e:
            st.error(f"读取 PDF 文件失败: {e}")
    else:
        # 空状态占位高度与 PDF 预览完全一致，保持网格完美对齐
        placeholder_height = iframe_height
        if raw_pdf_path:
            status_title = "PDF 路径未找到"
            status_desc = f"登记路径: <code>{raw_pdf_path}</code>"
        else:
            status_title = "未关联本地 PDF 文件"
            status_desc = "可通过上方「原网页」或「资源链接」查看详情"
        
        placeholder_html = f'''
        <div class="pdf-empty-placeholder" style="height: {placeholder_height}px;">
            <div class="empty-icon">📄</div>
            <div class="empty-title">{status_title}</div>
            <div class="empty-desc">{status_desc}</div>
        </div>
        '''
        st.markdown(placeholder_html, unsafe_allow_html=True)


def render_pagination(key_prefix: str = "bottom"):
    """通用底部分页控制条"""
    p_col1, p_col2, p_col3, p_col4, p_col5 = st.columns([3.0, 1.1, 1.0, 1.0, 1.0], vertical_alignment="center")
    with p_col1:
        st.markdown(
            f"<div class='pagination-text'>共找到 <span style='color:#4caf50;font-weight:700;margin:0 4px;'>{total_records:,}</span> 条数据（第 <b>{st.session_state.current_page}</b> / {total_pages} 页）</div>",
            unsafe_allow_html=True
        )
    with p_col2:
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
    with p_col3:
        if st.button("⬅️ 上一页", key=f"{key_prefix}_prev", disabled=(st.session_state.current_page <= 1), use_container_width=True):
            st.session_state.current_page -= 1
            st.session_state.top_jump = st.session_state.current_page
            st.session_state.bottom_jump = st.session_state.current_page
            st.rerun()
    with p_col4:
        page_input = st.number_input(
            "跳转页码",
            min_value=1,
            max_value=total_pages,
            value=st.session_state.current_page,
            key=f"{key_prefix}_jump",
            label_visibility="collapsed"
        )
        if page_input != st.session_state.current_page:
            st.session_state.current_page = page_input
            st.session_state.top_jump = page_input
            st.session_state.bottom_jump = page_input
            st.rerun()
    with p_col5:
        if st.button("下一页 ➡️", key=f"{key_prefix}_next", disabled=(st.session_state.current_page >= total_pages), use_container_width=True):
            st.session_state.current_page += 1
            st.session_state.top_jump = st.session_state.current_page
            st.session_state.bottom_jump = st.session_state.current_page
            st.rerun()


# ==================== 顶部紧凑单行整合工具栏（筛选 + 排版 + 分页，无背景框） ====================
with st.container():
    f_cols = st.columns([2.0, 0.85, 0.85, 0.9, 1.05, 0.95, 0.7, 1.3, 0.65, 0.65, 0.65], vertical_alignment="bottom")
    with f_cols[0]:
        st.text_input("🔤 关键词搜索", placeholder="输入标题 / 链接 / 磁力...", key="f_keyword", on_change=reset_page)
    with f_cols[1]:
        source_options = ["全部"] + stats["sources"]
        st.selectbox("来源渠道", source_options, key="f_source", on_change=reset_page)
    with f_cols[2]:
        category_options = ["全部"] + stats["categories"]
        st.selectbox("内容分类", category_options, key="f_category", on_change=reset_page)
    with f_cols[3]:
        pdf_filter_options = ["全部", "仅有本地 PDF 路径", "无 PDF 路径"]
        st.selectbox("📁 PDF状态", pdf_filter_options, key="f_pdf_filter", on_change=reset_page)
    with f_cols[4]:
        st.selectbox("🔃 排序方式", sort_options, key="f_sort", on_change=reset_page)
    with f_cols[5]:
        st.selectbox("🎴 排版模式", ["双列画廊 (推荐)", "单列大图", "三列紧凑", "📊 纯表格视图"], key="f_layout")
    with f_cols[6]:
        page_size_opts = [10, 20, 30, 40, 50]
        cur_idx = page_size_opts.index(st.session_state.f_page_size) if st.session_state.f_page_size in page_size_opts else 0
        st.selectbox(
            "每页条数",
            options=page_size_opts,
            index=cur_idx,
            key="f_page_size",
            format_func=lambda x: f"{x} 条/页",
            on_change=on_top_page_size_change
        )
    with f_cols[7]:
        st.markdown(
            f"""
            <div class="toolbar-stat-badge" title="共 {total_records:,} 条数据 (第 {st.session_state.current_page} / {total_pages} 页)">
                共 <span class="stat-num">{total_records:,}</span> 条 (<span class="stat-page">{st.session_state.current_page}</span>/{total_pages}页)
            </div>
            """,
            unsafe_allow_html=True
        )
    with f_cols[8]:
        if st.button("⬅️ 上页", key="top_prev", disabled=(st.session_state.current_page <= 1), use_container_width=True):
            st.session_state.current_page -= 1
            st.session_state.top_jump = st.session_state.current_page
            st.session_state.bottom_jump = st.session_state.current_page
            st.rerun()
    with f_cols[9]:
        page_input = st.number_input(
            "跳转页码",
            min_value=1,
            max_value=total_pages,
            value=st.session_state.current_page,
            key="top_jump",
            label_visibility="collapsed"
        )
        if page_input != st.session_state.current_page:
            st.session_state.current_page = page_input
            st.session_state.top_jump = page_input
            st.session_state.bottom_jump = page_input
            st.rerun()
    with f_cols[10]:
        if st.button("下页 ➡️", key="top_next", disabled=(st.session_state.current_page >= total_pages), use_container_width=True):
            st.session_state.current_page += 1
            st.session_state.top_jump = st.session_state.current_page
            st.session_state.bottom_jump = st.session_state.current_page
            st.rerun()

# 顶部工具栏与内容区之间的极简单线分隔
st.markdown('<div class="toolbar-divider"></div>', unsafe_allow_html=True)

if df.empty:
    st.info("💡 没有找到匹配条件的记录，请尝试调整搜索或筛选条件。")

else:
    if layout_mode == "📊 纯表格视图":
        # 纯表格模式
        display_df = df[["id", "title", "source", "category", "size", "url", "resource_link", "pdf_status"]].copy()
        column_config = {
            "id": st.column_config.NumberColumn("ID", width="small"),
            "title": st.column_config.TextColumn("标题", width="medium"),
            "source": st.column_config.TextColumn("来源", width="small"),
            "category": st.column_config.TextColumn("分类", width="small"),
            "size": st.column_config.TextColumn("大小", width="small"),
            "url": st.column_config.LinkColumn("🌐 详情网页", display_text="打开网页", width="medium"),
            "resource_link": st.column_config.LinkColumn("🧲 资源链接", display_text="资源链接", width="medium"),
            "pdf_status": st.column_config.TextColumn("📁 PDF", width="small"),
        }
        st.dataframe(
            display_df,
            column_config=column_config,
            use_container_width=True,
            hide_index=True
        )
    elif layout_mode == "单列大图":
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

# 注入 JavaScript：基于 IntersectionObserver 视口真懒加载 + 内部自由滑动动态渲染 + 初始滚动 200px
components.html(
    """
    <script>
    (function() {
        function initPdfViewportLazyLoading() {
            try {
                const pDoc = window.parent.document;
                if (!pDoc) return;

                function loadSingleImage(img) {
                    if (img && img.dataset.src && (!img.src || img.src !== img.dataset.src)) {
                        img.src = img.dataset.src;
                        img.onload = () => {
                            img.classList.remove('lazy-pdf-img');
                            img.classList.add('loaded');
                            checkContainerScroll(img.closest('.pdf-scroll-container'));
                        };
                        img.onerror = () => {
                            img.classList.remove('lazy-pdf-img');
                            img.classList.add('loaded');
                        };
                    }
                }

                function loadAllImagesInContainer(container) {
                    if (!container) return;
                    const lazyImgs = container.querySelectorAll('img.lazy-pdf-img, img[data-src]');
                    lazyImgs.forEach(img => {
                        loadSingleImage(img);
                        try {
                            if (window._pdfImgObserver) window._pdfImgObserver.unobserve(img);
                        } catch(e) {}
                    });
                }

                function checkContainerScroll(container) {
                    if (!container || container.dataset.initScrolled === "1") return;
                    const firstImg = container.querySelector('.pdf-page-img');
                    if (firstImg && firstImg.complete && firstImg.naturalHeight > 0) {
                        if (container.scrollHeight > container.clientHeight) {
                            container.scrollTop = 200;
                            container.dataset.initScrolled = "1";
                        }
                    }
                }

                // 1. 初始化 IntersectionObserver（提前 300px 预加载，确保滑到时已加载完毕）
                if (!window._pdfImgObserver) {
                    window._pdfImgObserver = new IntersectionObserver((entries, observer) => {
                        entries.forEach(entry => {
                            if (entry.isIntersecting) {
                                const img = entry.target;
                                loadSingleImage(img);
                                observer.unobserve(img);
                            }
                        });
                    }, {
                        root: null,
                        rootMargin: '300px 0px 300px 0px',
                        threshold: 0.01
                    });
                }

                // 2. 扫描并观察所有懒加载图片
                const lazyImages = pDoc.querySelectorAll('img.lazy-pdf-img');
                lazyImages.forEach(img => window._pdfImgObserver.observe(img));

                // 3. 为所有卡片容器绑定内部滚动事件（用户在容器内向下滑动时，即刻动态渲染容器内后续所有页码）
                const containers = pDoc.querySelectorAll('.pdf-scroll-container');
                containers.forEach(container => {
                    if (!container.dataset.scrollBound) {
                        container.dataset.scrollBound = "1";
                        container.addEventListener('scroll', () => {
                            loadAllImagesInContainer(container);
                        }, { passive: true });
                    }
                    checkContainerScroll(container);
                });
            } catch(e) {}
        }

        initPdfViewportLazyLoading();
        const timers = [30, 80, 150, 300, 600, 1200, 2000];
        timers.forEach(t => setTimeout(initPdfViewportLazyLoading, t));

        try {
            if (!window._pdfMutationObserver) {
                window._pdfMutationObserver = new MutationObserver(initPdfViewportLazyLoading);
                window._pdfMutationObserver.observe(window.parent.document.body, { childList: true, subtree: true });
            }
        } catch(e) {}
    })();
    </script>
    """,
    height=0,
    width=0
)


