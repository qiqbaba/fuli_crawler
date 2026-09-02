# -*- coding: utf-8 -*-
"""viewer_maintenance.py - Streamlit 网页端维护管理中心适配与渲染模块

本模块将 fixes/ 目录下的全部维护、修复、去重、清洗、重建与系统工具无缝集成至 Web 界面，
提供 5 大核心运维面板：
1. PDF 生命周期维护 (pdf_maintenance.py)
2. PDF 多维查重去重 (pdf_dedup.py)
3. 数据清洗与元数据 (data_cleaner.py)
4. 记录过滤与番号分离 (record_filter.py)
5. 缓存预热与数据库运维 (warmup_pdf_cache.py / db_utils.py)
"""

import io
import os
import re
import sys
import time
import glob
import shutil
import sqlite3
import hashlib
import contextlib
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import streamlit as st

# 项目配置与公共工具
from config import PROJECT_ROOT, PDF_BASE_DIR, get_db_path
from utils.pdf_utils import parse_filename, to_relative_path, clean_title_suffix
from utils.fanhao_filter import extract_fanhao
from utils.resource_link_cleaner import clean_resource_link
from utils.ui_compact import T

# fixes 模块工具
from fixes.db_utils import (
    get_connection,
    get_columns,
    get_total_count,
    backup_db,
    format_size,
    vacuum_db,
    get_export_dir,
    get_timestamp,
    export_records_to_db,
    export_to_csv,
    delete_records_cascade_pdf,
)
from fixes.pdf_dedup import (
    scan_all_physical_pdfs,
    find_hash_duplicates,
    find_name_variant_duplicates,
    find_db_pdf_duplicates,
    export_dedup_csv,
    export_dedup_db,
)
from fixes.data_cleaner import (
    find_dirty_link_records,
    sync_supabase_links,
    sync_dynamodb_links,
)
from fixes.record_filter import (
    get_all_duplicates,
    scan_fanhao_records,
    DUPLICATE_FIELDS,
)
from fixes.warmup_pdf_cache import PDF_THUMB_CACHE_DIR, render_single_pdf


# ===================================================================
# 终端输出捕获器（用于展示底层 fixes CLI 函数的实时运行日志）
# ===================================================================
class LogCapture:
    """实时捕获 stdout / stderr 输出并提供格式化文本"""
    def __init__(self):
        self.buffer = io.StringIO()

    def __enter__(self):
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = self.buffer
        sys.stderr = self.buffer
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout = self._stdout
        sys.stderr = self._stderr

    def get_text(self) -> str:
        return self.buffer.getvalue()


# ===================================================================
# 1. 面板一： PDF 生命周期维护 (fixes/pdf_maintenance.py)
# ===================================================================

