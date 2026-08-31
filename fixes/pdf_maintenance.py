"""PDF 文件全生命周期维护与数据库同步工具合集 (fixes/pdf_maintenance.py)

本脚本整合了 PDF 物理文件与 SQLite 数据库之间的一致性检查、路径规范、异常修复、缺失重建、孤儿隔离、智能关联、脏数据清理及多维查重去重等全生命周期维护功能。

包含以下 8 大核心子命令与维护流程：

1. check-dates: PDF 文件与数据库发布日期比对审计
   - 功能用途: 递归扫描本地 pdf/ 目录下的全部物理 PDF 文件，提取文件名中的日期与标题，并与数据库中对应的 publish_time 及记录信息进行比对。
   - 产出物: 自动生成 Markdown 审计报告 (pdf_date_check_report.md)，列出日期不符、数据库中未找到 (孤立文件)、多重匹配冲突等明细列表。

2. fix-paths: PDF 文件名日期修正与年份目录纠偏
   - Phase 1 (Unknown_Year 修复): 扫描 pdf/Unknown_Year 目录下以 Unknown_Date 开头的 PDF，根据数据库记录的有效 publish_time 将其重命名并迁移至对应年份文件夹（如 pdf/2025/）。
   - Phase 2 (全量一致性纠偏): 递归扫描所有年份目录，比对物理文件名日期与数据库日期，自动修正文件名日期前缀、移动至正确年份目录并同步更新数据库中的 pdf_path 路径。

3. redownload: 重新抓取渲染体积过小 (<20KB) 的损坏 PDF
   - 功能用途: 扫描物理目录中体积小于 20KB 的异常 PDF 文件（通常因反爬拦截、页面 404 或未加载完全导致）。
   - 修复方式: 拉起 Playwright 无头浏览器重新访问源 URL，注入针对性的广告屏蔽脚本，重新渲染生成标准 A4 边距 PDF 并覆盖旧文件。

4. rebuild: 重建缺失 PDF 文件与路径相对化 (多线程并发)
   - 路径相对化: 扫描数据库中所有包含 pdf_path 的有效记录，将绝对路径统一转为相对路径 (如 pdf/2025/xxx.pdf)。
   - 缺失并发重建: 找出物理文件不存在的有效记录，支持多线程 (默认 4-6 线程) Playwright 并发请求渲染生成 PDF，遇到 404 自动清理失效路径，并实时记录进度至 logs/pdf_redownload_progress.json。
   - 选项: 支持 --skip-download 仅执行路径相对化与数据库纠偏；支持 --workers 指定并发线程数。

5. orphan: 多余/孤立 PDF 文件管理与还原
   - 模式 1 (检查多余): 扫描各年份目录下的 PDF，比对数据库中是否有匹配记录；将数据库中无记录的多余/废弃 PDF 隔离移至 /pdf 根目录。
   - 模式 2 (恢复归位): 扫描 /pdf 根目录下的隔离 PDF，通过文件名日期或数据库标题索引智能分析归属，自动移回对应年份子目录并补齐日期前缀。

6. associate: 扫描磁盘未关联/断链 PDF 智能回填数据库
   - 功能用途: 扫描磁盘物理 PDF 文件，精准比对数据库 resources 表，找出物理文件存在但数据库 pdf_path 为空或断链的记录，通过标题与站点来源自动关联回填，免去重复下载。

7. clean-missing: 清理数据库中对应物理 PDF 已丢失的残留脏记录
   - 功能用途: 反向扫描数据库，检测 pdf_path 指向的物理文件是否真实存在（支持 --scope unknown 仅检查 Unknown_Year 或 --scope all 检查全量）。
   - 清理机制: 批量删除物理文件已不存在的数据库记录，并在删除后自动执行 VACUUM 回收数据库物理空间。

8. dedup: PDF 物理文件多维查重、去重与数据库引用纠偏
   - 功能用途: 支持基于内容 MD5 哈希查重、文件名/标题变体 (_1.pdf) 查重及数据库 pdf_path 共享检测。
   - 智能处理: 优先保留规范命名与大文件版本，清理多余物理副本时自动将数据库指向重定向至保留的主文件，杜绝断链。

用法与命令示例:
  python fixes/pdf_maintenance.py                             # 交互式主菜单 (包含 1-8 选项)
  python fixes/pdf_maintenance.py check-dates                 # 运行日期检查并生成报告
  python fixes/pdf_maintenance.py fix-paths                   # 预览路径与文件名修复计划 (Dry Run)
  python fixes/pdf_maintenance.py fix-paths --run             # 正式执行路径与文件名纠偏
  python fixes/pdf_maintenance.py redownload --run            # 重新下载覆盖 <20KB 的 PDF 文件
  python fixes/pdf_maintenance.py rebuild --run               # 路径相对化并多线程并发重新生成缺失 PDF
  python fixes/pdf_maintenance.py rebuild --run --workers 6   # 6 线程并发重建缺失 PDF
  python fixes/pdf_maintenance.py rebuild --run --skip-download # 仅执行路径相对化更新
  python fixes/pdf_maintenance.py orphan                      # 孤儿文件隔离/还原交互菜单
  python fixes/pdf_maintenance.py associate                   # 预览孤儿文件与断链记录关联计划 (Dry Run)
  python fixes/pdf_maintenance.py associate --run             # 正式将未关联物理文件回填入库
  python fixes/pdf_maintenance.py clean-missing --run         # 清理物理文件已丢失的数据库记录
  python fixes/pdf_maintenance.py dedup                       # PDF 查重与去重交互管理
  python fixes/pdf_maintenance.py dedup --mode hash --run     # 执行 MD5 哈希查重与去重纠偏
"""

import os
import re
import sys
import json
import base64
import sqlite3
import argparse
import random
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse, urlunparse

# 将项目根目录加入 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import PDF_BASE_DIR
from utils import setup_console_utf8
from utils.browser_factory import browser_factory
from utils.metadata_parser import sanitize_filename
from utils.pdf_utils import parse_filename, clean_title_suffix, to_relative_path, generate_unique_path
from utils.pdf_generator import PDFGenerator, PDFRenderConfig
from fixes.db_utils import (
    setup_fixes_module,
    get_connection,
    get_db_path,
    backup_db,
    get_export_dir,
    get_timestamp,
    export_records_to_db,
    export_to_csv,
    delete_records_cascade_pdf,
    print_banner,
    print_section,
    print_step,
    print_success,
    print_warning,
    print_error,
    confirm_action,
    pause_for_user,
)

setup_fixes_module()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRESS_FILE = os.path.join(PROJECT_ROOT, "logs", "pdf_redownload_progress.json")

date_regex = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ===================================================================
# 站点配置与永久域名/镜像域名管理
# ===================================================================
SITE_CONFIG = {
    "datang": {
        "main_domain": "https://dtbt7.com",
        "domain_pattern": r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)',
        "cache_name": "datang_domains.json",
    },
    "dashen": {
        "main_domain": "https://j4f4.com",
        "domain_pattern": r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)',
        "cache_name": "dashen_domains.json",
    },
    "jingpin": {
        "main_domain": "https://jpbt3.com",
        "domain_pattern": r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)',
        "cache_name": "jingpin_domains.json",
    },
    "tanhua": {
        "main_domain": "https://thbt8.com",
        "domain_pattern": r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)',
        "cache_name": "tanhua_domains.json",
    },
    "taose": {
        "main_domain": "https://taosebt.com",
        "domain_pattern": r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)',
        "cache_name": "taose_domains.json",
    },
    "mianfei_guochan": {
        "main_domain": "https://mfgc3.com",
        "domain_pattern": r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)',
        "cache_name": "mianfei_guochan_domains.json",
    },
    "madou": {
        "main_domain": "http://ypb.295282.xyz",
        "domain_pattern": r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)',
        "cache_name": "madou_domains.json",
    },
    "jingpin_toupai": {
        "main_domain": "",
        "domain_pattern": r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)',
        "cache_name": "jingpin_toupai_domains.json",
    },
}