def render_tab_pdf_maintenance():
    st.markdown(T("### PDF 物理文件全生命周期维护与数据库同步"))
    st.caption(T("对应 `fixes/pdf_maintenance.py`：提供日期比对审计、路径纠偏、微小损坏重抓、缺失重建、孤儿隔离还原及断链关联功能。"))
    
    db_path = get_db_path()
    
    tool_choice = st.radio(
        T("选择 PDF 维护子功能"),
        [
            "1. 日期比对审计 (check-dates)",
            "2. 路径与文件名纠偏 (fix-paths)",
            "3. 微小/损坏 PDF 重抓 (redownload <20KB)",
            "4. 缺失 PDF 并发重建 (rebuild)",
            "5. 孤立 PDF 隔离与还原 (orphan)",
            "6. 磁盘未关联 PDF 智能回填 (associate)",
            "7. 清理失效 PDF 记录 (clean-missing)",
        ],
        horizontal=True,
        key="pdf_maint_subtool",
        format_func=T
    )
    
    st.markdown("---")
    
    # ---------------- 1. 日期比对审计 ----------------
    if tool_choice.startswith("1."):
        st.markdown(T("#### PDF 文件与数据库发布日期比对审计"))
        st.info(T("递归扫描 `pdf/` 物理文件，提取文件名中的日期与标题，比对数据库中的 `publish_time`，并可一键生成 Markdown 审计报告。"))
        
        c1, c2 = st.columns([1, 4])
        with c1:
            run_audit = st.button(T("开始全量日期比对审计"), type="primary", use_container_width=True)
            
        if run_audit:
            with st.spinner("正在扫描物理文件与比对数据库记录，请稍候..."):
                pdf_files = []
                for root, _, files in os.walk(PDF_BASE_DIR):
                    for f in files:
                        if f.lower().endswith(".pdf"):
                            pdf_files.append((f, os.path.join(root, f)))
                
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT id, title, publish_time, pdf_path, url FROM resources")
                    db_rows = cursor.fetchall()
                
                db_by_pdf_fn = defaultdict(list)
                db_by_title = defaultdict(list)
                for row in db_rows:
                    r_id, r_title, r_pub, r_pdf, r_url = row
                    pub = r_pub.strip() if r_pub else "Unknown_Date"
                    rec = {"id": r_id, "title": r_title, "publish_time": pub, "pdf_path": r_pdf, "url": r_url}
                    if r_pdf:
                        fn = os.path.basename(r_pdf.replace('\\', '/')).lower()
                        db_by_pdf_fn[fn].append(rec)
                    if r_title:
                        db_by_title[r_title.strip()].append(rec)
                
                results = {"matched_ok": [], "date_mismatch": [], "db_not_found": [], "multiple_conflict": []}
                
                for filename, full_path in pdf_files:
                    fn_date, fn_title_part = parse_filename(filename)
                    fn_clean = clean_title_suffix(fn_title_part)
                    matched = []
                    
                    if filename.lower() in db_by_pdf_fn:
                        matched = db_by_pdf_fn[filename.lower()]
                    if not matched:
                        if fn_title_part in db_by_title:
                            matched = db_by_title[fn_title_part]
                        elif fn_clean in db_by_title:
                            matched = db_by_title[fn_clean]
                            
                    if not matched:
                        results["db_not_found"].append({"filename": filename, "path": full_path, "parsed_date": fn_date, "title": fn_title_part})
                    elif len(matched) == 1:
                        rec = matched[0]
                        if rec["publish_time"] == fn_date:
                            results["matched_ok"].append({"filename": filename, "id": rec["id"], "date": fn_date})
                        else:
                            results["date_mismatch"].append({
                                "filename": filename,
                                "id": rec["id"],
                                "file_date": fn_date,
                                "db_date": rec["publish_time"],
                                "title": rec["title"],
                                "path": full_path
                            })
                    else:
                        unique_dates = list(set(r["publish_time"] for r in matched))
                        if len(unique_dates) == 1:
                            if unique_dates[0] == fn_date:
                                results["matched_ok"].append({"filename": filename, "id": matched[0]["id"], "date": fn_date})
                            else:
                                results["date_mismatch"].append({
                                    "filename": filename,
                                    "id": matched[0]["id"],
                                    "file_date": fn_date,
                                    "db_date": unique_dates[0],
                                    "title": matched[0]["title"],
                                    "path": full_path
                                })
                        else:
                            results["multiple_conflict"].append({
                                "filename": filename,
                                "file_date": fn_date,
                                "matched_ids": [r["id"] for r in matched],
                                "db_dates": unique_dates,
                                "path": full_path
                            })
                
                st.session_state["check_dates_results"] = results
                st.session_state["check_dates_total_files"] = len(pdf_files)
                st.session_state["check_dates_total_db"] = len(db_rows)
        
        if "check_dates_results" in st.session_state:
            res = st.session_state["check_dates_results"]
            m_cols = st.columns(4)
            m_cols[0].metric(T("日期完全匹配"), f"{len(res['matched_ok']):,} 个")
            m_cols[1].metric(T("日期不一致"), f"{len(res['date_mismatch']):,} 个", delta=f"-{len(res['date_mismatch'])}", delta_color="inverse")
            m_cols[2].metric(T("数据库无记录 (孤儿)"), f"{len(res['db_not_found']):,} 个", delta=f"-{len(res['db_not_found'])}", delta_color="inverse")
            m_cols[3].metric(T("多重冲突"), f"{len(res['multiple_conflict']):,} 个")
            
            if res["date_mismatch"]:
                st.markdown(T("##### 日期不一致明细列表"))
                df_mismatch = pd.DataFrame(res["date_mismatch"])
                st.dataframe(df_mismatch, use_container_width=True, hide_index=True)
                
            if res["db_not_found"]:
                with st.expander(T(f"查看未找到数据库记录的孤儿文件 ({len(res['db_not_found'])} 个)")):
                    st.dataframe(pd.DataFrame(res["db_not_found"]), use_container_width=True, hide_index=True)

    # ---------------- 2. 路径与文件名纠偏 ----------------
    elif tool_choice.startswith("2."):
        st.markdown(T("#### PDF 文件名日期修正与年份目录纠偏 (fix-paths)"))
        st.info(T("纠偏 Unknown_Year 下的 Unknown_Date 文件，根据数据库发布日期重命名并迁移至正确年份文件夹，同时同步更新数据库中的 `pdf_path`。"))
        
        f_cols = st.columns([1, 1, 2])
        with f_cols[0]:
            btn_preview = st.button(T("扫描并预览纠偏计划 (Dry Run)"), use_container_width=True)
        with f_cols[1]:
            btn_run_fix = st.button(T("正式执行纠偏移动与改名"), type="primary", use_container_width=True)
            
        if btn_preview or btn_run_fix:
            from fixes.pdf_maintenance import run_fix_names_and_paths
            import argparse
            args = argparse.Namespace(
                run=btn_run_fix,
                verbose=False,
                db=db_path
            )
            with st.spinner("正在执行路径纠偏扫描..." if btn_preview else "正在执行正式改名与迁移..."):
                with LogCapture() as log:
                    try:
                        run_fix_names_and_paths(args)
                    except SystemExit:
                        pass
                
                output = log.get_text()
                if btn_run_fix:
                    st.success(T("纠偏执行完成！请查看下方日志："))
                else:
                    st.info(T("预览扫描完成："))
                st.code(output, language="text")

    # ---------------- 3. 微小/损坏 PDF 重抓 ----------------
    elif tool_choice.startswith("3."):
        st.markdown(T("#### 重新抓取渲染体积过小 (<20KB) 的损坏 PDF (redownload)"))
        st.info(T("扫描物理目录中体积小于 20KB 的损坏/空白 PDF，拉起 Playwright 无头浏览器重新访问源 URL，渲染生成标准 A4 边距 PDF 并覆盖旧文件。"))
        
        r_cols = st.columns([1, 1, 2])
        with r_cols[0]:
            r_workers = st.slider("并发下载线程数", min_value=1, max_value=8, value=4, key="redownload_workers")
        with r_cols[1]:
            st.write("")
            st.write("")
            btn_redownload_run = st.button(T("启动 Playwright 重新抓取渲染"), type="primary", use_container_width=True)
            
        if btn_redownload_run:
            from fixes.pdf_maintenance import run_redownload_small_pdfs
            import argparse
            args = argparse.Namespace(
                run=True,
                workers=r_workers,
                dry_run=False,
                verbose=False,
                db=db_path
            )
            with st.status(T("正在拉起 Playwright 重新渲染损坏 PDF..."), expanded=True) as status:
                with LogCapture() as log:
                    try:
                        run_redownload_small_pdfs(args)
                    except SystemExit:
                        pass
                status.update(label=T("重抓渲染完成！"), state="complete", expanded=True)
                st.code(log.get_text(), language="text")

    # ---------------- 4. 缺失 PDF 并发重建 ----------------
    elif tool_choice.startswith("4."):
        st.markdown(T("#### 重建缺失 PDF 文件与路径相对化 (rebuild)"))
        st.info(T("扫描数据库中所有记录，将绝对路径统一转换为相对路径；对本地物理缺失的 PDF 支持多线程 Playwright 并发重新生成。"))
        
        rb_cols = st.columns([1, 1, 1, 1])
        with rb_cols[0]:
            rb_workers = st.slider("并发重建线程数", min_value=1, max_value=8, value=4, key="rebuild_workers")
        with rb_cols[1]:
            skip_dl = st.checkbox("仅相对化路径 (跳过下载)", value=False, key="rebuild_skip_dl")
        with rb_cols[2]:
            st.write("")
            btn_rebuild_prev = st.button(T("预览缺失清单 (Dry Run)"), use_container_width=True)
        with rb_cols[3]:
            st.write("")
            btn_rebuild_run = st.button(T("正式执行重建/相对化"), type="primary", use_container_width=True)
            
        if btn_rebuild_prev or btn_rebuild_run:
            from fixes.pdf_maintenance import run_rebuild
            import argparse
            args = argparse.Namespace(
                run=btn_rebuild_run,
                workers=rb_workers,
                skip_download=skip_dl,
                db=db_path
            )
            with st.status(T("正在扫描与重建..." if btn_rebuild_run else "正在扫描缺失情况..."), expanded=True) as status:
                with LogCapture() as log:
                    try:
                        run_rebuild(args)
                    except SystemExit:
                        pass
                status.update(label=T("操作完成！" if btn_rebuild_run else "预览扫描完成"), state="complete", expanded=True)
                st.code(log.get_text(), language="text")

    # ---------------- 5. 孤立 PDF 隔离与还原 ----------------
    elif tool_choice.startswith("5."):
        st.markdown(T("#### 多余/孤立 PDF 文件管理与还原 (orphan)"))
        st.info(T("将数据库中无记录的多余/废弃 PDF 隔离移至 `/pdf` 根目录；或从根目录智能分析归属还原归位回年份子目录。"))
        
        orphan_mode = st.radio("选择操作模式", ["模式 1: 扫描各年份目录，将多余无记录 PDF 隔离到根目录", "模式 2: 扫描根目录隔离文件，智能恢复归位至年份子目录"], key="orphan_mode_sel")
        
        o_cols = st.columns([1, 1, 2])
        with o_cols[0]:
            btn_orphan_prev = st.button(T("预览隔离/归位计划"), use_container_width=True)
        with o_cols[1]:
            btn_orphan_run = st.button(T("正式执行隔离/归位"), type="primary", use_container_width=True)
            
        if btn_orphan_prev or btn_orphan_run:
            # fixes/pdf_maintenance.py 实际提供的是 _move_orphans_to_root / _restore_orphans_from_root，
            # 二者在执行前通过 input() 交互确认；Web 环境下需按按钮意图自动应答（预览='n'，正式执行='y'）
            from fixes.pdf_maintenance import _move_orphans_to_root, _restore_orphans_from_root
            import argparse
            import builtins
            mode_num = 1 if "模式 1" in orphan_mode else 2
            pdf_base = os.path.abspath(PDF_BASE_DIR)
            args = argparse.Namespace(run=btn_orphan_run, db=db_path)
            with st.spinner("正在分析孤立文件..."):
                with LogCapture() as log:
                    _orig_input = builtins.input
                    builtins.input = lambda *a, **k: ("y" if btn_orphan_run else "n")
                    try:
                        if mode_num == 1:
                            _move_orphans_to_root(db_path, pdf_base, args)
                        else:
                            _restore_orphans_from_root(db_path, pdf_base, args)
                    except SystemExit:
                        pass
                    finally:
                        builtins.input = _orig_input
                st.code(log.get_text(), language="text")

    # ---------------- 6. 磁盘未关联 PDF 智能回填 ----------------
    elif tool_choice.startswith("6."):
        st.markdown(T("#### 扫描磁盘未关联/断链 PDF 智能回填数据库 (associate)"))
        st.info(T("精准比对数据库，找出物理文件存在但数据库 `pdf_path` 为空或断链的记录，通过标题与站点来源自动关联回填。"))
        
        a_cols = st.columns([1, 1, 2])
        with a_cols[0]:
            btn_assoc_prev = st.button(T("预览关联计划 (Dry Run)"), use_container_width=True)
        with a_cols[1]:
            btn_assoc_run = st.button(T("正式关联入库"), type="primary", use_container_width=True)
            
        if btn_assoc_prev or btn_assoc_run:
            from fixes.pdf_maintenance import run_associate
            import argparse
            args = argparse.Namespace(
                run=btn_assoc_run,
                db=db_path
            )
            with st.spinner("正在分析未关联 PDF..."):
                with LogCapture() as log:
                    try:
                        run_associate(args)
                    except SystemExit:
                        pass
                st.code(log.get_text(), language="text")

    # ---------------- 7. 清理失效 PDF 记录 ----------------
    elif tool_choice.startswith("7."):
        st.markdown(T("#### 清理数据库中对应物理 PDF 已丢失的残留脏记录 (clean-missing)"))
        st.info(T("反向扫描数据库，检测 `pdf_path` 指向的物理文件是否真实存在，批量删除物理文件已不存在的数据库脏记录，并自动 VACUUM 回收空间。"))
        
        cm_scope = st.radio("清理作用域", ["unknown (仅清理 Unknown_Year 目录下失效记录)", "all (全量检查并清理所有年份失效记录)"], key="cm_scope_radio")
        scope_val = "unknown" if "unknown" in cm_scope else "all"
        
        cm_cols = st.columns([1, 1, 2])
        with cm_cols[0]:
            btn_cm_prev = st.button(T("预览待清理记录 (Dry Run)"), use_container_width=True)
        with cm_cols[1]:
            btn_cm_run = st.button(T("正式批量删除并压缩数据库"), type="primary", use_container_width=True)
            
        if btn_cm_prev or btn_cm_run:
            from fixes.pdf_maintenance import run_clean_missing_records
            import argparse
            args = argparse.Namespace(
                run=btn_cm_run,
                scope=scope_val,
                db=db_path
            )
            with st.spinner("正在检查物理文件有效性..."):
                with LogCapture() as log:
                    try:
                        run_clean_missing_records(args)
                    except SystemExit:
                        pass
                st.code(log.get_text(), language="text")