CONFIG_MAP = {
    "seju": PDFRenderConfig(
        margin={"top": "20mm", "bottom": "20mm", "left": "20mm", "right": "20mm"}
    ),
    "gcbt": PDFRenderConfig(
        need_img_proxy=False,
        wait_until="domcontentloaded",
        pre_access_url=None,
        referer="https://gcbt.net/",
        need_lazy_scroll=True,
        emulate_media="screen",
        ad_selectors=[
            '.layui-layer', '.layui-layer-shade',
            '.modal', '.modal-backdrop',
            '.swal-overlay', '.swal-modal', '.swal2-container',
            '[id*="layui-layer"]'
        ],
        ad_block_js="""() => {
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
        }"""
    ),
    "madou": PDFRenderConfig(
        ad_selectors=[
            'div[style*="height:60px"]',
            'div[style*="height:55px"]',
            'div[style*="height:70px"]',
            '#bottom_float'
        ]
    ),
    "datang": PDFRenderConfig(
        ad_block_js="""() => {
            const breadcrumbs = document.querySelector('.breadcrumbs');
            if (breadcrumbs) {
                let prev = breadcrumbs.previousElementSibling;
                while (prev) {
                    if (prev.classList.contains('gs-isgood') && 
                        !prev.textContent.includes('永久地址') && 
                        !prev.textContent.includes('永久')) {
                        prev.remove();
                    }
                    prev = prev.previousElementSibling;
                }
            }
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float');
            if (bottomFloat) {
                bottomFloat.remove();
            }
        }"""
    ),
    "dashen": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            # 新版 SPA 横幅广告与底飘广告容器及元素
            '.hf-container',
            '#hf-container',
            '.hf-link',
            '.hf-img',
            '.dp-container',
            '#dp-container',
            '.dp-link',
            '.dp-img',
            # 合作伙伴广告区域与友情链接、页脚广告
            '.partner-grid',
            '.partner-links',
            '.site-footer',
            'footer.site-footer',
            # 旧版固定高度广告及浮动层
            'div[style*="height:60px"]',
            'div[style*="height:140px"]',
            'div[style*="height:150px"]',
            'div[style*="height:55px"]',
            'div[style*="height:70px"]',
            'div[style*="height:80px"]',
            'div[style*="height:95px"]',
            '#bottom_float',
            '.bottom_float',
            # 弹窗与遮罩层
            '.layui-layer',
            '.layui-layer-shade',
            '[id*="layui-layer"]',
            '.modal',
            '.modal-backdrop',
            '.swal-overlay',
            '.swal-modal',
            '.swal2-container',
        ],
        ad_block_js="""() => {
            // 1. 移除所有 iframe
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';

            // 2. 移除横幅广告与底飘广告
            document.querySelectorAll('.hf-container, #hf-container, .hf-link, .hf-img, .dp-container, #dp-container, .dp-link, .dp-img').forEach(el => el.remove());

            // 3. 移除合作伙伴广告卡片、友情链接及页脚广告
            document.querySelectorAll('.partner-grid, .partner-links').forEach(el => {
                const card = el.closest('.info-card') || el;
                card.remove();
            });
            document.querySelectorAll('.site-footer, footer.site-footer').forEach(el => el.remove());

            // 4. 移除旧版固定高度广告及浮动层
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:140px"], div[style*="height:150px"], div[style*="height:55px"], div[style*="height:70px"], div[style*="height:80px"], div[style*="height:95px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float');
            if (bottomFloat) bottomFloat.remove();

            // 5. 移除弹窗及遮罩层
            document.querySelectorAll('.layui-layer, .layui-layer-shade, [id*="layui-layer"], .modal, .modal-backdrop, .swal-overlay, .swal-modal, .swal2-container').forEach(el => el.remove());
        }""",
        ad_url_patterns=[
            r'(?:doubleclick|googleads|googlesyndication|google-analytics)\.com',
            r'(?:adservice|pagead2|partnerads)\.googlesyndication',
            r'(?:cas\.pm|syndication|adsystem)\.com',
            r'(?:googleadservices|googletagmanager)\.com',
            r'\.css\?ver=.*&(?:ad|ads|banner)',
            r'(?:popup|pop-under|popunder)',
            r'(?:layer|float)_?(?:ad|adv|ads)',
            r'/ad(?:s|sense|unit|server|frame|script)\.',
            r'(?:s\d+\.cnzz|cnzz\.com|h5\.cnzz)',
            r'(?:hm\.baidu|posbaidu|cpro\.baidu)',
            r'(?:tanx|alimama|mmstat)\.com',
            r'(?:qzs\.qq|qq\.com)/ad',
        ]
    ),
    "jingpin": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            'div[style*="height:60px"]',
            'div[style*="height:55px"]',
            'div[style*="height:70px"]',
            '#bottom_float',
            '.bottom_float',
            '.layui-layer',
            '.layui-layer-shade',
            '[id*="layui-layer"]',
            '.modal',
            '.modal-backdrop'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float');
            if (bottomFloat) {
                bottomFloat.remove();
            }
        }""",
        ad_url_patterns=[
            r'(?:doubleclick|googleads|googlesyndication|google-analytics)\.com',
            r'(?:adservice|pagead2|partnerads)\.googlesyndication',
            r'(?:cas\.pm|syndication|adsystem)\.com',
            r'(?:googleadservices|googletagmanager)\.com',
            r'\.css\?ver=.*&(?:ad|ads|banner)',
            r'(?:popup|pop-under|popunder)',
            r'(?:layer|float)_?(?:ad|adv|ads)',
            r'/ad(?:s|sense|unit|server|frame|script)\.',
            r'(?:s\d+\.cnzz|cnzz\.com|h5\.cnzz)',
            r'(?:hm\.baidu|posbaidu|cpro\.baidu)',
            r'(?:tanx|alimama|mmstat)\.com',
            r'(?:qzs\.qq|qq\.com)/ad',
        ]
    ),
    "tanhua": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            'div[style*="height:60px"]',
            'div[style*="height:55px"]',
            'div[style*="height:70px"]',
            '#bottom_float',
            '.bottom_float',
            '.layui-layer',
            '.layui-layer-shade',
            '[id*="layui-layer"]',
            '.modal',
            '.modal-backdrop'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float');
            if (bottomFloat) {
                bottomFloat.remove();
            }
        }""",
        ad_url_patterns=[
            r'(?:doubleclick|googleads|googlesyndication|google-analytics)\.com',
            r'(?:adservice|pagead2|partnerads)\.googlesyndication',
            r'(?:cas\.pm|syndication|adsystem)\.com',
            r'(?:googleadservices|googletagmanager)\.com',
            r'\.css\?ver=.*&(?:ad|ads|banner)',
            r'(?:popup|pop-under|popunder)',
            r'(?:layer|float)_?(?:ad|adv|ads)',
            r'/ad(?:s|sense|unit|server|frame|script)\.',
            r'(?:s\d+\.cnzz|cnzz\.com|h5\.cnzz)',
            r'(?:hm\.baidu|posbaidu|cpro\.baidu)',
            r'(?:tanx|alimama|mmstat)\.com',
            r'(?:qzs\.qq|qq\.com)/ad',
        ]
    ),
    "taose": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            'div[style*="height:60px"]',
            'div[style*="height:55px"]',
            'div[style*="height:70px"]',
            '#bottom_float',
            '.layui-layer',
            '.layui-layer-shade',
            '[id*="layui-layer"]',
            '.modal',
            '.modal-backdrop'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float');
            if (bottomFloat) {
                bottomFloat.remove();
            }
        }""",
        ad_url_patterns=[
            r'(?:doubleclick|googleads|googlesyndication|google-analytics)\.com',
            r'(?:adservice|pagead2|partnerads)\.googlesyndication',
            r'(?:cas\.pm|syndication|adsystem)\.com',
            r'(?:googleadservices|googletagmanager)\.com',
            r'\.css\?ver=.*&(?:ad|ads|banner)',
            r'(?:popup|pop-under|popunder)',
            r'(?:layer|float)_?(?:ad|adv|ads)',
            r'/ad(?:s|sense|unit|server|frame|script)\.',
            r'(?:s\d+\.cnzz|cnzz\.com|h5\.cnzz)',
            r'(?:hm\.baidu|posbaidu|cpro\.baidu)',
            r'(?:tanx|alimama|mmstat)\.com',
            r'(?:qzs\.qq|qq\.com)/ad',
        ]
    ),
    "mianfei_guochan": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            'div[style*="height:60px"]',
            'div[style*="height:55px"]',
            'div[style*="height:70px"]',
            '#bottom_float',
            '.bottom_float',
            '.layui-layer',
            '.layui-layer-shade',
            '[id*="layui-layer"]',
            '.modal',
            '.modal-backdrop'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float');
            if (bottomFloat) {
                bottomFloat.remove();
            }
        }""",
        ad_url_patterns=[
            r'(?:doubleclick|googleads|googlesyndication|google-analytics)\.com',
            r'(?:adservice|pagead2|partnerads)\.googlesyndication',
            r'(?:cas\.pm|syndication|adsystem)\.com',
            r'(?:googleadservices|googletagmanager)\.com',
            r'\.css\?ver=.*&(?:ad|ads|banner)',
            r'(?:popup|pop-under|popunder)',
            r'(?:layer|float)_?(?:ad|adv|ads)',
            r'/ad(?:s|sense|unit|server|frame|script)\.',
            r'(?:s\d+\.cnzz|cnzz\.com|h5\.cnzz)',
            r'(?:hm\.baidu|posbaidu|cpro\.baidu)',
            r'(?:tanx|alimama|mmstat)\.com',
            r'(?:qzs\.qq|qq\.com)/ad',
        ]
    ),
    "jingpin_toupai": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            'div[style*="height:60px"]', 
            'div[style*="height:55px"]', 
            'div[style*="height:70px"]',
            '#bottom_float',
            '.layui-layer',
            '.layui-layer-shade',
            '[id*="layui-layer"]',
            '.modal',
            '.modal-backdrop'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
        }"""
    )
}


def infer_source(source, url="", pdf_path="", title=""):
    """推断资源所属的爬虫站点标识"""
    if source and str(source).strip():
        return str(source).strip()
    if pdf_path:
        base = os.path.basename(pdf_path).lower()
        for s in SITE_CONFIG:
            if f"_{s}.pdf" in base or f"_{s}_" in base:
                return s
        for s in ("seju", "gcbt", "u3c3"):
            if f"_{s}.pdf" in base or f"_{s}_" in base:
                return s
    if url:
        netloc = urlparse(url).netloc.lower()
        for s, cfg in SITE_CONFIG.items():
            cached = load_cached_domains(s)
            if netloc in cached:
                return s
        if "seju.life" in netloc:
            return "seju"
        if "gcbt.net" in netloc:
            return "gcbt"
        if "u3c3.com" in netloc:
            return "u3c3"
        if "movie.php?id=" in url:
            return "madou"
        if "torrent/" in url:
            return "jingpin_toupai"
    return "datang"


def extract_domains_from_text(content: str, pattern: str = r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)') -> list:
    """从 HTML 或文本中提取镜像域名，支持明文正则与 Base64 编码"""
    if not content:
        return []
    found = set()
    for d in re.findall(pattern, content):
        found.add(d)
    b64_d_matches = re.findall(r'd\(["\']([A-Za-z0-9+/=]{4,})["\']\)', content)
    for b64_str in b64_d_matches:
        try:
            decoded = base64.b64decode(b64_str).decode('utf-8', errors='ignore')
            for d in re.findall(pattern, decoded):
                found.add(d)
        except Exception:
            pass
    general_b64 = re.findall(r'["\']([A-Za-z0-9+/=]{12,})["\']', content)
    for b64_str in general_b64:
        try:
            decoded = base64.b64decode(b64_str).decode('utf-8', errors='ignore')
            for d in re.findall(pattern, decoded):
                found.add(d)
        except Exception:
            pass
    return list(found)


def load_cached_domains(source: str) -> list:
    """从本地缓存加载有效域名列表"""
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
    cache_path = os.path.join(cache_dir, f"{source}_domains.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def save_cached_domains(source: str, domains: list):
    """保存域名列表到本地缓存"""
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{source}_domains.json")
    try:
        to_save = list(dict.fromkeys(domains))
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[-] 保存域名缓存失败: {e}")


def fetch_domains_from_permanent(source: str) -> list:
    """从站点的永久域名（main_domain）动态拉取并解密最新镜像域名列表"""
    cfg = SITE_CONFIG.get(source)
    if not cfg or not cfg.get("main_domain"):
        return []
    main_domain = cfg["main_domain"]
    pattern = cfg.get("domain_pattern", r'([a-z0-9]{2,10}\.\d{5,7}\.xyz)')
    print(f"  [*] 正在从 {source.upper()} 永久主站 {main_domain} 动态拉取最新镜像域名...")
    try:
        from curl_cffi import requests as cffi_requests
        from crawlers.base_crawler import DecryptMixin
        resp = cffi_requests.get(main_domain, timeout=15, impersonate="chrome120")
        if resp.status_code == 200:
            decrypted = DecryptMixin().decrypt_html(resp.text)
            content_to_parse = decrypted if decrypted else resp.text
            new_domains = extract_domains_from_text(content_to_parse, pattern)
            unique = [d for d in dict.fromkeys(new_domains) if d not in main_domain]
            if unique:
                save_cached_domains(source, unique)
                print(f"  [+] 成功从永久主站获取到 {len(unique)} 个最新镜像域名: {unique}")
                return unique
    except Exception as e:
        print(f"  [-] 从永久主站 {main_domain} 获取最新域名失败: {e}")
    return []


def get_latest_mirror_domains(source: str, exclude_domain: str = None) -> list:
    """获取最新可用的镜像域名列表（优先缓存，无有效则从永久域名拉取）"""
    cached = load_cached_domains(source)
    valid = [d for d in cached if d != exclude_domain] if exclude_domain else cached
    if valid:
        return valid
    
    # 缓存中没有或者排除后为空，从永久域名获取
    fresh = fetch_domains_from_permanent(source)
    if exclude_domain:
        fresh = [d for d in fresh if d != exclude_domain]
    return fresh


def replace_domain_in_url(url: str, new_domain: str) -> str:
    """将 URL 中的 host / netloc 替换为新的镜像域名"""
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    return urlunparse((scheme, new_domain, parsed.path, parsed.params, parsed.query, parsed.fragment))


def render_pdf_page(page, url: str, file_path: str, source: str, min_size_kb: float = 20.0):
    """使用 Playwright page 访问 url 并渲染 PDF 保存到 file_path。
    返回: (is_valid: bool, size_kb: float, status_msg: str, page_content: str)
    """
    config = CONFIG_MAP.get(source, PDFRenderConfig())
    try:
        try:
            page.unroute("**/*")
        except Exception:
            pass

        if config.ad_url_patterns:
            patterns = [re.compile(p, re.I) for p in config.ad_url_patterns]
            def ad_blocker(route):
                u = route.request.url
                for pat in patterns:
                    if pat.search(u):
                        route.abort()
                        return
                route.continue_()
            try:
                page.route("**/*", ad_blocker)
            except Exception:
                pass

        response = page.goto(url, timeout=35000, wait_until="domcontentloaded")
        time.sleep(2.5)

        if response and response.status in (404, 403, 500, 502, 503):
            return False, 0.0, f"HTTP {response.status}", ""

        page_content = ""
        try:
            page_content = page.content()
        except Exception:
            pass

        if page_content and ("正在检测" in page_content or "available_domain_html" in page_content or "403 Forbidden" in page_content):
            return False, 0.0, "检测到域名跳转拦截页", page_content

        try:
            js_code = "() => {\n"
            if config.ad_selectors:
                js_code += f"    const selectors = {config.ad_selectors};\n"
                js_code += "    selectors.forEach(sel => { document.querySelectorAll(sel).forEach(el => el.remove()); });\n"
            if config.ad_block_js:
                js_code += f"    try {{ ({config.ad_block_js})(); }} catch(e) {{ console.error(e); }}\n"
            js_code += "}"
            page.evaluate(js_code)
        except Exception:
            pass

        if config.emulate_media:
            try:
                page.emulate_media(media=config.emulate_media)
            except Exception:
                pass

        margin = getattr(config, "margin", {"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"})
        scale = getattr(config, "scale", 0.75)

        page.pdf(
            path=file_path,
            format="A4",
            scale=scale,
            print_background=True,
            margin=margin
        )

        if os.path.exists(file_path):
            new_size_kb = os.path.getsize(file_path) / 1024.0
            if new_size_kb >= min_size_kb:
                return True, new_size_kb, "成功", page_content
            else:
                return False, new_size_kb, f"体积依然过小 ({new_size_kb:.2f} KB < {min_size_kb} KB)", page_content
        else:
            return False, 0.0, "PDF 文件未在本地生成", page_content
    except Exception as e:
        return False, 0.0, f"渲染异常: {e}", ""


# ===================================================================
# 功能 1: check-dates - 检查 PDF 文件与数据库日期的匹配情况
# ===================================================================
def run_check_dates(args):
    db_path = get_db_path()
    pdf_base = PDF_BASE_DIR

    print("=" * 60)
    print("[*] 开始检查 PDF 文件与数据库日期的匹配情况...")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    if not os.path.exists(pdf_base):
        print(f"[-] 错误: PDF 根目录不存在: {pdf_base}")
        sys.exit(1)

    # 1. 扫描所有 PDF 物理文件
    print("[*] 正在扫描 PDF 目录...")
    pdf_files = []
    for root, dirs, files in os.walk(pdf_base):
        for f in files:
            if f.lower().endswith(".pdf"):
                pdf_files.append((f, os.path.join(root, f)))
    total_phys_files = len(pdf_files)
    print(f"[+] 扫描到 {total_phys_files} 个 PDF 物理文件。")

    # 2. 连接数据库
    print("[*] 正在加载数据库中的资源记录...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, publish_time, pdf_path, url FROM resources")
    db_rows = cursor.fetchall()
    conn.close()
    print(f"[+] 成功加载 {len(db_rows)} 条数据库记录。")

    # 3. 建立索引
    db_by_pdf_filename = defaultdict(list)
    db_by_title = defaultdict(list)
    for row in db_rows:
        r_id, r_title, r_publish_time, r_pdf_path, r_url = row
        pub_time = r_publish_time.strip() if r_publish_time else "Unknown_Date"
        if not pub_time:
            pub_time = "Unknown_Date"
        record = {"id": r_id, "title": r_title, "publish_time": pub_time,
                  "pdf_path": r_pdf_path, "url": r_url}
        if r_pdf_path:
            filename_part = os.path.basename(r_pdf_path.replace('\\', '/')).lower()
            db_by_pdf_filename[filename_part].append(record)
        if r_title:
            db_by_title[r_title.strip()].append(record)

    # 4. 比对
    print("[*] 正在比对文件与数据库...")
    results = {"matched_ok": [], "date_mismatch": [], "db_not_found": [], "multiple_conflict": []}

    for filename, full_path in pdf_files:
        fn_date, fn_title_part = parse_filename(filename)
        fn_clean_title = clean_title_suffix(fn_title_part)
        matched_records = []

        if filename.lower() in db_by_pdf_filename:
            matched_records = db_by_pdf_filename[filename.lower()]
        if not matched_records:
            if fn_title_part in db_by_title:
                matched_records = db_by_title[fn_title_part]
            elif fn_clean_title in db_by_title:
                matched_records = db_by_title[fn_clean_title]
        if not matched_records:
            results["db_not_found"].append({
                "filename": filename, "path": full_path,
                "fn_date": fn_date, "clean_title": fn_clean_title})
            continue

        unique_dates = list(set(r["publish_time"] for r in matched_records))
        if len(unique_dates) > 1:
            exact_by_path = []
            for r in matched_records:
                if r["pdf_path"]:
                    basename = os.path.basename(r["pdf_path"].replace('\\', '/')).lower()
                    if basename == filename.lower():
                        exact_by_path.append(r)
            if len(exact_by_path) == 1:
                record = exact_by_path[0]
                if fn_date == record["publish_time"]:
                    results["matched_ok"].append((filename, full_path, record))
                else:
                    results["date_mismatch"].append({
                        "filename": filename, "path": full_path,
                        "fn_date": fn_date, "db_date": record["publish_time"],
                        "record": record})
            else:
                results["multiple_conflict"].append({
                    "filename": filename, "path": full_path,
                    "fn_date": fn_date, "matched_records": matched_records})
        else:
            record = matched_records[0]
            db_date = unique_dates[0]
            cmp_fn_date = fn_date if fn_date else "Unknown_Date"
            cmp_db_date = db_date if db_date else "Unknown_Date"
            if cmp_fn_date == cmp_db_date:
                results["matched_ok"].append((filename, full_path, record))
            else:
                results["date_mismatch"].append({
                    "filename": filename, "path": full_path,
                    "fn_date": fn_date, "db_date": db_date,
                    "record": record, "all_matched_records": matched_records})

    # 5. 报告
    report_lines = [
        "# PDF 文件日期与数据库日期检查报告\n",
        f"- **检查时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **扫描 PDF 物理文件数**: {total_phys_files}",
        f"- **正常一致文件数**: {len(results['matched_ok'])}",
        f"- **日期不符文件数**: {len(results['date_mismatch'])}",
        f"- **数据库中未找到记录的文件数**: {len(results['db_not_found'])}",
        f"- **多重匹配冲突文件数**: {len(results['multiple_conflict'])}",
        "\n" + "=" * 40 + "\n"]
    if results["date_mismatch"]:
        report_lines.append("## 1. 日期不符文件列表\n")
        report_lines.append("| 序号 | 物理文件名 | 文件中提取日期 | 数据库中日期 | 数据库记录ID | 物理路径 |")
        report_lines.append("| --- | --- | --- | --- | --- | --- |")
        for i, item in enumerate(results["date_mismatch"], 1):
            rel_path = os.path.relpath(item["path"], pdf_base)
            report_lines.append(
                f"| {i} | {item['filename']} | {item['fn_date'] or '无'} | "
                f"{item['db_date']} | {item['record']['id']} | pdf/{rel_path} |")
        report_lines.append("\n")
    if results["db_not_found"]:
        report_lines.append("## 2. 数据库中未找到匹配记录的文件列表\n")
        report_lines.append("| 序号 | 物理文件名 | 提取日期 | 提取标题 | 物理路径 |")
        report_lines.append("| --- | --- | --- | --- | --- |")
        for i, item in enumerate(results["db_not_found"], 1):
            rel_path = os.path.relpath(item["path"], pdf_base)
            report_lines.append(
                f"| {i} | {item['filename']} | {item['fn_date'] or '无'} | "
                f"{item['clean_title']} | pdf/{rel_path} |")
        report_lines.append("\n")
    if results["multiple_conflict"]:
        report_lines.append("## 3. 多重匹配冲突文件列表\n")
        report_lines.append("| 序号 | 物理文件名 | 文件提取日期 | 匹配到的数据库日期 | 匹配到的记录ID列表 | 物理路径 |")
        report_lines.append("| --- | --- | --- | --- | --- | --- |")
        for i, item in enumerate(results["multiple_conflict"], 1):
            dates = [r["publish_time"] for r in item["matched_records"]]
            ids = [r["id"] for r in item["matched_records"]]
            rel_path = os.path.relpath(item["path"], pdf_base)
            report_lines.append(
                f"| {i} | {item['filename']} | {item['fn_date'] or '无'} | "
                f"{dates} | {ids} | pdf/{rel_path} |")
        report_lines.append("\n")

    ts = get_timestamp()
    report_path = os.path.join(get_export_dir(), f"pdf_date_check_report_{ts}.md")
    with open(report_path, "w", encoding="utf-8") as rf:
        rf.write("\n".join(report_lines))

    print("\n" + "=" * 60)
    print("                      检查结果摘要")
    print("=" * 60)
    print(f" 物理文件总数:                      {total_phys_files}")
    print(f" 正常一致文件数:                    {len(results['matched_ok'])}")
    print(f" 日期不符文件数 (需修复):            {len(results['date_mismatch'])}")
    print(f" 数据库中未找到 (孤立文件):          {len(results['db_not_found'])}")
    print(f" 多重匹配冲突 (需人工介入):          {len(results['multiple_conflict'])}")
    print("=" * 60)
    print(f"[+] 详细报告已生成至: {report_path}")
    print("=" * 60)


# ===================================================================
# 功能 2: fix-paths - 将 Unknown_Year 中的 PDF 按数据库日期移到正确年份文件夹
#          (Phase 1) + 全量扫描修复文件名日期不匹配 (Phase 2)
# ===================================================================
def run_fix_names_and_paths(args):
    db_path = get_db_path()
    pdf_base = PDF_BASE_DIR
    unknown_year_dir = os.path.join(pdf_base, "Unknown_Year")

    print("=" * 60)
    print(f"[*] 运行模式: {'【正式修复模式】' if args.run else '【预览模式 (Dry Run)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print(f"[*] 未知年份 PDF 目录: {unknown_year_dir}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    if not os.path.exists(unknown_year_dir):
        print(f"[-] 错误: 未知年份目录不存在: {unknown_year_dir}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    files = [f for f in os.listdir(unknown_year_dir) if f.endswith(".pdf")]
    total_files = len(files)
    print(f"[*] 扫描到 Unknown_Year 下的 PDF 文件数: {total_files}")

    stats = {"success": 0, "conflict": 0, "not_found": 0,
             "still_unknown_date": 0, "invalid_date_format": 0, "error": 0}
    move_plans = []

    print("[*] 正在分析文件匹配关系...")
    for idx, filename in enumerate(files, 1):
        if not filename.startswith("Unknown_Date_"):
            if args.verbose:
                print(f"[-] 跳过非 Unknown_Date 开头的文件: {filename}")
            continue

        title_part = filename[len("Unknown_Date_"):-4]
        if not title_part:
            if args.verbose:
                print(f"[-] 跳过空标题文件: {filename}")
            stats["error"] += 1
            continue

        clean_title = title_part
        suffix = ""
        cursor.execute("SELECT id, publish_time, url, pdf_path FROM resources WHERE title = ?", (clean_title,))
        matched_rows = cursor.fetchall()

        if not matched_rows:
            match = re.search(r"_(?P<num>\d+)$", title_part)
            if match:
                suffix = match.group(0)
                clean_title = title_part[: -len(suffix)]
                cursor.execute("SELECT id, publish_time, url, pdf_path FROM resources WHERE title = ?", (clean_title,))
                matched_rows = cursor.fetchall()

        if not matched_rows:
            cursor.execute("SELECT id, publish_time, url, pdf_path, title FROM resources WHERE title LIKE ?", (f"%{clean_title}%",))
            like_rows = cursor.fetchall()
            if len(like_rows) == 1:
                matched_rows = [(like_rows[0][0], like_rows[0][1], like_rows[0][2], like_rows[0][3])]
            elif len(like_rows) > 1:
                valid_like_rows = [r for r in like_rows if r[1] != 'Unknown_Date' and date_regex.match(r[1])]
                if len(valid_like_rows) == 1:
                    matched_rows = [(valid_like_rows[0][0], valid_like_rows[0][1], valid_like_rows[0][2], valid_like_rows[0][3])]

        if not matched_rows:
            stats["not_found"] += 1
            if args.verbose:
                print(f"[NOT FOUND] 数据库中未找到匹配的资源: {filename}")
            continue

        unique_dates = list(set(r[1] for r in matched_rows))
        if len(unique_dates) > 1:
            matched_by_path = []
            for r in matched_rows:
                db_pdf_path = r[3]
                if db_pdf_path:
                    db_filename = os.path.basename(db_pdf_path.replace('\\', '/'))
                    if db_filename.lower() == filename.lower():
                        matched_by_path.append(r)
            if len(matched_by_path) == 1:
                matched_rows = matched_by_path
                unique_dates = [matched_rows[0][1]]
                if args.verbose:
                    print(f"[RESOLVED] 文件 {filename} 对应多个日期，已通过 pdf_path 匹配到唯一记录: ID={matched_rows[0][0]}, Date={unique_dates[0]}")
            else:
                stats["conflict"] += 1
                if args.verbose or True:
                    print(f"[CONFLICT] 文件 {filename} 对应多个不同的数据库日期: {unique_dates}")
                continue

        matched_date = unique_dates[0]
        matched_ids = [r[0] for r in matched_rows]

        if matched_date == "Unknown_Date" or not matched_date:
            stats["still_unknown_date"] += 1
            if args.verbose:
                print(f"[STILL UNKNOWN] 数据库中对应日期仍为 Unknown_Date: {filename}")
            continue
        if not date_regex.match(matched_date):
            stats["invalid_date_format"] += 1
            if args.verbose:
                print(f"[INVALID FORMAT] 数据库中日期格式非 YYYY-MM-DD ({matched_date}): {filename}")
            continue

        stats["success"] += 1
        year = matched_date.split('-')[0]
        new_filename = f"{matched_date}_{clean_title}{suffix}.pdf"
        src_path = os.path.join(unknown_year_dir, filename)
        dst_dir = os.path.join(pdf_base, year)
        dst_path = os.path.join(dst_dir, new_filename)
        move_plans.append((src_path, dst_path, matched_ids, new_filename, matched_date))
        if args.verbose:
            print(f"[PLAN] {filename} -> {year}/{new_filename} (ID: {matched_ids})")

    print("\n" + "=" * 60)
    print(f"[*] 分析完成！准备处理 {len(move_plans)} 个文件...")

    do_run = args.run
    if not do_run:
        print("[*] 当前为预览模式，未执行任何操作。")
        try:
            confirm = input("[*] 检测完毕，是否直接开始移动文件并更新数据库？[y/N]: ").strip().lower()
            if confirm in ('y', 'yes'):
                do_run = True
        except (KeyboardInterrupt, EOFError):
            print("\n[-] 运行已取消")
            conn.close()
            return

    if do_run:
        print("[*] 开始执行物理移动和数据库更新...")
        success_moved = 0
        for src_path, dst_path, matched_ids, new_filename, matched_date in move_plans:
            try:
                dst_dir = os.path.dirname(dst_path)
                if not os.path.exists(dst_dir):
                    os.makedirs(dst_dir, exist_ok=True)
                os.rename(src_path, dst_path)
                abs_dst_path = os.path.abspath(dst_path)
                id_placeholders = ",".join("?" for _ in matched_ids)
                cursor.execute(
                    f"UPDATE resources SET pdf_path = ? WHERE id IN ({id_placeholders})",
                    [abs_dst_path] + matched_ids)
                success_moved += 1
            except Exception as e:
                print(f"[-] 移动文件失败 {os.path.basename(src_path)}: {e}")
                stats["error"] += 1
        conn.commit()
        print(f"[+] 物理修复完成！成功移动并更新了 {success_moved} 个文件。")
    else:
        print("[*] 未执行任何操作。")

    # ===================================================================
    # Phase 2: 全量扫描所有年份文件夹，修复文件名日期与数据库不一致
    # ===================================================================
    print("\n" + "=" * 60)
    print("  Phase 2: 全量检查 - 修复文件名日期与数据库不匹配")
    print("=" * 60)

    print("[*] 正在全量扫描 PDF 目录 (递归所有年份文件夹)...")
    all_pdf_files = []
    for root, dirs, files in os.walk(pdf_base):
        norm_root = os.path.normpath(root)
        if "Unknown_Year" in norm_root:
            continue
        for f in files:
            if f.lower().endswith(".pdf"):
                all_pdf_files.append((f, os.path.join(root, f)))
    total_phase2 = len(all_pdf_files)
    print(f"[+] 扫描到 {total_phase2} 个 PDF 文件 (不含 Unknown_Year)。")

    # 建立数据库索引
    print("[*] 正在构建数据库索引...")
    known_sources = {"seju", "u3c3", "datang", "gcbt", "madou", "jingpin_toupai", "taose", "dashen", "tanhua", "jingpin", "mianfei_guochan"}
    cursor.execute("SELECT id, title, publish_time, pdf_path, url, source FROM resources")
    db_rows = cursor.fetchall()

    db_by_pdf_filename = defaultdict(list)
    db_by_title_and_source = defaultdict(list)
    db_by_title = defaultdict(list)
    for row in db_rows:
        r_id, r_title, r_publish_time, r_pdf_path, r_url, r_source = row
        pub_time = r_publish_time.strip() if r_publish_time else "Unknown_Date"
        if not pub_time:
            pub_time = "Unknown_Date"
        record = {"id": r_id, "title": r_title, "publish_time": pub_time,
                  "pdf_path": r_pdf_path, "url": r_url, "source": r_source or ""}
        if r_pdf_path:
            filename_part = os.path.basename(r_pdf_path.replace('\\', '/')).lower()
            db_by_pdf_filename[filename_part].append(record)
        if r_title:
            t_str = r_title.strip()
            db_by_title[t_str].append(record)
            if r_source:
                db_by_title_and_source[(t_str, r_source.lower())].append(record)

    # 分析不匹配
    print("[*] 正在比对文件名日期与数据库日期...")
    mismatches = []
    for filename, full_path in all_pdf_files:
        fn_date, fn_title_part, fn_source, _ = parse_pdf_filename(filename, known_sources)
        fn_clean_title = clean_title_suffix(fn_title_part)

        matched_records = []
        if filename.lower() in db_by_pdf_filename:
            matched_records = db_by_pdf_filename[filename.lower()]
        if not matched_records:
            if fn_source:
                if (fn_title_part, fn_source.lower()) in db_by_title_and_source:
                    matched_records = db_by_title_and_source[(fn_title_part, fn_source.lower())]
                elif (fn_clean_title, fn_source.lower()) in db_by_title_and_source:
                    matched_records = db_by_title_and_source[(fn_clean_title, fn_source.lower())]
            else:
                if fn_title_part in db_by_title:
                    matched_records = db_by_title[fn_title_part]
                elif fn_clean_title in db_by_title:
                    matched_records = db_by_title[fn_clean_title]
        if not matched_records:
            continue

        unique_dates = list(set(r["publish_time"] for r in matched_records))
        if len(unique_dates) > 1:
            exact_by_path = []
            for r in matched_records:
                if r["pdf_path"]:
                    basename = os.path.basename(r["pdf_path"].replace('\\', '/')).lower()
                    if basename == filename.lower():
                        exact_by_path.append(r)
            if len(exact_by_path) == 1:
                record = exact_by_path[0]
                if fn_date != record["publish_time"]:
                    mismatches.append((filename, full_path, record["publish_time"], [record]))
        else:
            db_date = unique_dates[0]
            cmp_fn_date = fn_date if fn_date else "Unknown_Date"
            cmp_db_date = db_date if db_date else "Unknown_Date"
            if cmp_fn_date != cmp_db_date:
                mismatches.append((filename, full_path, db_date, matched_records))

    print(f"[+] 发现 {len(mismatches)} 个文件名日期与数据库不符的文件。")

    phase2_success = 0
    if mismatches:
        print(f"\n{'='*60}")
        print(f"  Phase 2: 准备处理 {len(mismatches)} 个文件...")
        for filename, src_path, db_date, matched_records in mismatches:
            if db_date and date_regex.match(db_date):
                target_year = db_date.split('-')[0]
            else:
                target_year = "Unknown_Year"
                db_date = "Unknown_Date"
            _, fn_title_part = parse_filename(filename)
            new_filename = f"{db_date}_{fn_title_part}.pdf"
            target_dir = os.path.join(pdf_base, target_year)

            if do_run:
                if not os.path.exists(target_dir):
                    os.makedirs(target_dir, exist_ok=True)
                dst_path = generate_unique_path(target_dir, new_filename)
            else:
                dst_path = os.path.join(target_dir, new_filename)
                if os.path.exists(dst_path):
                    name, ext = os.path.splitext(new_filename)
                    dst_path = os.path.join(target_dir, f"{name}_1{ext}")

            matched_ids = [r["id"] for r in matched_records]
            rel_src = os.path.relpath(src_path, pdf_base)
            rel_dst = os.path.relpath(dst_path, pdf_base)
            print(f"  [PLAN] ID: {matched_ids} | pdf/{rel_src} -> pdf/{rel_dst}")

            if do_run:
                try:
                    os.rename(src_path, dst_path)
                    abs_dst_path = os.path.abspath(dst_path)
                    id_placeholders = ",".join("?" for _ in matched_ids)
                    cursor.execute(
                        f"UPDATE resources SET pdf_path = ? WHERE id IN ({id_placeholders})",
                        [abs_dst_path] + matched_ids
                    )
                    phase2_success += 1
                except Exception as e:
                    print(f"    [-] 修复失败: {e}")

        if do_run:
            conn.commit()
            print(f"\n[+] Phase 2 完成！成功修复 {phase2_success}/{len(mismatches)} 个文件。")
        else:
            print("\n[*] Phase 2 预览完成，未执行实际操作。")
    else:
        print("[+] 所有文件名日期与数据库一致，无需修复。")

    conn.close()
    print("\n" + "=" * 60)
    print("                      统计报告")
    print("=" * 60)
    print(f" 扫描文件总数:                      {total_files}")
    print(f" 匹配成功数 (可修复):                 {stats['success']}")
    print(f" 数据库中仍未修复 (Still Unknown):   {stats['still_unknown_date']}")
    print(f" 数据库中未找到 (Not Found):         {stats['not_found']}")
    print(f" 多重日期冲突 (Conflict):             {stats['conflict']}")
    print(f" 无效日期格式:                      {stats['invalid_date_format']}")
    print(f" 其他处理错误 (Error):               {stats['error']}")
    print("=" * 60)


# ===================================================================
# 功能 3: redownload - 重新下载体积小于 20KB 的 PDF
# ===================================================================
def run_redownload_small_pdfs(args):
    db_path = get_db_path()
    pdf_base = os.path.abspath(PDF_BASE_DIR)

    print("=" * 60)
    print(f"[*] 运行模式: {'【正式修复模式 (重下/覆盖/域名自愈)】' if args.run else '【预览模式 (Dry Run)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    if not os.path.exists(pdf_base):
        print(f"[-] 错误: PDF 根目录不存在: {pdf_base}")
        sys.exit(1)

    print("[*] 正在扫描 PDF 物理文件以寻找体积小于 20KB 的文件...")
    small_files = []
    for root, dirs, files in os.walk(pdf_base):
        for f in files:
            if f.lower().endswith(".pdf"):
                full_path = os.path.join(root, f)
                try:
                    size_bytes = os.path.getsize(full_path)
                    size_kb = size_bytes / 1024.0
                    if size_kb < 20.0:
                        small_files.append((full_path, size_kb))
                except OSError as e:
                    print(f"[-] 无法读取文件大小 {f}: {e}")

    total_small = len(small_files)
    print(f"[+] 扫描完成，共找到 {total_small} 个体积小于 20KB 的 PDF 文件。")
    if total_small == 0:
        print("[*] 未发现需要重新保存的 PDF 文件。")
        return

    print("[*] 正在加载数据库中的 PDF 路径进行比对匹配...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, url, pdf_path, publish_time, source FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    db_rows = cursor.fetchall()

    db_map = {}
    for row in db_rows:
        db_pdf_path = row[3]
        if db_pdf_path:
            base_f = os.path.basename(db_pdf_path.replace('\\', '/')).lower()
            db_map.setdefault(base_f, []).append(row)

    to_download = []
    not_found_in_db = []

    for file_path, size_kb in small_files:
        filename = os.path.basename(file_path).lower()
        rows = db_map.get(filename, [])
        target_rel = to_relative_path(file_path).lower()
        matched_row = None
        for row in rows:
            db_rel = to_relative_path(row[3]).lower()
            if db_rel == target_rel:
                matched_row = row
                break
        if not matched_row and rows:
            for row in rows:
                if os.path.basename(row[3]).lower() == filename:
                    matched_row = row
                    break
        if matched_row:
            r_id, title, url, pdf_path_db, publish_time, source = matched_row
            src = infer_source(source, url, file_path, title)
            to_download.append({
                "file_path": file_path,
                "size_kb": size_kb,
                "id": r_id,
                "title": title,
                "url": url,
                "pdf_path": pdf_path_db,
                "publish_time": publish_time,
                "source": src
            })
        else:
            not_found_in_db.append((file_path, size_kb))

    print(f"[*] 成功匹配数据库记录: {len(to_download)} 个")
    if not_found_in_db:
        print(f"[!] 未能匹配数据库记录: {len(not_found_in_db)} 个 (无法获取 URL 重新下载)")
        if args.verbose:
            for fp, sz in not_found_in_db:
                print(f"  - {fp} ({sz:.2f} KB)")

    if not to_download:
        print("[*] 无匹配的数据库记录可用于重新下载。")
        conn.close()
        return

    do_run = args.run
    if not do_run:
        print("\n" + "=" * 60)
        print("                  需要重新下载的 PDF 预览")
        print("=" * 60)
        preview_limit = 20
        for idx, task in enumerate(to_download[:preview_limit], 1):
            print(f"[{idx}] 路径: {task['file_path']}")
            print(f"    大小: {task['size_kb']:.2f} KB | ID: {task['id']} | 标题: {task['title']} | 来源: {task['source']} | 日期: {task['publish_time']}")
            print(f"    URL:  {task['url']}")
            print("-" * 60)
        if len(to_download) > preview_limit:
            print(f"... 还有 {len(to_download) - preview_limit} 个文件未列出")
        print("\n[*] 预览结束。")
        try:
            confirm = input("[*] 检测完毕，是否直接开始重新下载、域名自愈并覆盖修复？[y/N]: ").strip().lower()
            if confirm in ('y', 'yes'):
                do_run = True
        except (KeyboardInterrupt, EOFError):
            print("\n[-] 运行已取消")
            conn.close()
            return

    if not do_run:
        print("[*] 未重新下载任何文件。")
        conn.close()
        return

    print(f"\n[*] 准备拉起 Playwright 重新下载 {len(to_download)} 个 PDF 文件...")
    success_count = 0
    fail_count = 0
    total_db_domain_updates = 0

    try:
        _, browser, context = browser_factory.create_browser_context(
            headless=True,
            viewport={'width': 1280, 'height': 900}
        )
        for idx, task in enumerate(to_download, 1):
            file_path = task["file_path"]
            size_kb = task["size_kb"]
            r_id = task["id"]
            title = task["title"]
            curr_url = task["url"]
            source = task["source"]
            old_domain = urlparse(curr_url).netloc.split(':')[0] if curr_url else ""

            print(f"\n[*] [{idx}/{len(to_download)}] 正在请求: {curr_url} (来源: {source}, 当前大小: {size_kb:.2f} KB)")
            page = context.new_page()
            try:
                is_valid, new_size_kb, status_msg, page_html = render_pdf_page(page, curr_url, file_path, source)
                if is_valid:
                    success_count += 1
                    print(f"  [+] 成功重新保存并覆盖! 新文件大小: {new_size_kb:.2f} KB")
                else:
                    print(f"  [-] 首次请求/渲染异常: {status_msg}")
                    # 检查网址是否失效，尝试从页面或永久域名拉取最新镜像域名
                    print("  [*] 正在检查网址是否失效，并根据永久域名获取最新镜像域名...")
                    if page_html:
                        extracted = extract_domains_from_text(page_html)
                        if extracted:
                            save_cached_domains(source, extracted)

                    candidate_domains = get_latest_mirror_domains(source, exclude_domain=old_domain)
                    recovered = False
                    if candidate_domains:
                        for cand in candidate_domains:
                            new_url = replace_domain_in_url(curr_url, cand)
                            print(f"  [*] 尝试使用最新镜像域名: {cand} -> {new_url}")
                            is_valid_retry, retry_size_kb, retry_msg, _ = render_pdf_page(page, new_url, file_path, source)
                            if is_valid_retry:
                                print(f"  [+] 使用最新镜像域名 {cand} 重新生成成功! 新文件大小: {retry_size_kb:.2f} KB")
                                if old_domain and cand != old_domain:
                                    cursor.execute(
                                        "UPDATE resources SET url = REPLACE(url, ?, ?) WHERE url LIKE ?",
                                        (old_domain, cand, f"%{old_domain}%")
                                    )
                                    affected_rows = cursor.rowcount
                                    conn.commit()
                                    total_db_domain_updates += affected_rows
                                    print(f"  [+] 数据库已批量更新失效域名: 将所有包含 '{old_domain}' 的记录 ({affected_rows} 条) 替换为 '{cand}'")

                                    # 同步更新内存队列中后续记录的 URL
                                    for rem in to_download[idx:]:
                                        if rem.get("url") and old_domain in rem["url"]:
                                            rem["url"] = replace_domain_in_url(rem["url"], cand)

                                recovered = True
                                success_count += 1
                                break
                            else:
                                print(f"  [-] 尝试域名 {cand} 失败: {retry_msg}")
                    if not recovered:
                        fail_count += 1
                        print(f"  [-] 最终未能成功修复该 PDF: {file_path}")
            except Exception as download_err:
                print(f"  [-] 下载处理异常: {download_err}")
                fail_count += 1
            finally:
                page.close()
            time.sleep(random.uniform(1.5, 3.0))
    except Exception as run_e:
        print(f"[-] Playwright 运行异常: {run_e}")
    finally:
        browser_factory.destroy_thread_resources()

    conn.close()
    print("\n" + "=" * 60)
    print("                      下载统计报告")
    print("=" * 60)
    print(f" 计划重新下载数:             {len(to_download)}")
    print(f" 成功重新下载/覆盖数:         {success_count}")
    print(f" 失败或依然不合格数:         {fail_count}")
    print(f" 数据库失效域名替换影响记录:   {total_db_domain_updates} 条")
    print("=" * 60)



# ===================================================================
# 功能 4: rebuild - 重建缺失的 PDF 文件并路径相对化 (多线程并发)
# ===================================================================
class MissingPDFDownloader:
    """缺失 PDF 并发下载与渲染器"""
    def __init__(self, db_path: str, max_workers: int = 4):
        self.db_path = db_path
        self.max_workers = max_workers
        self.generator = PDFGenerator(r2_uploader=None)
        
        self.lock = threading.Lock()
        self.total = 0
        self.processed = 0
        self.success = 0
        self.not_found = 0
        self.failed = 0
        self.start_time = 0
        
        # 线程安全数据库写入锁
        self.db_lock = threading.Lock()

    def update_progress_file(self, current_item_info: str = ""):
        """更新 JSON 进度文件供前台/监控读取"""
        now = time.time()
        elapsed = max(0.1, now - self.start_time)
        speed = self.processed / elapsed
        remaining = self.total - self.processed
        eta = remaining / speed if speed > 0 else 0

        data = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "RUNNING" if self.processed < self.total else "COMPLETED",
            "total": self.total,
            "processed": self.processed,
            "remaining": remaining,
            "success": self.success,
            "not_found_404": self.not_found,
            "failed": self.failed,
            "percent": round((self.processed / self.total * 100) if self.total > 0 else 0, 2),
            "speed_per_sec": round(speed, 2),
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": round(eta, 1),
            "last_item": current_item_info
        }

        try:
            os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
            temp_file = PROGRESS_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if os.path.exists(PROGRESS_FILE):
                os.remove(PROGRESS_FILE)
            os.rename(temp_file, PROGRESS_FILE)
        except Exception:
            pass

    def _db_update(self, sql: str, params: tuple):
        """线程安全的 SQLite 更新操作"""
        with self.db_lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params)
                conn.commit()
            except Exception as e:
                print(f"[-] 数据库更新异常: {e}", flush=True)
            finally:
                conn.close()

    def process_single_record(self, record: dict):
        """单个记录的下载与渲染工作单元"""
        r_id = record["id"]
        title = record["title"]
        url = record["url"]
        publish_time = record["publish_time"]
        source = record["source"]

        config = CONFIG_MAP.get(source, PDFRenderConfig())
        
        item_status = ""
        generated_path = None
        t0 = time.time()

        try:
            _, browser, context = browser_factory.create_browser_context(headless=True, source=source)
            
            # 1. 预检查 404
            is_404 = False
            temp_page = context.new_page()
            try:
                resp = temp_page.goto(url, timeout=25000, wait_until="domcontentloaded")
                if resp and resp.status == 404:
                    is_404 = True
            except Exception:
                pass
            finally:
                try:
                    temp_page.close()
                except Exception:
                    pass

            if is_404:
                self._db_update("UPDATE resources SET pdf_path = '' WHERE id = ?", (r_id,))
                with self.lock:
                    self.processed += 1
                    self.not_found += 1
                item_status = f"[-] 页面 404 (已清理 pdf_path) ID={r_id} 来源={source}"
            else:
                # 2. 正常渲染生成 PDF
                rel_pdf_path = self.generator.generate_pdf(
                    page_or_context=context,
                    target_url_or_page=url,
                    publish_date=publish_time,
                    title=title,
                    source_name=source,
                    config=config
                )

                if rel_pdf_path:
                    self._db_update("UPDATE resources SET pdf_path = ? WHERE id = ?", (rel_pdf_path, r_id))
                    with self.lock:
                        self.processed += 1
                        self.success += 1
                    generated_path = rel_pdf_path
                    item_status = f"[+] 渲染成功 ID={r_id} 来源={source} -> {rel_pdf_path}"
                else:
                    with self.lock:
                        self.processed += 1
                        self.failed += 1
                    item_status = f"[-] 渲染失败 (保留原样) ID={r_id} 来源={source}"

        except Exception as e:
            with self.lock:
                self.processed += 1
                self.failed += 1
            item_status = f"[-] 异常 ID={r_id} 来源={source} | 错误: {e}"

        elapsed_item = time.time() - t0
        
        # 打印实时进度
        with self.lock:
            pct = (self.processed / self.total * 100) if self.total > 0 else 0
            now = time.time()
            total_elapsed = max(0.1, now - self.start_time)
            speed = self.processed / total_elapsed
            rem_sec = (self.total - self.processed) / speed if speed > 0 else 0
            rem_min = rem_sec / 60.0
            
            print(f"[{self.processed:>3}/{self.total}] ({pct:>5.1f}%) "
                  f"成功:{self.success:<3} 404:{self.not_found:<2} 失败:{self.failed:<2} "
                  f"(单篇 {elapsed_item:.1f}s | 预估剩余 {rem_min:.1f}分) | {item_status}", flush=True)

        self.update_progress_file(item_status)
        return generated_path

    def run(self, missing_records: list):
        self.total = len(missing_records)
        if self.total == 0:
            print("[+] 经检查，当前没有缺失物理文件的断链记录，全部正常！", flush=True)
            self.update_progress_file("无需修复")
            return

        print(f"[+] 发现 {self.total} 条物理文件缺失记录，准备并发下载 (线程数={self.max_workers})...", flush=True)
        self.start_time = time.time()
        self.update_progress_file("准备就绪")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(self.process_single_record, rec) for rec in missing_records]
            for _ in as_completed(futures):
                pass

        total_time = time.time() - self.start_time
        print("\n" + "=" * 60, flush=True)
        print("                      并发下载完成报告", flush=True)
        print("=" * 60, flush=True)
        print(f" 计划下载总数:               {self.total}", flush=True)
        print(f" 成功渲染生成:               {self.success}", flush=True)
        print(f" 页面不存在 (404已清理) 数:   {self.not_found}", flush=True)
        print(f" 下载渲染失败数:             {self.failed}", flush=True)
        print(f" 总耗时:                     {total_time:.1f} 秒 ({total_time / 60.0:.2f} 分钟)", flush=True)
        print("=" * 60, flush=True)


def run_rebuild(args):
    db_path = getattr(args, "db", None) or get_db_path()
    base_dir = os.path.abspath(PDF_BASE_DIR)
    project_dir = os.path.dirname(base_dir)
    workers = getattr(args, "workers", 4) or 4

    print("=" * 60)
    print(f"[*] 运行模式: {'【正式修复模式】' if args.run else '【预览模式 (Dry Run)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] 项目根目录: {project_dir}")
    print(f"[*] PDF 根目录: {base_dir}")
    print(f"[*] 并发工作线程数: {workers}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    if not os.path.exists(base_dir):
        print(f"[*] 创建 PDF 根目录: {base_dir}")
        if args.run:
            os.makedirs(base_dir, exist_ok=True)

    print("[*] 正在扫描 PDF 物理文件...")
    phys_files = set()
    if os.path.exists(base_dir):
        for root, dirs, files in os.walk(base_dir):
            for f in files:
                if f.lower().endswith(".pdf"):
                    phys_files.add(os.path.abspath(os.path.join(root, f)).lower())
    print(f"[+] 物理目录中现存的 PDF 文件数: {len(phys_files)}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT id, title, pdf_path, url, publish_time, source FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    rows = cursor.fetchall()
    print(f"[*] 数据库中含有 pdf_path 的总记录数: {len(rows)}")

    needs_update_to_relative = []
    missing_records = []

    for r_id, title, pdf_path, url, publish_time, source in rows:
        rel_path = to_relative_path(pdf_path)
        norm_abs_path = os.path.abspath(os.path.join(project_dir, rel_path))
        if norm_abs_path.lower() in phys_files:
            if pdf_path != rel_path:
                needs_update_to_relative.append((r_id, rel_path))
        else:
            missing_records.append({
                "id": r_id,
                "title": title,
                "pdf_path": pdf_path,
                "url": url,
                "publish_time": publish_time or "Unknown_Date",
                "source": source or "unknown"
            })

    print(f"[*] 需要转换为相对路径（且物理文件已存在）的记录数: {len(needs_update_to_relative)}")
    print(f"[*] 真正物理缺失（本地无文件）的记录数: {len(missing_records)}")
    print("=" * 60)

    # --- 路径相对化 ---
    if needs_update_to_relative:
        if args.run:
            print("[*] 正在执行相对路径纠偏更新数据库...")
            success_update = 0
            for r_id, rel_path in needs_update_to_relative:
                try:
                    cursor.execute("UPDATE resources SET pdf_path = ? WHERE id = ?", (rel_path, r_id))
                    success_update += 1
                except Exception as e:
                    print(f"[-] 纠偏 ID {r_id} 失败: {e}")
            conn.commit()
            print(f"[+] 路径相对化成功更新了 {success_update} 条记录。")
        else:
            print(f"[PLAN] 将更新 {len(needs_update_to_relative)} 条记录为相对路径（示例前 5 条）：")
            for r_id, rel_path in needs_update_to_relative[:5]:
                print(f"  - ID: {r_id} -> {rel_path}")
            print("[*] 提示: 预览模式下未修改数据库。")

    conn.close()

    # --- 重新下载缺失文件 ---
    if missing_records:
        if args.skip_download:
            print("[*] 参数指定跳过重新下载缺失文件。")
        elif not args.run:
            print(f"\n[PLAN] 发现 {len(missing_records)} 个物理缺失的文件（示例前 5 个）：")
            for rec in missing_records[:5]:
                print(f"  - ID: {rec['id']} | 标题: {rec['title']} | 日期: {rec['publish_time']} | 来源: {rec['source']}")
                print(f"    URL: {rec['url']}")
            print("[*] 提示: 预览模式下未下载任何文件。")
            print("[*] 若要正式开始并发下载，请运行: python fixes/pdf_maintenance.py rebuild --run")
        else:
            downloader = MissingPDFDownloader(db_path=db_path, max_workers=workers)
            downloader.run(missing_records)

    print("[+] rebuild 运行结束。")



# ===================================================================
# 功能 5: orphan - 检查多余PDF或恢复多余PDF
# ===================================================================
def _move_orphans_to_root(db_path, pdf_base, args):
    """扫描所有年份子文件夹，将数据库中无对应记录的PDF移到/pdf根目录"""
    print("=" * 60)
    print("[*] 检查多余PDF - 将年份文件夹中数据库中无记录的PDF移到/pdf根目录")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        return
    if not os.path.exists(pdf_base):
        print(f"[-] 错误: PDF 根目录不存在: {pdf_base}")
        return

    # 1. 扫描所有PDF物理文件（排除根目录本身和Unknown_Year）
    print("[*] 正在扫描PDF目录...")
    all_pdf_files = {}  # rel_path -> (full_path, filename, year_folder)
    year_folders = []
    for item in os.listdir(pdf_base):
        item_path = os.path.join(pdf_base, item)
        if os.path.isdir(item_path) and item != "Unknown_Year":
            year_folders.append(item)
            for root, dirs, files in os.walk(item_path):
                for f in files:
                    if f.lower().endswith(".pdf"):
                        full_path = os.path.join(root, f)
                        rel = os.path.relpath(full_path, pdf_base)
                        all_pdf_files[rel] = (full_path, f, item)

    print(f"[+] 扫描到 {len(year_folders)} 个年份文件夹, 共 {len(all_pdf_files)} 个PDF文件。")

    # 2. 加载数据库记录
    print("[*] 正在加载数据库中的资源记录...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, publish_time, pdf_path, url FROM resources")
    db_rows = cursor.fetchall()
    print(f"[+] 成功加载 {len(db_rows)} 条数据库记录。")

    # 建立索引: 按pdf_path（相对路径和绝对路径）和文件名
    db_by_relpath = {}
    db_by_abspath = {}
    db_by_filename = defaultdict(list)
    db_by_title = defaultdict(list)
    for row in db_rows:
        r_id, r_title, r_publish_time, r_pdf_path, r_url = row
        if r_pdf_path:
            rel = r_pdf_path.replace('\\', '/')
            db_by_relpath[rel.lower()] = row
            abs_p = os.path.abspath(os.path.join(pdf_base, '..', rel)).lower()
            db_by_abspath[abs_p] = row
            fn = os.path.basename(rel).lower()
            db_by_filename[fn].append(row)
        if r_title:
            db_by_title[r_title.strip()].append(row)

    # 3. 比对，找出孤儿文件
    print("[*] 正在比对文件与数据库...")
    matched = []
    associated = []      # (filename, full_path, year_folder, record_id, title, pub_time)
    warn_conflict = []   # (filename, full_path, year_folder, record)
    truly_orphan = []    # (filename, full_path, year_folder)
    for rel_path, (full_path, filename, year_folder) in all_pdf_files.items():
        norm_rel = rel_path.replace('\\', '/').lower()
        norm_abs = full_path.lower()
        found = False

        # 按相对路径匹配
        if norm_rel in db_by_relpath:
            found = True
        # 按绝对路径匹配
        if not found and norm_abs in db_by_abspath:
            found = True
        # 按文件名匹配
        if not found:
            fn_lower = filename.lower()
            if fn_lower in db_by_filename:
                # 检查是否有多个匹配，且其中某个的路径与当前文件一致
                for r in db_by_filename[fn_lower]:
                    db_p = r[3].replace('\\', '/').lower() if r[3] else ""
                    if db_p and os.path.basename(db_p) == fn_lower:
                        found = True
                        break
                if not found:
                    found = True  # 只要有文件名匹配就算

        if found:
            matched.append((filename, full_path, year_folder))
            continue

        # 未通过路径/文件名匹配，尝试通过文件名中的资源名在数据库中搜索标题
        fn_date, fn_title = parse_filename(filename)
        clean_title = clean_title_suffix(fn_title) if fn_title else ""

        matched_records = []
        if clean_title and clean_title in db_by_title:
            matched_records = db_by_title[clean_title]
        if not matched_records and fn_title and fn_title in db_by_title:
            matched_records = db_by_title[fn_title]

        if matched_records:
            # 取第一个匹配的记录
            record = matched_records[0]
            r_id, r_title, r_publish_time, r_pdf_path, r_url = record

            if not r_pdf_path:  # pdf_path 为空，可以关联
                associated.append((filename, full_path, year_folder, r_id, r_title, r_publish_time))
            else:  # 已有 pdf_path，给出警告
                warn_conflict.append((filename, full_path, year_folder, record))
                print(f"[!] 警告: PDF '{filename}' 对应数据库记录(ID={r_id}, 标题='{r_title}') "
                      f"已存在 pdf_path='{r_pdf_path}'，可能为重复PDF")
        else:
            truly_orphan.append((filename, full_path, year_folder))

    # 4. 执行自动关联（将关联到的 pdf_path 写入数据库）
    if associated:
        print(f"\n[*] 发现 {len(associated)} 个PDF文件可通过标题匹配到数据库记录（pdf_path 为空），正在自动关联...")
        for fn, full_path, yf, r_id, r_title, r_publish_time in associated:
            try:
                rel_pdf_path = to_relative_path(full_path)
                cursor.execute("UPDATE resources SET pdf_path = ? WHERE id = ?", (rel_pdf_path, r_id))
                print(f"  [+] 已关联: {yf}/{fn} -> 记录ID={r_id}, 标题='{r_title}', pdf_path='{rel_pdf_path}'")
            except Exception as e:
                print(f"  [-] 关联失败 {fn}: {e}")
        conn.commit()
        print("[+] 自动关联完成。\n")

    # 5. 报告
    print("\n" + "=" * 60)
    print(f"  扫描文件总数: {len(all_pdf_files)}")
    print(f"  数据库有记录(保留): {len(matched)}")
    print(f"  通过标题自动关联: {len(associated)}")
    print(f"  标题匹配但已有pdf_path(需人工确认): {len(warn_conflict)}")
    print(f"  完全无匹配(多余): {len(truly_orphan)}")
    print("=" * 60)

    all_orphans = []
    for item in warn_conflict:
        all_orphans.append({"type": "warn", "filename": item[0], "path": item[1], "folder": item[2], "record": item[3]})
    for item in truly_orphan:
        all_orphans.append({"type": "orphan", "filename": item[0], "path": item[1], "folder": item[2]})

    if not all_orphans:
        print("[*] 未发现多余PDF文件。")
        conn.close()
        return

    # 列出标题匹配但已有 pdf_path 的警告文件
    if warn_conflict:
        print("\n[!] 以下PDF文件通过标题匹配到数据库记录，但记录已有pdf_path，需人工确认:")
        for fn, fp, yf, record in warn_conflict:
            r_id, r_title, r_publish_time, r_pdf_path, r_url = record
            print(f"  [ID={r_id}] {yf}/{fn}")
            print(f"       数据库已有 pdf_path: {r_pdf_path}")
        print()

    # 列出完全无匹配的多余文件
    if truly_orphan:
        print("[*] 以下为数据库中完全无对应记录的PDF文件（多余PDF）:")
        for i, (fn, fp, yf) in enumerate(truly_orphan, 1):
            print(f"  [{i}] {yf}/{fn}")
        print()

    # 6. 询问是否移动多余PDF
    try:
        confirm = input(f"\n[*] 是否将这 {len(all_orphans)} 个多余/冲突PDF文件移动到 {pdf_base} 根目录？[y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n[-] 操作已取消")
        conn.close()
        return

    if confirm not in ('y', 'yes'):
        print("[*] 未执行移动操作。")
        conn.close()
        return

    # 7. 执行移动
    print("[*] 正在移动多余PDF文件...")
    moved_count = 0
    for item in all_orphans:
        fn = item["filename"]
        full_path = item["path"]
        yf = item["folder"]
        try:
            dst_path = os.path.join(pdf_base, fn)
            # 避免重名
            if os.path.exists(dst_path):
                name, ext = os.path.splitext(fn)
                counter = 1
                while os.path.exists(dst_path):
                    dst_path = os.path.join(pdf_base, f"{name}_{counter}{ext}")
                    counter += 1
            os.rename(full_path, dst_path)
            print(f"  [+] 已移动: {yf}/{fn} -> {os.path.basename(dst_path)}")
            moved_count += 1
        except Exception as e:
            print(f"  [-] 移动失败 {fn}: {e}")

    print(f"\n[+] 成功移动 {moved_count}/{len(all_orphans)} 个文件到 {pdf_base} 根目录。")
    conn.close()


def _restore_orphans_from_root(db_path, pdf_base, args):
    """将/pdf根目录下的PDF文件移回对应年份文件夹"""
    print("=" * 60)
    print("[*] 恢复多余PDF - 将/pdf根目录下的PDF移回对应年份文件夹")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] PDF 根目录: {pdf_base}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        return
    if not os.path.exists(pdf_base):
        print(f"[-] 错误: PDF 根目录不存在: {pdf_base}")
        return

    # 1. 扫描/pdf根目录下的PDF文件
    print("[*] 正在扫描/pdf根目录下的PDF文件...")
    root_pdfs = []
    for f in os.listdir(pdf_base):
        if f.lower().endswith(".pdf"):
            full_path = os.path.join(pdf_base, f)
            if os.path.isfile(full_path):
                root_pdfs.append((f, full_path))

    if not root_pdfs:
        print("[*] /pdf根目录下没有PDF文件。")
        return

    print(f"[+] 扫描到 {len(root_pdfs)} 个PDF文件。")

    # 2. 加载数据库记录
    print("[*] 正在加载数据库中的资源记录...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, publish_time, pdf_path FROM resources")
    db_rows = cursor.fetchall()
    print(f"[+] 成功加载 {len(db_rows)} 条数据库记录。")

    # 建立索引
    db_by_title = defaultdict(list)
    db_by_filename = defaultdict(list)
    for row in db_rows:
        r_id, r_title, r_publish_time, r_pdf_path = row
        if r_pdf_path:
            fn = os.path.basename(r_pdf_path.replace('\\', '/')).lower()
            db_by_filename[fn].append(row)
        if r_title:
            db_by_title[r_title.strip()].append(row)

    # 3. 对每个根目录PDF，判断应放入哪个年份文件夹
    print("[*] 正在分析PDF文件归属...")
    restore_plans = []  # (src_path, dst_path, filename, year)
    no_match = []
    skipped = []

    for filename, full_path in root_pdfs:
        # 尝试从文件名解析日期
        fn_date, fn_title = parse_filename(filename)
        clean_title = clean_title_suffix(fn_title) if fn_title else None

        target_year = None
        target_date = None

        if fn_date and date_regex.match(fn_date):
            # 文件名已有日期，直接使用
            target_date = fn_date
            target_year = fn_date.split('-')[0]
        else:
            # 尝试从数据库查找
            matched_records = []

            # 按文件名匹配
            if filename.lower() in db_by_filename:
                matched_records = db_by_filename[filename.lower()]

            # 按标题匹配
            if not matched_records and fn_title:
                if fn_title in db_by_title:
                    matched_records = db_by_title[fn_title]
                elif clean_title and clean_title in db_by_title:
                    matched_records = db_by_title[clean_title]

            if matched_records:
                # 取第一个有有效日期的记录
                for r in matched_records:
                    _, _, pub_time, _ = r
                    if pub_time and date_regex.match(pub_time):
                        target_date = pub_time
                        target_year = pub_time.split('-')[0]
                        break
                if not target_year and matched_records:
                    _, _, pub_time, _ = matched_records[0]
                    if pub_time and date_regex.match(pub_time):
                        target_date = pub_time
                        target_year = pub_time.split('-')[0]

        if target_year and target_year.isdigit():
            year_dir = os.path.join(pdf_base, target_year)
            new_filename = filename
            if fn_date is None and target_date:
                # 文件名没有日期前缀，加上
                new_filename = f"{target_date}_{fn_title or filename[:-4]}.pdf"
            dst_path = os.path.join(year_dir, new_filename)
            # 避免重名
            if os.path.exists(dst_path):
                name, ext = os.path.splitext(new_filename)
                counter = 1
                while os.path.exists(dst_path):
                    dst_path = os.path.join(year_dir, f"{name}_{counter}{ext}")
                    counter += 1
            restore_plans.append((full_path, dst_path, filename, target_year))
        elif fn_date == "Unknown_Date":
            skipped.append((filename, "文件中日期为 Unknown_Date，无法确定年份"))
            no_match.append((filename, "文件中日期为 Unknown_Date"))
        else:
            no_match.append((filename, "无法从文件名或数据库确定日期"))

    # 4. 报告
    print("\n" + "=" * 60)
    print(f"  /pdf根目录文件总数: {len(root_pdfs)}")
    print(f"  可恢复(有对应年份): {len(restore_plans)}")
    print(f"  无法确定归属: {len(no_match)}")
    print("=" * 60)

    if restore_plans:
        print("\n[*] 以下文件可恢复到对应年份文件夹:")
        for src, dst, fn, year in restore_plans:
            print(f"  {fn} -> {year}/{os.path.basename(dst)}")

    if no_match:
        print("\n[!] 以下文件无法确定归属:")
        for fn, reason in no_match:
            print(f"  {fn} ({reason})")

    if not restore_plans:
        print("[*] 没有可恢复的文件。")
        conn.close()
        return

    # 5. 询问是否移动
    try:
        confirm = input(f"\n[*] 是否将这 {len(restore_plans)} 个文件移回对应年份文件夹？[y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n[-] 操作已取消")
        conn.close()
        return

    if confirm not in ('y', 'yes'):
        print("[*] 未执行移动操作。")
        conn.close()
        return

    # 6. 执行移动
    print("[*] 正在移动文件...")
    moved_count = 0
    for src_path, dst_path, filename, year in restore_plans:
        try:
            dst_dir = os.path.dirname(dst_path)
            if not os.path.exists(dst_dir):
                os.makedirs(dst_dir, exist_ok=True)
            os.rename(src_path, dst_path)
            print(f"  [+] 已移动: {filename} -> {year}/{os.path.basename(dst_path)}")
            moved_count += 1
        except Exception as e:
            print(f"  [-] 移动失败 {filename}: {e}")

    print(f"\n[+] 成功移动 {moved_count}/{len(restore_plans)} 个文件。")
    conn.close()


def run_orphan(args):
    """多余PDF管理 - 检查多余PDF或恢复多余PDF"""
    db_path = get_db_path()
    pdf_base = os.path.abspath(PDF_BASE_DIR)

    print("=" * 60)
    print("                 多余PDF管理工具")
    print("=" * 60)
    print("  1. 检查多余PDF - 将年份文件夹中数据库中无记录的PDF移到/pdf根目录")
    print("  2. 恢复多余PDF - 将/pdf根目录下的PDF移回对应年份文件夹")
    print("=" * 60)

    try:
        choice = input("请输入序号 [1/2] (直接回车默认 1): ").strip()
        if not choice:
            choice = "1"
    except (KeyboardInterrupt, EOFError):
        print("\n[-] 运行已取消")
        return

    if choice == "1":
        _move_orphans_to_root(db_path, pdf_base, args)
    elif choice == "2":
        _restore_orphans_from_root(db_path, pdf_base, args)
    else:
        print("[-] 无效的序号。")


# ===================================================================
# 功能 6: associate - 扫描未关联/断链 PDF 智能回填数据库
# ===================================================================
def normalize_rel_path(path_str: str, project_root: str) -> str:
    """标准化相对路径"""
    if not path_str:
        return ""
    p = str(path_str).strip().replace("\\", "/")
    proj_norm = project_root.replace("\\", "/")
    if p.lower().startswith(proj_norm.lower() + "/"):
        p = p[len(proj_norm) + 1:]
    return p.lstrip("/")


def parse_pdf_filename(fname: str, known_sources: set):
    """解析 PDF 文件名中的 日期、标题、来源和计数后缀"""
    name = fname[:-4] if fname.lower().endswith('.pdf') else fname
    counter = None
    m_count = re.search(r'_(\d+)$', name)
    if m_count:
        counter = int(m_count.group(1))
        name = name[:m_count.start()]

    date_prefix = None
    m_date = re.match(r'^(\d{4}-\d{2}-\d{2}|Unknown_Date)_(.*)$', name)
    if m_date:
        date_prefix = m_date.group(1)
        name = m_date.group(2)

    source = None
    for s in sorted(known_sources, key=lambda x: len(x), reverse=True):
        if name.lower().endswith(f"_{s.lower()}"):
            source = s
            name = name[:-len(f"_{s}")]
            break

    title = name
    return date_prefix, title, source, counter


def run_associate(args):
    """扫描未关联/断链的物理 PDF 文件，智能匹配回填数据库 resources 表"""
    project_root = PROJECT_ROOT
    target_db = getattr(args, "db", None) or get_db_path()
    run_mode = getattr(args, "run", False)

    print("=" * 70)
    print("[*] 启动 PDF 孤儿文件与断链记录智能关联程序")
    print(f"[*] 运行模式: {'【正式执行模式 (写入数据库)】' if run_mode else '【预览模式 (Dry Run)】'}")
    print(f"[*] 项目根目录: {project_root}")
    print(f"[*] 目标数据库: {target_db}")
    print("=" * 70)

    if not os.path.exists(target_db):
        print(f"[-] 错误: 数据库文件不存在: {target_db}")
        return

    # 1. 扫描磁盘上所有 PDF 物理文件
    print("[*] 正在扫描磁盘 PDF 物理文件...")
    disk_pdfs = {}  # norm_rel_path_lower -> (rel_p, abs_p, size)
    for root, dirs, files in os.walk(project_root):
        if any(ig in root for ig in [".venv", ".git", ".vscode", "__pycache__", ".agents"]):
            continue
        for f in files:
            if f.lower().endswith(".pdf"):
                abs_p = os.path.join(root, f)
                rel_p = os.path.relpath(abs_p, project_root).replace("\\", "/")
                sz = os.path.getsize(abs_p)
                disk_pdfs[rel_p.lower()] = (rel_p, abs_p, sz)

    print(f"[+] 磁盘共发现 {len(disk_pdfs)} 个 PDF 物理文件。")

    # 2. 读取数据库记录
    print("[*] 正在加载数据库资源记录...")
    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, publish_time, url, pdf_path, source FROM resources")
    rows = cursor.fetchall()
    print(f"[+] 成功加载 {len(rows)} 条数据库记录。")

    known_sources = set(r["source"].lower() for r in rows if r["source"])

    # 建立数据库引用与标题索引
    db_path_map = defaultdict(list)
    db_by_sanitized_title_and_source = defaultdict(list)
    db_by_sanitized_title = defaultdict(list)

    for r in rows:
        p = normalize_rel_path(r["pdf_path"], project_root)
        if p:
            db_path_map[p.lower()].append(r)

        t = r["title"] or ""
        t_clean = t.strip().lower()
        t_sanitized = sanitize_filename(t).strip().lower()
        s = (r["source"] or "").strip().lower()

        db_by_sanitized_title[t_clean].append(r)
        if t_sanitized != t_clean:
            db_by_sanitized_title[t_sanitized].append(r)

        if s:
            db_by_sanitized_title_and_source[(t_clean, s)].append(r)
            if t_sanitized != t_clean:
                db_by_sanitized_title_and_source[(t_sanitized, s)].append(r)

    # 3. 找出未被 DB pdf_path 引用的磁盘文件
    unreferenced_disk = [v for k, v in disk_pdfs.items() if k not in db_path_map]
    print(f"[*] 磁盘上未被数据库 pdf_path 引用的文件数: {len(unreferenced_disk)}")

    # 4. 执行匹配与生成计划
    fill_empty_plan = []    # (record_id, title, old_path, new_rel_path, source)
    repair_broken_plan = [] # (record_id, title, old_path, new_rel_path, source)
    duplicate_kept_count = 0
    pure_orphan_count = 0

    assigned_record_ids = set()

    for rel_p, abs_p, sz in unreferenced_disk:
        fname = os.path.basename(rel_p)
        dt, t, s, cnt = parse_pdf_filename(fname, known_sources)
        t_key = t.strip().lower()

        matched_recs = []
        if s:
            if (t_key, s.lower()) in db_by_sanitized_title_and_source:
                matched_recs = db_by_sanitized_title_and_source[(t_key, s.lower())]
        elif t_key in db_by_sanitized_title:
            matched_recs = db_by_sanitized_title[t_key]

        if not matched_recs:
            pure_orphan_count += 1
            continue

        # 优先寻找未分配过的空路径记录
        empty_recs = [
            r for r in matched_recs
            if not normalize_rel_path(r["pdf_path"], project_root) and r["id"] not in assigned_record_ids
        ]
        # 其次寻找未分配过的断链记录 (路径文件已不存在)
        broken_recs = [
            r for r in matched_recs
            if normalize_rel_path(r["pdf_path"], project_root)
            and normalize_rel_path(r["pdf_path"], project_root).lower() not in disk_pdfs
            and r["id"] not in assigned_record_ids
        ]
        # 检查是否已有其他有效记录
        valid_recs = [
            r for r in matched_recs
            if normalize_rel_path(r["pdf_path"], project_root)
            and normalize_rel_path(r["pdf_path"], project_root).lower() in disk_pdfs
        ]

        if empty_recs:
            target_r = empty_recs[0]
            assigned_record_ids.add(target_r["id"])
            fill_empty_plan.append((target_r["id"], target_r["title"], "", rel_p, target_r["source"]))
        elif broken_recs:
            target_r = broken_recs[0]
            assigned_record_ids.add(target_r["id"])
            repair_broken_plan.append((target_r["id"], target_r["title"], target_r["pdf_path"], rel_p, target_r["source"]))
        elif valid_recs:
            duplicate_kept_count += 1
        else:
            pure_orphan_count += 1

    total_updates = len(fill_empty_plan) + len(repair_broken_plan)

    print("\n" + "=" * 70)
    print("                      匹配统计概览")
    print("=" * 70)
    print(f" 1. 待更新记录 (空 pdf_path 填充):           {len(fill_empty_plan):>6} 条")
    print(f" 2. 待更新记录 (失效/断链 pdf_path 修复):    {len(repair_broken_plan):>6} 条")
    print(f" 3. 待更新记录总数:                         {total_updates:>6} 条")
    print(f" 4. 已有有效关联的重复/多版本副本 (保持保护): {duplicate_kept_count:>6} 个")
    print(f" 5. 纯孤立文件 (无对应标题记录):             {pure_orphan_count:>6} 个")
    print("=" * 70)

    if total_updates == 0:
        print("[+] 无需更新任何数据库记录。")
        conn.close()
        return

    # 打印前 10 个示例
    print("\n[示例] 拟更新记录预览 (前 10 条):")
    sample_list = (fill_empty_plan + repair_broken_plan)[:10]
    for idx, (rec_id, title, old_p, new_p, src) in enumerate(sample_list, 1):
        action = "【填充空路径】" if not old_p else "【修复断链】"
        print(f"  {idx:>2}. ID: {rec_id} | 来源: {src} | 动作: {action}")
        print(f"      标题: {title[:40]}")
        print(f"      旧路径: {old_p or '(空)'}")
        print(f"      新路径: {new_p}")

    if not run_mode:
        print("\n" + "-" * 70)
        print("[!] 当前为【预览模式 (Dry Run)】，数据库未作任何变更。")
        print("[!] 若确认执行更正，请添加 --run 参数运行:")
        print("    python fixes/pdf_maintenance.py associate --run")
        print("-" * 70)
        conn.close()
        return

    # 5. 正式执行模式
    print("\n[*] 准备执行更新，正在备份数据库...")
    backup_file = backup_db(target_db, "fix_associations")
    print(f"[+] 数据库备份完毕: {backup_file}")

    print(f"[*] 正在批量更新 {total_updates} 条记录...")
    updated_count = 0
    try:
        cursor.execute("BEGIN TRANSACTION")
        
        # 执行空路径填充
        for rec_id, title, old_p, new_p, src in fill_empty_plan:
            cursor.execute("UPDATE resources SET pdf_path = ? WHERE id = ?", (new_p, rec_id))
            updated_count += 1

        # 执行断链修复
        for rec_id, title, old_p, new_p, src in repair_broken_plan:
            cursor.execute("UPDATE resources SET pdf_path = ? WHERE id = ?", (new_p, rec_id))
            updated_count += 1

        conn.commit()
        print(f"[+] 数据库更新成功！共完成 {updated_count} 条记录的 pdf_path 更正。")
    except Exception as e:
        conn.rollback()
        print(f"[-] 更新过程中发生异常，事务已回滚: {e}")
    finally:
        conn.close()


# ===================================================================
# 功能 7: clean-missing - 清理缺失记录
# ===================================================================
def run_clean_missing_records(args):
    """清理数据库中对应物理 PDF 文件已不存在的残留记录（强制级联清理关联 PDF）"""
    db_path = get_db_path(getattr(args, "db", None))
    scope = getattr(args, "scope", "unknown")
    is_run = getattr(args, "run", False) or getattr(args, "yes", False)

    print_banner("清理物理缺失 PDF 对应的数据库脏记录")
    print(f"[*] 运行模式: {'【正式删除模式 (RUN)】' if is_run else '【预览模式 (DRY RUN)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] 扫描范围: {'Unknown_Year 目录' if scope == 'unknown' else '全量 PDF 记录'}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print_error(f"数据库文件不存在: {db_path}")
        return

    conn = get_connection(db_path)
    cursor = conn.cursor()

    if scope == "unknown":
        cursor.execute("SELECT id, title, pdf_path FROM resources WHERE pdf_path LIKE '%Unknown_Year%'")
    else:
        cursor.execute("SELECT id, title, pdf_path FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")

    rows = cursor.fetchall()
    print_step(f"数据库中待检查记录数: {len(rows)}")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    to_delete = []

    for r_id, title, pdf_path in rows:
        if not pdf_path:
            continue
        abs_p = pdf_path if os.path.isabs(pdf_path) else os.path.join(project_root, pdf_path)
        if not os.path.exists(abs_p):
            to_delete.append((r_id, title, pdf_path))

    print_step(f"发现物理文件已不存在但数据库中残留的记录数: {len(to_delete)}")
    print("=" * 60)

    if not to_delete:
        print_success("没有需要清理的记录。")
        conn.close()
        return

    print_section("残留记录样例预览 (Top 20)")
    for idx, (r_id, title, pdf_path) in enumerate(to_delete[:20], 1):
        print(f"[{idx:2d}] ID: {r_id:<6d} | 路径: {pdf_path:<40s} | 标题: {(title or '')[:40]}")
    if len(to_delete) > 20:
        print(f"  ... 以及其他 {len(to_delete) - 20} 条记录")
    print("─" * 60)

    delete_ids = [item[0] for item in to_delete]

    # 导出审计表 (默认 .db)
    if getattr(args, "export_db", True):
        exp_records = [{"id": r[0], "title": r[1], "missing_pdf_path": r[2]} for r in to_delete]
        export_records_to_db(exp_records, f"missing_pdf_records_{get_timestamp()}.db", table_name="missing_records")

    if getattr(args, "export_csv", False):
        exp_records = [{"id": r[0], "title": r[1], "missing_pdf_path": r[2]} for r in to_delete]
        export_to_csv(exp_records, f"missing_pdf_records_{get_timestamp()}.csv")

    delete_records_cascade_pdf(conn, delete_ids, is_run=is_run)
    conn.close()


# ===================================================================
# 功能 8: dedup - PDF 物理文件多维查重、去重与数据库引用纠偏
# ===================================================================
def run_dedup(args):
    from fixes.pdf_dedup import run_pdf_dedup
    is_run = getattr(args, "run", False) or getattr(args, "yes", False)
    run_pdf_dedup(
        mode=getattr(args, "mode", "all"),
        keep=getattr(args, "keep", "primary"),
        run=is_run,
        export_db=getattr(args, "export_db", True),
        export_csv=getattr(args, "export_csv", False),
        trash=getattr(args, "trash", False),
        db_path=getattr(args, "db", None),
        max_workers=getattr(args, "workers", 16),
    )


# ===================================================================
# 主入口 - 支持子命令: check-dates, fix-paths, redownload, rebuild, orphan, associate, clean-missing, dedup
# ===================================================================
def interactive_menu():
    """PDF 维护工具合集全局主菜单 (常驻循环)"""
    args = argparse.Namespace()
    for attr in ('run', 'verbose', 'skip_download', 'scope', 'mode', 'keep', 'export_db', 'export_csv', 'trash', 'db', 'workers', 'yes'):
        if attr == 'scope':
            setattr(args, attr, 'unknown')
        elif attr == 'mode':
            setattr(args, attr, 'all')
        elif attr == 'keep':
            setattr(args, attr, 'primary')
        elif attr == 'workers':
            setattr(args, attr, 4)
        elif attr == 'db':
            setattr(args, attr, None)
        elif attr == 'export_db':
            setattr(args, attr, True)
        else:
            setattr(args, attr, False)

    while True:
        print_banner("PDF 全生命周期维护工具合集")
        print("  请选择要运行的功能：")
        print()
        print("    1. check-dates   - 检查 PDF 文件与数据库日期的匹配情况并生成报告")
        print("    2. fix-paths     - 将 Unknown_Year 中的 PDF 移到正确年份文件夹 + 全量修复文件名日期不匹配")
        print("    3. redownload    - 重新下载体积小于 20KB 的 PDF 文件")
        print("    4. rebuild       - 重建缺失的 PDF 文件并路径相对化 (多线程并发)")
        print("    5. orphan        - 检查多余PDF或将多余PDF移回原处")
        print("    6. associate     - 扫描未关联/断链 PDF 智能回填数据库")
        print("    7. clean-missing - 清理物理文件已删除但数据库仍残留的脏记录 (级联清理 PDF)")
        print("    8. dedup         - PDF 物理文件多维查重、去重与数据库引用纠偏")
        print()
        print("    0. 退出程序")
        print("=" * 60)

        try:
            choice = input("  请输入序号 [0-8] (直接回车默认 1): ").strip()
            if not choice:
                choice = "1"
        except (KeyboardInterrupt, EOFError):
            print("\n[-] 运行已取消")
            break

        if choice in ("0", "q", "quit", "exit"):
            print_step("已退出程序。")
            break

        if choice == "1":
            run_check_dates(args)
        elif choice == "2":
            setattr(args, "run", confirm_action("是否正式执行路径与文件名重命名修复？", default=False))
            run_fix_names_and_paths(args)
        elif choice == "3":
            setattr(args, "run", confirm_action("是否正式启动 Playwright 重新抓取损坏的 PDF？", default=False))
            run_redownload_small_pdfs(args)
        elif choice == "4":
            setattr(args, "run", confirm_action("是否正式启动重建缺失 PDF 任务？", default=False))
            run_rebuild(args)
        elif choice == "5":
            run_orphan(args)
        elif choice == "6":
            setattr(args, "run", confirm_action("是否正式将未关联物理文件写入数据库？", default=False))
            run_associate(args)
        elif choice == "7":
            setattr(args, "run", confirm_action("是否正式清理缺失记录（【强制级联清理关联 PDF】）？", default=False))
            run_clean_missing_records(args)
        elif choice == "8":
            run_dedup(args)
        else:
            print_warning("无效的序号。")

        pause_for_user()


def main():
    setup_console_utf8()
    parser = argparse.ArgumentParser(
        description="PDF 维护工具合集 - 检查日期、修正路径、重新下载小文件、并发重建缺失文件、孤儿管理、智能关联、清理缺失记录、多维查重去重")
    subparsers = parser.add_subparsers(dest="command", help="可用的子命令")

    # check-dates
    p_check = subparsers.add_parser("check-dates", help="检查 PDF 文件与数据库日期的匹配情况并生成报告")
    p_check.set_defaults(func=run_check_dates)

    # fix-paths
    p_fix = subparsers.add_parser("fix-paths", help="将 Unknown_Year 中的 PDF 按数据库日期移到正确年份文件夹 + 全量检查修复文件名日期不匹配")
    p_fix.add_argument("--run", action="store_true", default=False,
                       help="正式运行修复，不加此参数时仅进行预览 (Dry Run)")
    p_fix.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_fix.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_fix.add_argument("--verbose", "-v", action="store_true", default=False,
                       help="详细输出每个文件的分析计划")
    p_fix.set_defaults(func=run_fix_names_and_paths)

    # redownload
    p_redl = subparsers.add_parser("redownload", help="重新下载体积小于 20KB 的 PDF 文件")
    p_redl.add_argument("--run", action="store_true", default=False,
                        help="正式运行修复，不加此参数时仅进行预览 (Dry Run)")
    p_redl.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_redl.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_redl.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="详细输出")
    p_redl.set_defaults(func=run_redownload_small_pdfs)

    # rebuild
    p_rebuild = subparsers.add_parser("rebuild", help="重建缺失的 PDF 文件并路径相对化 (支持多线程并发)")
    p_rebuild.add_argument("--run", action="store_true", default=False,
                           help="正式执行修复和更新，不加此参数时仅进行预览 (Dry Run)")
    p_rebuild.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_rebuild.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_rebuild.add_argument("--workers", "-w", type=int, default=4,
                           help="并发下载线程数 (默认 4)")
    p_rebuild.add_argument("--skip-download", action="store_true", default=False,
                           help="仅执行路径相对化和纠偏，不重新下载物理缺失的文件")
    p_rebuild.add_argument("--db", default=None,
                           help="指定自定义 SQLite 数据库路径")
    p_rebuild.set_defaults(func=run_rebuild)

    # orphan
    p_orphan = subparsers.add_parser("orphan", help="检查多余PDF或将多余PDF移回原处")
    p_orphan.set_defaults(func=run_orphan)

    # associate
    p_assoc = subparsers.add_parser("associate", help="扫描磁盘未关联/断链 PDF，通过标题与站点智能关联回填数据库")
    p_assoc.add_argument("--run", action="store_true", default=False,
                         help="正式执行数据库回填更新，不加此参数时仅进行预览 (Dry Run)")
    p_assoc.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_assoc.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_assoc.add_argument("--db", default=None,
                         help="指定自定义 SQLite 数据库路径")
    p_assoc.set_defaults(func=run_associate)

    # clean-missing
    p_clean_missing = subparsers.add_parser("clean-missing", help="清理数据库中对应物理 PDF 已不存在的脏记录")
    p_clean_missing.add_argument("--run", action="store_true", default=False,
                                 help="正式执行删除，不加此参数时仅进行预览 (Dry Run)")
    p_clean_missing.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_clean_missing.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_clean_missing.add_argument("--export-db", action="store_true", default=True, help="导出被删除记录至独立 .db 库 (默认开启)")
    p_clean_missing.add_argument("--export-csv", action="store_true", default=False, help="导出被删除记录至 CSV 审计表")
    p_clean_missing.add_argument("--scope", choices=["unknown", "all"], default="unknown",
                                 help="扫描范围: unknown (仅 Unknown_Year) 或 all (全部 PDF 记录)")
    p_clean_missing.add_argument("--db", default=None,
                                 help="指定自定义 SQLite 数据库路径")
    p_clean_missing.set_defaults(func=run_clean_missing_records)

    # dedup
    p_dedup = subparsers.add_parser("dedup", help="PDF 文件多维查重、去重与数据库引用纠偏")
    p_dedup.add_argument("--mode", "-m", choices=["all", "hash", "name", "title", "db"], default="all",
                         help="查重模式: hash (内容MD5), name/title (文件名与标题变体), db (数据库关联), all (全量综合，默认)")
    p_dedup.add_argument("--keep", "-k", choices=["primary", "larger", "newest", "oldest"], default="primary",
                         help="保留策略: primary (规范文件名优先，默认), larger (最大体积), newest (最新生成), oldest (最早生成)")
    p_dedup.add_argument("--run", action="store_true", default=False,
                         help="正式执行物理文件清理与数据库重定向，不加此参数时仅进行安全预览 (Dry Run)")
    p_dedup.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_dedup.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_dedup.add_argument("--export-db", action="store_true", default=True, help="导出查重审计明细至独立 .db 数据库 (默认开启)")
    p_dedup.add_argument("--export-csv", action="store_true", default=False,
                         help="导出查重明细至 CSV 审计表")
    p_dedup.add_argument("--trash", action="store_true", default=False,
                         help="将多余重复文件移动至 cache/pdf_trash 隔离区而非直接物理删除")
    p_dedup.add_argument("--db", default=None,
                         help="指定自定义 SQLite 数据库路径")
    p_dedup.add_argument("--workers", "-w", type=int, default=16,
                         help="多线程哈希计算线程数 (默认 16)")
    p_dedup.set_defaults(func=run_dedup)

    args = parser.parse_args()

    if args.command is None:
        interactive_menu()
    else:
        args.func(args)


if __name__ == "__main__":
    main()