# ===================================================================
# 2. 面板二： PDF 多维查重去重 (fixes/pdf_dedup.py)
# ===================================================================

def render_tab_pdf_dedup():
    st.markdown(T("### PDF 物理文件多维查重、智能去重与数据库自动重定向纠偏"))
    st.caption(T("对应 `fixes/pdf_dedup.py`：支持 MD5 三阶段快速哈希查重、文件名变体查重、数据库引用共享查重与全量去重。"))
    
    db_path = get_db_path()
    
    col1, col2, col3, col4 = st.columns([1.1, 1.5, 0.9, 0.9])
    with col1:
        mode = st.selectbox(
            T("查重维度 (Mode)"),
            ["hash (MD5 内容完全一致)", "name (文件名变体如 _1.pdf)", "db (数据库路径共享与无效引用)", "all (全量多维综合查重)"],
            key="dedup_mode_sel",
            format_func=T
        )
        mode_val = mode.split(" ")[0]
    with col2:
        keep = st.selectbox(
            T("保留策略 (Keep)"),
            [
                "primary (推荐: 规范性得分 > 体积最大 > 最新修改)",
                "larger (优先体积: 体积最大 > 规范性得分 > 最新修改)",
                "newest (优先最新: 修改时间最新 > 规范性得分 > 体积最大)",
                "oldest (优先最早: 修改时间最早 > 规范性得分 > 体积最大)"
            ],
            key="dedup_keep_sel",
            help="【保留判定完整条件链】\n1. 规范性打分：不在 Unknown_Year (+100分) > 含有效日期 YYYY-MM-DD (+50分) > 无 _1/_2 序号后缀 (+30分)\n2. 体积比较：文件字节大小 (bytes)\n3. 时间排序：文件最后修改时间 (mtime)",
            format_func=T
        )
        keep_val = keep.split(" ")[0]
    with col3:
        use_trash = st.checkbox("移入隔离区 (.trash)", value=False, help="若勾选，则将待删除副本移入隔离区而非直接删除。")
    with col4:
        export_csv_opt = st.checkbox("导出 CSV 审计表", value=True)
        
    st.markdown("---")
    
    b_cols = st.columns([1, 1, 1, 2])
    with b_cols[0]:
        btn_preview = st.button(T("扫描并预览查重结果 (Dry Run)"), type="primary", use_container_width=True)
    with b_cols[1]:
        btn_run_dedup = st.button(T("正式执行去重纠偏 (Run)"), use_container_width=True)
        
    if btn_preview or btn_run_dedup:
        from fixes.pdf_dedup import run_pdf_dedup
        with st.status(T("正在进行全量多维 PDF 查重扫描..." if btn_preview else "正在执行安全去重与数据库重定向..."), expanded=True) as status:
            with LogCapture() as log:
                try:
                    run_pdf_dedup(
                        mode=mode_val,
                        keep=keep_val,
                        run=btn_run_dedup,
                        export_csv=export_csv_opt,
                        export_db=True,
                        trash=use_trash,
                        db_path=db_path
                    )
                except SystemExit:
                    pass
            status.update(label=T("去重纠偏完成！" if btn_run_dedup else "查重预览完成"), state="complete", expanded=True)
            st.code(log.get_text(), language="text")


# ===================================================================
# 3. 面板三： 数据清洗与元数据 (fixes/data_cleaner.py)
# ===================================================================

def render_tab_data_cleaner():
    st.markdown(T("### 数据清洗、域名替换与元数据修复工具箱"))
    st.caption(T("对应 `fixes/data_cleaner.py`：提供广告标签清洗、多云同步、域名替换、表结构升级、磁力大小补全与空链接回填。"))
    
    db_path = get_db_path()
    
    sub_tool = st.radio(
        T("选择数据清洗子功能"),
        [
            "1. 广告与推广噪声清洗 (clean-noise)",
            "2. 域名/镜像批量替换 (replace-domain)",
            "3. 表结构升级与元数据提取 (upgrade-db)",
            "4. Darklyn 磁力大小并发补全 (fetch-sizes)",
            "5. 空资源链接 Playwright 重抓 (fetch-empty-links)",
        ],
        horizontal=True,
        key="cleaner_subtool",
        format_func=T
    )
    
    st.markdown("---")
    
    # ---------------- 1. 广告与推广噪声清洗 ----------------
    if sub_tool.startswith("1."):
        st.markdown(T("#### 清理 resource_link 广告与标签噪声 (clean-noise)"))
        st.info(T("扫描 `resource_link` 剔除广告推广行、下载渠道废弃说明、多余标签以及无用空行。支持同步云端 Supabase 与 AWS DynamoDB。"))
        
        sync_sup = st.checkbox("同步更新至云端 Supabase", value=False)
        sync_dyn = st.checkbox("同步更新至 AWS DynamoDB", value=False)
        exp_csv = st.checkbox("导出清洗前后明细 CSV", value=True)
        
        c1, c2 = st.columns([1, 1])
        with c1:
            btn_prev = st.button(T("扫描并预览噪声记录 (Dry Run)"), use_container_width=True)
        with c2:
            btn_run = st.button(T("正式执行清洗写入数据库"), type="primary", use_container_width=True)
            
        if btn_prev or btn_run:
            from fixes.data_cleaner import run_clean_noise
            import argparse
            args = argparse.Namespace(
                run=btn_run,
                yes=btn_run,
                export_csv=exp_csv,
                sync_supabase=sync_sup,
                sync_dynamodb=sync_dyn,
                db=db_path
            )
            with st.spinner("正在扫描与清洗链接噪声..."):
                with LogCapture() as log:
                    try:
                        run_clean_noise(args)
                    except SystemExit:
                        pass
                st.code(log.get_text(), language="text")

    # ---------------- 2. 域名/镜像批量替换 ----------------
    elif sub_tool.startswith("2."):
        st.markdown(T("#### 批量替换 URL 中的域名或镜像子串 (replace-domain)"))
        st.info(T("当采集源网站域名变更或发布页镜像更新时，批量替换 `resources.url` 中的旧域名（如 `dyh.393659.xyz` 替换为 `dtn.628563.xyz`）。"))
        
        presets = {
            "大唐 (datang)": ("dyh.393659.xyz", "dtn.628563.xyz"),
            "大神 (dashen)": ("dsh.123456.xyz", "dsh.789012.xyz"),
            "精品 (jingpin)": ("jpbt1.com", "jpbt3.com"),
            "探花 (tanhua)": ("thbt1.com", "thbt8.com"),
        }
        
        p_choice = st.selectbox("常用预设快捷填入", ["自定义"] + list(presets.keys()))
        default_old = presets[p_choice][0] if p_choice != "自定义" else "dyh.393659.xyz"
        default_new = presets[p_choice][1] if p_choice != "自定义" else "dtn.628563.xyz"
        
        c1, c2 = st.columns(2)
        with c1:
            old_str = st.text_input("待替换的旧域名 / 子串", value=default_old)
        with c2:
            new_str = st.text_input("替换为的新域名 / 子串", value=default_new)
            
        b1, b2 = st.columns([1, 1])
        with b1:
            btn_rep_prev = st.button(T("预览替换匹配样例"), use_container_width=True)
        with b2:
            btn_rep_run = st.button(T("正式执行替换并备份数据库"), type="primary", use_container_width=True)
            
        if btn_rep_prev or btn_rep_run:
            from fixes.data_cleaner import run_replace_domain
            import argparse
            args = argparse.Namespace(
                old=old_str,
                new=new_str,
                run=btn_rep_run,
                yes=btn_rep_run,
                db=db_path
            )
            with st.spinner("正在查找匹配记录..."):
                with LogCapture() as log:
                    try:
                        run_replace_domain(args)
                    except SystemExit:
                        pass
                st.code(log.get_text(), language="text")

    # ---------------- 3. 表结构升级与元数据提取 ----------------
    elif sub_tool.startswith("3."):
        st.markdown(T("#### 数据库表结构升级与历史元数据提取 (upgrade-db)"))
        st.info(T("自动升级 `resources` 表对齐标准 12 字段并建立索引；从历史记录提取 `size`、`resource_format`、`pikpak_link` 并批量回填。"))
        
        u1, u2 = st.columns([1, 1])
        with u1:
            btn_up_prev = st.button(T("检查表结构与元数据缺失 (Dry Run)"), use_container_width=True)
        with u2:
            btn_up_run = st.button(T("正式升级表结构并回填元数据"), type="primary", use_container_width=True)
            
        if btn_up_prev or btn_up_run:
            from fixes.data_cleaner import run_upgrade_db
            import argparse
            args = argparse.Namespace(
                run=btn_up_run,
                yes=btn_up_run,
                db=db_path
            )
            with st.spinner("正在检测与升级表结构..."):
                with LogCapture() as log:
                    try:
                        run_upgrade_db(args)
                    except SystemExit:
                        pass
                st.code(log.get_text(), language="text")

    # ---------------- 4. Darklyn 磁力大小并发补全 ----------------
    elif sub_tool.startswith("4."):
        st.markdown(T("#### Darklyn API 磁力链接大小批量补全 (fetch-sizes)"))
        st.info(T("扫描数据库中缺失 `size` 的磁力链接，并发调用 Darklyn API 批量查询真实文件大小并回填数据库。"))
        
        c1, c2 = st.columns(2)
        with c1:
            limit_val = st.number_input("单次抓取条数限制 (0 表示无限制)", min_value=0, max_value=100000, value=200, step=100)
        with c2:
            workers_val = st.slider("并发请求线程数", min_value=1, max_value=16, value=6)
            
        b1, b2 = st.columns([1, 1])
        with b1:
            btn_fs_prev = st.button(T("扫描缺失大小的磁力记录"), use_container_width=True)
        with b2:
            btn_fs_run = st.button(T("启动并发查询并回填数据库"), type="primary", use_container_width=True)
            
        if btn_fs_prev or btn_fs_run:
            from fixes.data_cleaner import run_fetch_sizes
            import argparse
            args = argparse.Namespace(
                limit=limit_val,
                workers=workers_val,
                run=btn_fs_run,
                apply=btn_fs_run,
                watch=False,
                db=db_path
            )
            with st.status(T("正在并发请求 Darklyn API 查询磁力大小..."), expanded=True) as status:
                with LogCapture() as log:
                    try:
                        run_fetch_sizes(args)
                    except SystemExit:
                        pass
                status.update(label=T("磁力大小补全完成！" if btn_fs_run else "扫描完成"), state="complete", expanded=True)
                st.code(log.get_text(), language="text")

    # ---------------- 5. 空资源链接 Playwright 重抓 ----------------
    elif sub_tool.startswith("5."):
        st.markdown(T("#### Playwright 重新访问页面抓取并回填空资源链接 (fetch-empty-links)"))
        st.info(T("针对数据库中 `resource_link` 为空的记录，拉起 Playwright 无头浏览器重新请求页面，解析正文回填资源链接。"))
        
        site_filter = st.text_input("站点域名过滤 (如 seju.life，留空表示全部站点)", value="seju.life")
        
        b1, b2 = st.columns([1, 1])
        with b1:
            btn_fel_prev = st.button(T("扫描空链接记录清单"), use_container_width=True)
        with b2:
            btn_fel_run = st.button(T("启动 Playwright 重新访问解析入库"), type="primary", use_container_width=True)
            
        if btn_fel_prev or btn_fel_run:
            from fixes.data_cleaner import run_fetch_empty_links
            import argparse
            args = argparse.Namespace(
                site=site_filter,
                run=btn_fel_run,
                yes=btn_fel_run,
                db=db_path
            )
            with st.status(T("正在拉起 Playwright 重新抓取空链接..."), expanded=True) as status:
                with LogCapture() as log:
                    try:
                        run_fetch_empty_links(args)
                    except SystemExit:
                        pass
                status.update(label=T("重抓完成！" if btn_fel_run else "扫描完成"), state="complete", expanded=True)
                st.code(log.get_text(), language="text")


# ===================================================================
# 4. 面板四： 记录过滤与番号分离 (fixes/record_filter.py)
# ===================================================================

def render_tab_record_filter():
    st.markdown(T("### 记录多维查重、番号分离与级联安全清理"))
    st.caption(T("对应 `fixes/record_filter.py`：支持 URL/磁力/标题组合多维查重、独立 DB 导出、批量去重（级联删除 PDF）以及日本番号识别提取。"))
    
    db_path = get_db_path()
    
    sub_tool = st.radio(
        T("选择过滤与去重子功能"),
        [
            "1. 数据库多维记录查重与级联清理 (duplicates)",
            "2. 严格日本番号识别、分布统计与独立库导出 (fanhao)",
        ],
        horizontal=True,
        key="record_filter_subtool",
        format_func=T
    )
    
    st.markdown("---")
    
    # ---------------- 1. 数据库多维记录查重 ----------------
    if sub_tool.startswith("1."):
        st.markdown(T("#### 数据库记录多维查重、独立 DB 导出与批量去重 (强制级联删除 PDF)"))
        st.info(T("检测数据库重复记录，支持导出为独立 SQLite `.db` 库。**去重判定条件**：① 优先在含物理 PDF 记录中筛选（防误删丢失文件）；② 再按 ID 最大 (最新) 或 ID 最小 (最旧) 保留唯一一条；③ 对多余副本**强制同步级联删除关联的物理 PDF 文件**。"))
        
        c1, c2, c3 = st.columns([1.1, 1.4, 0.9])
        with c1:
            field_choice = st.selectbox(
                T("查重维度 (Field)"),
                ["url (URL 地址重复)", "resource_link (磁力链接重复)", "title_link (标题 + 磁力链接联合重复)", "all (全维度综合查重)"],
                key="dup_field_sel",
                format_func=T
            )
            field_val = field_choice.split(" ")[0]
        with c2:
            keep_choice = st.selectbox(
                T("去重保留策略"),
                [
                    "newest (优先含PDF记录 > 最新入库: ID 最大)",
                    "oldest (优先含PDF记录 > 最旧入库: ID 最小)"
                ],
                key="dup_keep_choice",
                help="【去重保留判定完整条件链】\n1. PDF 优先判定：若组内存在已生成 PDF 的记录 (pdf_path 非空)，优先在含 PDF 的候选集中筛选（防止误删导致 PDF 孤立丢失）；\n2. ID 时序排序：在候选集中按 ID 最大 (最新入库) 或 ID 最小 (最早入库) 确定唯一保留记录；\n3. 物理级联清理：对其余冗余副本强制从数据库删除，并同步物理级联删除关联的本地 PDF 文件。",
                format_func=T
            )
            keep_val = keep_choice.split(" ")[0]
        with c3:
            export_db_opt = st.checkbox("默认导出为独立 SQLite .db", value=True)
            export_csv_opt = st.checkbox("同时导出为 CSV 审计表", value=False)
            
        b1, b2, b3 = st.columns([1, 1, 2])
        with b1:
            btn_dup_prev = st.button(T("扫描并预览重复记录 (Dry Run)"), type="primary", use_container_width=True)
        with b2:
            btn_dup_run = st.button(T("批量去重并级联删除 PDF"), use_container_width=True)
            
        if btn_dup_prev or btn_dup_run:
            from fixes.record_filter import run_duplicates_cli
            import argparse
            args = argparse.Namespace(
                field=field_val,
                keep=keep_val,
                run=btn_dup_run,
                export_db=export_db_opt,
                export_csv=export_csv_opt,
                db=db_path
            )
            with st.status(T("正在扫描重复记录..." if btn_dup_prev else "正在执行批量去重与物理 PDF 级联删除..."), expanded=True) as status:
                with LogCapture() as log:
                    try:
                        run_duplicates_cli(args)
                    except SystemExit:
                        pass
                status.update(label=T("去重与级联删除完成！" if btn_dup_run else "查重完成"), state="complete", expanded=True)
                st.code(log.get_text(), language="text")

    # ---------------- 2. 严格日本番号识别 ----------------
    elif sub_tool.startswith("2."):
        st.markdown(T("#### 严格日本番号识别、前缀分布统计与独立库导出 (fanhao)"))
        st.info(T("基于严格算法精准识别标题中的日本番号，统计 Top 20 厂商前缀分布；可一键将番号记录导出为独立 SQLite 库，或批量删除并级联清理对应 PDF。"))
        
        mode_act = st.radio("操作模式", ["preview (扫描与前缀分布统计)", "export (导出为全新独立 SQLite 数据库)", "delete (批量删除番号记录并级联清理关联 PDF)"], horizontal=True, key="fanhao_act_mode")
        mode_val = mode_act.split(" ")[0]
        
        b1, b2 = st.columns([1, 2])
        with b1:
            btn_fh_run = st.button(T("执行番号操作"), type="primary", use_container_width=True)
            
        if btn_fh_run:
            from fixes.record_filter import run_fanhao_cli
            import argparse
            args = argparse.Namespace(
                mode=mode_val,
                run=True,
                yes=True,
                db=db_path
            )
            with st.status(T("正在执行日本番号识别与处理..."), expanded=True) as status:
                with LogCapture() as log:
                    try:
                        run_fanhao_cli(args)
                    except SystemExit:
                        pass
                status.update(label=T("番号处理完成！"), state="complete", expanded=True)
                st.code(log.get_text(), language="text")


# ===================================================================
# 5. 面板五： 缓存预热与数据库运维 (fixes/warmup_pdf_cache.py & db_utils.py)
# ===================================================================

def render_tab_system_and_cache():
    st.markdown(T("### PDF 缩略图全量并发预热与数据库运维"))
    st.caption(T("对应 `fixes/warmup_pdf_cache.py` 与 `fixes/db_utils.py`：提供 100% 缓存命中秒开预热、一键数据库备份与 VACUUM 碎片压缩。"))
    
    db_path = get_db_path()
    
    sub_tool = st.radio(
        T("选择运维子功能"),
        [
            "1. PDF 缩略图全量并发预热 (warmup_all_pdf_cache)",
            "2. 数据库一键备份与备份管理 (backup_db)",
            "3. 数据库碎片整理与压缩 (vacuum_db)",
        ],
        horizontal=True,
        key="sys_subtool",
        format_func=T
    )
    
    st.markdown("---")
    
    # ---------------- 1. PDF 缩略图全量并发预热 ----------------
    if sub_tool.startswith("1."):
        st.markdown(T("#### PDF 缩略图全量并发预热 (PDF Thumbnail Cache Warmup)"))
        st.info(T("批量/离线并发将数据库中所有 PDF 文件的第一页光栅化渲染为高质 JPEG 写入 `cache/pdf_thumbs`，浏览时 100% 命中缓存，实现 0 毫秒秒开。"))
        
        # 统计当前缓存状态
        os.makedirs(PDF_THUMB_CACHE_DIR, exist_ok=True)
        cached_flags = glob.glob(os.path.join(PDF_THUMB_CACHE_DIR, "*_done.flag"))
        
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
            total_pdf_recs = cursor.fetchone()[0]
            
        m1, m2, m3 = st.columns(3)
        m1.metric(T("数据库含 PDF 记录数"), f"{total_pdf_recs:,}")
        m2.metric(T("已生成预热缓存数"), f"{len(cached_flags):,}")
        m3.metric(T("预热覆盖率"), f"{(len(cached_flags) / total_pdf_recs * 100):.1f}%" if total_pdf_recs > 0 else "0%")
        
        c1, c2, c3 = st.columns(3)
        with c1:
            workers = st.slider("并发渲染线程数", min_value=1, max_value=32, value=min(16, (os.cpu_count() or 4) * 2))
        with c2:
            dpi_val = st.selectbox("渲染分辨率 (DPI)", [90, 105, 120, 150], index=1)
        with c3:
            quality_val = st.slider("JPEG 压缩质量", min_value=50, max_value=95, value=75)
            
        if st.button(T("启动全量并发缓存预热"), type="primary", use_container_width=True):
            from fixes.warmup_pdf_cache import resolve_pdf_path
            
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, title, pdf_path FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
                rows = cursor.fetchall()
                
            valid_tasks = []
            skipped = 0
            missing = 0
            
            for item_id, title, raw_path in rows:
                resolved = resolve_pdf_path(raw_path)
                if not resolved or not os.path.exists(resolved):
                    missing += 1
                    continue
                try:
                    mtime = os.path.getmtime(resolved)
                    key = hashlib.md5(f"{resolved}_{mtime}_{dpi_val}_{quality_val}".encode("utf-8")).hexdigest()
                    done_flag = os.path.join(PDF_THUMB_CACHE_DIR, f"{key}_done.flag")
                    if os.path.exists(done_flag):
                        skipped += 1
                    else:
                        valid_tasks.append((resolved, mtime, title))
                except Exception:
                    missing += 1
                    
            st.info(T(f"扫描完毕：已有缓存 {skipped} 个，文件失效 {missing} 个，待预热生成 {len(valid_tasks)} 个 PDF。"))
            
            if not valid_tasks:
                st.success(T("所有有效 PDF 均已完成预热！"))
            else:
                progress_bar = st.progress(0.0)
                status_text = st.empty()
                
                success_count = 0
                fail_count = 0
                completed = 0
                total = len(valid_tasks)
                
                t0 = time.perf_counter()
                
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    future_map = {
                        pool.submit(render_single_pdf, p, m, dpi_val, quality_val): t
                        for p, m, t in valid_tasks
                    }
                    for future in as_completed(future_map):
                        completed += 1
                        try:
                            if future.result():
                                success_count += 1
                            else:
                                fail_count += 1
                        except Exception:
                            fail_count += 1
                            
                        if completed % 10 == 0 or completed == total:
                            progress_bar.progress(completed / total)
                            rate = completed / max(0.001, (time.perf_counter() - t0))
                            status_text.text(T(f"预热进度: {completed}/{total} (成功 {success_count}, 失败 {fail_count}) | 速率: {rate:.1f} 页/秒"))
                            
                st.success(T(f"全量预热完成！本次成功渲染 {success_count} 个 PDF，总耗时 {(time.perf_counter() - t0):.2f} 秒。"))

    # ---------------- 2. 数据库一键备份与管理 ----------------
    elif sub_tool.startswith("2."):
        st.markdown(T("#### 数据库一键备份与历史备份管理 (backup_db)"))
        st.info(T("在执行任何维护操作前创建带精确时间戳的 `.bak` 备份文件，确保数据绝对安全。"))
        
        db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        st.write(T(f"**当前数据库文件**：`{db_path}` ({format_size(db_size)})"))
        
        if st.button(T("立即创建数据库备份 (.bak)"), type="primary"):
            with st.spinner("正在复制备份数据库文件..."):
                bak_path = backup_db(db_path, prefix_tag="manual_web")
                st.success(T(f"备份成功！备份文件位于：`{bak_path}` ({format_size(os.path.getsize(bak_path))})"))
                
        st.markdown(T("##### 历史备份列表"))
        bak_files = glob.glob(os.path.join(os.path.dirname(db_path), "*.bak"))
        if not bak_files:
            st.info("暂无历史备份文件。")
        else:
            bak_data = []
            for b in sorted(bak_files, key=os.path.getmtime, reverse=True):
                stat = os.stat(b)
                bak_data.append({
                    "备份文件名": os.path.basename(b),
                    "大小": format_size(stat.st_size),
                    "修改时间": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "完整路径": b
                })
            st.dataframe(pd.DataFrame(bak_data), use_container_width=True, hide_index=True)

    # ---------------- 3. 数据库碎片整理与压缩 ----------------
    elif sub_tool.startswith("3."):
        st.markdown(T("#### 数据库碎片整理与空间压缩 (vacuum_db)"))
        st.info(T("批量删除记录或更新数据后，SQLite 不会自动缩小文件体积。执行 `VACUUM` 可彻底清理碎片、重建索引并释放磁盘物理空间。"))
        
        size_before = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        st.metric(T("当前数据库物理体积"), format_size(size_before))
        
        if st.button(T("立即执行 VACUUM 压缩数据库"), type="primary"):
            with st.spinner("正在执行 VACUUM 碎片整理，大型数据库可能需要几秒到十几秒..."):
                # fixes.db_utils.vacuum_db 接收 sqlite3 连接且无返回值，需在调用前后自行测量文件体积
                size_b = os.path.getsize(db_path) if os.path.exists(db_path) else 0
                with sqlite3.connect(db_path) as conn:
                    vacuum_db(conn)
                size_a = os.path.getsize(db_path) if os.path.exists(db_path) else 0
                freed = max(0, size_b - size_a)
                st.success(T(f"压缩完成！原体积: {format_size(size_b)} 压缩后: {format_size(size_a)} (释放了 {format_size(freed)})"))


# ===================================================================
# 维护中心主入口函数
# ===================================================================

def render_maintenance_hub():
    """在 Streamlit 中渲染完整的 5 大维护面板"""
    st.markdown(f"""
    <div class="fixes-hub-header-banner">
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
            <div>
                <h2 class="fixes-hub-banner-title">
                    {T('福利资源库与 PDF 运维控制台 (Fixes Hub)')}
                </h2>
                <p class="fixes-hub-banner-desc">
                    已全量集成 <code>fixes/</code> 目录下的 5 大核心维护模块、20+ 项自动化修复、去重、清洗与系统运维功能。
                </p>
            </div>
            <div style="display: flex; gap: 8px;">
                <span class="fixes-hub-badge-blue">
                    {T('安全沙盒 / 支持 Dry Run')}
                </span>
                <span class="fixes-hub-badge-green">
                    {T('自动 BAK 备份')}
                </span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        T("1. PDF 维护与重建"),
        T("2. PDF 多维查重去重"),
        T("3. 数据清洗与元数据"),
        T("4. 记录过滤与番号分离"),
        T("5. 缓存预热与系统运维"),
    ])
    
    with tab1:
        render_tab_pdf_maintenance()
    with tab2:
        render_tab_pdf_dedup()
    with tab3:
        render_tab_data_cleaner()
    with tab4:
        render_tab_record_filter()
    with tab5:
        render_tab_system_and_cache()
