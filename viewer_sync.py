# -*- coding: utf-8 -*-
"""viewer_sync.py - 多平台云端同步、数据迁移与统计中心 Web 可视化面板

本模块将 sync/ 目录下的全部云端同步、对象存储流转、去重键维护与用量统计工具无缝集成至 Streamlit 界面，
提供 5 大核心云端与迁移面板：
1. 多云用量统计监控 (stats.py)
2. Supabase 云端同步与归档 (supabase_sync.py)
3. Cloudflare R2 PDF 管理与生命周期 (r2_sync.py)
4. AWS DynamoDB 去重与数据同步 (dynamodb_sync.py)
5. 本地链接与磁力轻量导出 (export_urls_magnets.py)
"""

import io
import os
import re
import sys
import time
import shutil
import sqlite3
import logging
import argparse
import threading
import contextlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import streamlit as st

# 项目配置与公共工具
from config import PROJECT_ROOT, PDF_BASE_DIR, get_db_path
from utils.ui_compact import T

logger = logging.getLogger("viewer_sync")


# ===================================================================
# 统一日志与终端实时流式捕获引擎（支持 print 与 logging 同步流式推流）
# ===================================================================

class LiveStreamWriter:
    """包装流，在写入时收集内容并按时间节流触发 UI 实时回调"""
    def __init__(self, buffer: io.StringIO, on_update=None, min_interval: float = 0.15):
        self.buffer = buffer
        self.on_update = on_update
        self.min_interval = min_interval
        self._last_time = 0.0
        self._lock = threading.Lock()
        self._in_callback = False

    def write(self, s: str):
        if not s:
            return
        with self._lock:
            self.buffer.write(s)
            if not self.on_update or self._in_callback:
                return
            now = time.time()
            if (now - self._last_time >= self.min_interval) or ("\n" in s and now - self._last_time >= 0.08):
                self._last_time = now
                self._in_callback = True
                try:
                    self.on_update(self.buffer.getvalue())
                except Exception:
                    pass
                finally:
                    self._in_callback = False

    def flush(self):
        with self._lock:
            self.buffer.flush()
            if self.on_update and not self._in_callback:
                self._in_callback = True
                try:
                    self.on_update(self.buffer.getvalue())
                except Exception:
                    pass
                finally:
                    self._in_callback = False


class LiveSyncLogCapture:
    """实时捕获 sys.stdout / sys.stderr 及相关 logger 输出并同步触发实时 UI 回调"""
    def __init__(
        self,
        on_update=None,
        min_interval: float = 0.15,
        logger_names: Optional[List[str]] = None
    ):
        self.buffer = io.StringIO()
        self.stream = LiveStreamWriter(self.buffer, on_update=on_update, min_interval=min_interval)
        self.logger_names = logger_names or [
            "fuli_crawler",               # 根节点：捕获所有 fuli_crawler.* 子 logger
            "viewer_sync",
            "fuli_crawler.sync.stats",
            "fuli_crawler.sync.dynamodb_sync",
            "fuli_crawler.sync.supabase_sync",
            "fuli_crawler.sync.r2_sync",
            "fuli_crawler.utils.deduplication",
            # 兼容旧名称（直接用 logging.getLogger 的模块）
            "sync.stats",
            "sync.dynamodb_sync",
            "sync.supabase_sync",
            "sync.r2_sync",
        ]
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        self._stdout = None
        self._stderr = None

    def __enter__(self):
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = self.stream
        sys.stderr = self.stream
        for name in self.logger_names:
            l = logging.getLogger(name)
            l.addHandler(self.handler)
            l.setLevel(logging.INFO)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.stream.flush()
        except Exception:
            pass
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        for name in self.logger_names:
            l = logging.getLogger(name)
            l.removeHandler(self.handler)

    def get_text(self) -> str:
        return self.buffer.getvalue()


# 兼容旧名称
SyncLogCapture = LiveSyncLogCapture


class LiveStatusRunner:
    """live_sync_status 上下文执行状态与指标承载器"""
    def __init__(self):
        self.raw_text = ""
        self.duration = 0.0
        self.has_error = False
        self.error_msg = ""

    def get_text(self) -> str:
        return self.raw_text

    def get_duration(self) -> float:
        return self.duration


@contextlib.contextmanager
def live_sync_status(
    running_label: str,
    complete_label: str = "执行完成",
    error_label: str = "执行中断或发生异常",
    expanded_on_complete: bool = False,
    max_display_lines: int = 120,
    logger_names: Optional[List[str]] = None
):
    """
    上下文管理器：在 Streamlit 中提供实时终端进度输出与状态容器
    - 运行期：展示带旋转指示器的状态卡片，实时流式更新原脚本输出的终端与日志信息；
    - 完成期：状态变为对勾完成，自动更新总耗时，优雅收起或折叠；
    - 异常期：捕获异常，状态置为错误，并完整展开错误日志。
    """
    runner = LiveStatusRunner()
    t0 = time.time()

    with st.status(running_label, expanded=True) as status_box:
        log_placeholder = st.empty()
        log_placeholder.caption(T("已就绪，正在监听底层控制台输出与实时进度..."))

        def _on_update(full_text: str):
            cleaned = clean_sync_log(full_text)
            if cleaned:
                lines = cleaned.splitlines()
                if len(lines) > max_display_lines:
                    display_text = (
                        f"... (前文已省略，显示最新 {max_display_lines} 行进度) ...\n"
                        + "\n".join(lines[-max_display_lines:])
                    )
                else:
                    display_text = cleaned
                log_placeholder.code(display_text, language="text")

        with LiveSyncLogCapture(on_update=_on_update, logger_names=logger_names) as log_cap:
            try:
                yield runner
            except Exception as e:
                runner.has_error = True
                runner.error_msg = str(e)
                print(f"[-] 执行中断异常: {e}")
                raise
            finally:
                dur = time.time() - t0
                runner.duration = dur
                runner.raw_text = log_cap.get_text()

                # 最终刷新一次 UI
                _on_update(runner.raw_text)

                # 判断是否有显式错误标识
                is_failed = runner.has_error or (
                    runner.raw_text and ("[-] 错误" in runner.raw_text or "[-] 同步执行异常" in runner.raw_text)
                )

                if is_failed:
                    status_box.update(
                        label=f"{error_label}（耗时 {dur:.2f} 秒）",
                        state="error",
                        expanded=True
                    )
                else:
                    status_box.update(
                        label=f"{complete_label}（耗时 {dur:.2f} 秒）",
                        state="complete",
                        expanded=expanded_on_complete
                    )


# ===================================================================
# 终端日志精简清洗与结构化指标渲染引擎
# ===================================================================

def format_bytes_human(size_bytes: Union[int, float]) -> str:
    """格式化字节大小"""
    try:
        size = float(size_bytes)
    except (ValueError, TypeError):
        return str(size_bytes)
    if size >= 1024 ** 4:
        return f"{size / (1024 ** 4):.2f} TB"
    elif size >= 1024 ** 3:
        return f"{size / (1024 ** 3):.2f} GB"
    elif size >= 1024 ** 2:
        return f"{size / (1024 ** 2):.2f} MB"
    elif size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{int(size)} B"


def clean_sync_log(raw_text: str) -> str:
    """过滤控制台装饰线与冗余噪音"""
    cleaned_lines = []
    for line in raw_text.splitlines():
        line_s = line.strip()
        if not line_s:
            continue
        # 过滤长分隔线
        if re.match(r'^[=\-*#─_]{4,}$', line_s):
            continue
        # 剔除花哨 Emoji 装饰，保持极简清晰
        line_clean = re.sub(r'[\U00010000-\U0010ffff]', '', line_s)
        cleaned_lines.append(line_clean)
    return "\n".join(cleaned_lines)


def render_sync_result(
    raw_text: str,
    default_label: str = "执行完成",
    custom_metrics: Optional[Dict[str, str]] = None
):
    """渲染同步与运维执行结果卡片及日志折叠区域"""
    if not raw_text or not raw_text.strip():
        st.info(T(f"{default_label}，无日志输出。"))
        return

    # 1. 指标卡片展示
    if custom_metrics:
        num_cols = min(len(custom_metrics), 5)
        m_cols = st.columns(num_cols)
        for idx, (lbl, val) in enumerate(custom_metrics.items()):
            m_cols[idx % num_cols].metric(T(lbl), val)

    # 2. 精简日志提要
    cleaned = clean_sync_log(raw_text)
    if cleaned:
        st.code(cleaned, language="text")
    else:
        st.info(T(f"{default_label}，未产生数据变动。"))

    # 3. 完整原始日志
    with st.expander(T("查看底层完整执行日志与调试详情"), expanded=False):
        st.code(raw_text, language="text")


# ===================================================================
# 功能卡片栅格选择器 (复用 maint-card 体系，保证与批量管理界面统一)
# ===================================================================

def _on_sync_card_click(state_key: str, opt_key: str):
    st.session_state[state_key] = opt_key


def render_sync_card_selector(
    options: List[Dict[str, Any]],
    state_key: str,
    cols_per_row: int = 5
) -> str:
    """渲染现代化云端同步功能卡片栅格选择矩阵"""
    if state_key not in st.session_state:
        st.session_state[state_key] = options[0]["key"]
    current_val = st.session_state[state_key]

    all_keys = [o["key"] for o in options]
    if current_val not in all_keys:
        current_val = options[0]["key"]
        st.session_state[state_key] = current_val

    rows = [options[i:i + cols_per_row] for i in range(0, len(options), cols_per_row)]

    for row_idx, row_opts in enumerate(rows):
        cols = st.columns(cols_per_row)
        for col_idx in range(cols_per_row):
            with cols[col_idx]:
                if col_idx < len(row_opts):
                    opt = row_opts[col_idx]
                    is_active = (opt["key"] == current_val)
                    active_cls = "is-selected" if is_active else ""
                    tag_type = opt.get("tag_type", "default")
                    tag_html = (
                        '<span class="maint-card-tag tag-active">● 当前工作区</span>'
                        if is_active
                        else f'<span class="maint-card-tag tag-{tag_type}">{T(opt["tag"])}</span>'
                    )

                    st.markdown(
                        f"""
                        <div class="maint-card-container {active_cls}">
                            <div class="maint-card-header">
                                <span class="maint-card-badge">{opt['badge']}</span>
                                {tag_html}
                            </div>
                            <div class="maint-card-title">{T(opt['title'])}</div>
                            <div class="maint-card-desc">{T(opt['desc'])}</div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
                    st.button(
                        "",
                        key=f"sgrid_{state_key}_{opt['key']}",
                        help=T(f"切换至：{opt['title']}"),
                        use_container_width=True,
                        on_click=_on_sync_card_click,
                        args=(state_key, opt["key"])
                    )
                else:
                    st.empty()

    st.markdown('<div class="analytics-divider" style="margin: 14px 0 20px 0;"></div>', unsafe_allow_html=True)
    return current_val


# ===================================================================
# 1. 面板一：多云用量统计监控 (sync/stats.py)
# ===================================================================

def render_tab_cloud_stats():
    st.markdown(T("#### 跨平台多云存储与数据库用量监控 (stats.py)"))
    st.info(T("实时查询系统连接的 3 大云端基础设施（Supabase、Cloudflare R2、AWS DynamoDB）用量、记录规模及免费配额水位。"))

    c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.2, 1.5], vertical_alignment="bottom")
    with c1:
        chk_supabase = st.checkbox(T("Supabase (PostgreSQL)"), value=True, key="cs_chk_sb")
    with c2:
        chk_r2 = st.checkbox(T("Cloudflare R2 (S3)"), value=True, key="cs_chk_r2")
    with c3:
        chk_dynamodb = st.checkbox(T("AWS DynamoDB (NoSQL)"), value=True, key="cs_chk_ddb")
    with c4:
        r2_year = st.selectbox(
            T("R2 检索年份"),
            ["全部年份", "2026", "2025", "2024", "2023"],
            index=0,
            key="cs_r2_year",
            disabled=not chk_r2
        )

    btn_query = st.button(T("立即查询云端统计报告"), type="primary")

    if btn_query:
        if not (chk_supabase or chk_r2 or chk_dynamodb):
            st.warning(T("请至少勾选一个云平台进行查询。"))
            return

        with live_sync_status(
            running_label=T("正在跨云端检索最新用量数据，这可能需要数秒..."),
            complete_label=T("多云用量查询完成"),
            error_label=T("多云用量查询中断或异常")
        ) as runner:
            import config
            if hasattr(config, "sanitize_dead_proxies"):
                config.sanitize_dead_proxies()

            from sync.stats import query_supabase, query_r2, query_dynamodb

            selected_year = None if r2_year == "全部年份" else r2_year
            if chk_supabase:
                try:
                    query_supabase()
                except Exception as e:
                    logger.error("[-] Supabase 查询异常: %s", e)
            if chk_r2:
                try:
                    query_r2(year=selected_year)
                except Exception as e:
                    logger.error("[-] Cloudflare R2 查询异常: %s", e)
            if chk_dynamodb:
                try:
                    query_dynamodb()
                except Exception as e:
                    logger.error("[-] AWS DynamoDB 查询异常: %s", e)

        raw_text = runner.get_text()
        dur = runner.get_duration()
        metrics = {}

        # 从输出日志中提炼指标
        m_sb_cnt = re.search(r'Supabase.*?总记录数[:：]\s*([\d,]+)', raw_text)
        if m_sb_cnt:
            metrics["Supabase 记录数"] = f"{m_sb_cnt.group(1)} 条"

        m_sb_size = re.search(r'表大小 \(RPC\)\s*[:：]\s*([^\n\r]+)', raw_text) or re.search(r'估算表大小\s*[:：]\s*~([^\n\r(]+)', raw_text)
        if m_sb_size:
            metrics["Supabase 表大小"] = m_sb_size.group(1).strip()

        m_r2_cnt = re.search(r'PDF 文件总数[:：]\s*([\d,]+)', raw_text)
        if m_r2_cnt:
            metrics["R2 文件总数"] = f"{m_r2_cnt.group(1)} 个"

        m_r2_size = re.search(r'PDF 总大小[:：]\s*([^\n\r(]+)', raw_text)
        if m_r2_size:
            metrics["R2 总占用"] = m_r2_size.group(1).strip()

        m_ddb_cnt = re.search(r'近似记录数[:：]\s*([\d,]+)', raw_text) or re.search(r'精确记录数[:：]\s*([\d,]+)', raw_text)
        if m_ddb_cnt:
            metrics["DynamoDB 记录数"] = f"{m_ddb_cnt.group(1)} 条"

        m_ddb_size = re.search(r'DynamoDB.*?表大小[:：]\s*([^\n\r(]+)', raw_text)
        if m_ddb_size:
            metrics["DynamoDB 表大小"] = m_ddb_size.group(1).strip()

        metrics["统计耗时"] = f"{dur:.2f} 秒"

        # 结构化抽取 R2 年份表格
        year_data = []
        for y_m in re.finditer(r'^\s*(\d{4}|Unknown_Year)[:：]\s*([\d,]+)\s*个文件,\s*([^\n\r]+)', raw_text, re.MULTILINE):
            year_data.append({
                "年份目录": y_m.group(1),
                "文件数量": y_m.group(2),
                "存储体积": y_m.group(3).strip()
            })

        # 结构化抽取 Supabase 来源分布表格
        source_data = []
        in_sources = False
        for line in raw_text.splitlines():
            if "按来源分布:" in line:
                in_sources = True
                continue
            if in_sources:
                m_src = re.match(r'^\s*([a-zA-Z0-9_\-]+)[:：]\s*([\d,]+)\s*条', line)
                if m_src:
                    source_data.append({
                        "采集来源 (source)": m_src.group(1),
                        "记录数量": m_src.group(2)
                    })
                else:
                    if line.strip().startswith("[*]") or line.strip().startswith("="):
                        in_sources = False

        render_sync_result(raw_text, default_label="多云用量查询完成", custom_metrics=metrics)

        if year_data:
            st.markdown(T("##### Cloudflare R2 年份分布明细"))
            st.dataframe(pd.DataFrame(year_data), use_container_width=True, hide_index=True)

        if source_data:
            st.markdown(T("##### Supabase 采集站点来源分布排行"))
            st.dataframe(pd.DataFrame(source_data), use_container_width=True, hide_index=True)


# ===================================================================
# 2. 面板二：Supabase 云端同步与归档 (sync/supabase_sync.py)
# ===================================================================

def render_tab_supabase_sync():
    st.markdown(T("#### Supabase (PostgreSQL) 云端数据同步与归档 (supabase_sync.py)"))
    st.info(T("将 Supabase 云端数据库记录无损拉取并合并至本地 SQLite (INSERT OR IGNORE)，支持分批安全清理云端数据以释放 500MB 免费配额。"))

    mode_sync = T("同步云端数据到本地 SQLite")
    mode_clean = T("单独清理云端已同步记录 (释放 500MB 配额)")

    sb_mode = st.radio(
        T("操作模式"),
        [mode_sync, mode_clean],
        horizontal=True,
        key="sb_mode_radio"
    )

    st.markdown("---")

    default_db = get_db_path()

    # ---------------- 模式 1: 同步云端数据到本地 SQLite ----------------
    if sb_mode == mode_sync:
        col_cfg1, col_cfg2 = st.columns([3, 1])
        with col_cfg1:
            target_db = st.text_input(T("本地 SQLite 目标数据库路径"), value=default_db, key="sb_target_db")
        with col_cfg2:
            do_backup = st.checkbox(T("前置自动备份本地库"), value=True, key="sb_do_backup", help=T("同步前自动复制一份带时间戳的 .bak 副本"))

        st.markdown(T("##### 云端已同步数据清理选项 (释放云端存储配额)"))
        clean_cloud = st.checkbox(T("同步成功后清理云端已同步记录"), value=False, key="sb_clean_cloud")
        st.caption(T("提示：如果本次同步未勾选清理，后续可切换上方操作模式为「单独清理云端已同步记录」随时执行安全清理。"))

        confirm_clean = False
        if clean_cloud:
            st.warning(T("警告：勾选此项将在本地成功合并后，从 Supabase 云端分批 DELETE 已同步记录。此操作不可逆，主要用于保持云端数据库在 500MB 免费额度以内。"))
            confirm_clean = st.checkbox(T("我已知晓风险，确认执行云端清理"), value=False, key="sb_confirm_clean")

        btn_sync = st.button(T("开始同步 Supabase 数据"), type="primary")

        if btn_sync:
            if clean_cloud and not confirm_clean:
                st.error(T("已开启清理云端数据，但未勾选确认复选框。操作已拦截。"))
                return

            with live_sync_status(
                running_label=T("正在连接 Supabase 并执行流式同步与合并..."),
                complete_label=T("Supabase 数据同步完成"),
                error_label=T("Supabase 数据同步中断或异常")
            ) as runner:
                from sync.supabase_sync import sync_data
                try:
                    sync_data(
                        db_path=target_db.strip(),
                        do_backup=do_backup,
                        delete_cloud=clean_cloud
                    )
                except Exception as e:
                    print(f"[-] 同步执行异常: {e}")

            raw_text = runner.get_text()
            dur = runner.get_duration()
            metrics = {}

            # 提炼指标
            m_fetch = re.search(r'云端读取总数\s*[:：]\s*(\d+)', raw_text)
            m_added = re.search(r'本地新增合并数\s*[:：]\s*(\d+)', raw_text)
            m_ignored = re.search(r'本地重复忽略数\s*[:：]\s*(\d+)', raw_text)
            m_total = re.search(r'当前本地总记录数\s*[:：]\s*(\d+)', raw_text)

            if m_fetch:
                metrics["云端读取"] = f"{int(m_fetch.group(1)):,} 条"
            if m_added:
                metrics["本地新增"] = f"{int(m_added.group(1)):,} 条"
            if m_ignored:
                metrics["重复忽略"] = f"{int(m_ignored.group(1)):,} 条"
            if m_total:
                metrics["本地总数"] = f"{int(m_total.group(1)):,} 条"
            metrics["总耗时"] = f"{dur:.2f} 秒"

            render_sync_result(raw_text, default_label="Supabase 数据同步完成", custom_metrics=metrics)

    # ---------------- 模式 2: 单独清理云端已同步记录 ----------------
    else:
        st.warning(T("安全保护机制：系统将自动比对云端记录与本地 SQLite 数据库，仅清理本地已确凿存在有效归档的记录（以 url 为准）。若检测到本地尚未同步的新数据，将受严格保护保留。"))

        c1, c2 = st.columns([3, 1])
        with c1:
            clean_db = st.text_input(T("本地 SQLite 比对数据库路径 (用于校验本地副本)"), value=default_db, key="sb_clean_db")
        with c2:
            clean_limit = st.number_input(T("最大清理记录数 (0为不限)"), min_value=0, max_value=1000000, value=0, step=1000, key="sb_clean_limit")

        # 保证互斥联动：初始化与状态回调（不可同时勾选）
        if "sb_clean_dry_run" not in st.session_state:
            st.session_state["sb_clean_dry_run"] = True
        if "sb_clean_force_confirm" not in st.session_state:
            st.session_state["sb_clean_force_confirm"] = False

        # 若历史状态中两者同时为 True，优先纠正为安全预览模式
        if st.session_state.get("sb_clean_dry_run") and st.session_state.get("sb_clean_force_confirm"):
            st.session_state["sb_clean_force_confirm"] = False

        def _on_sb_dry_run_change():
            if st.session_state.get("sb_clean_dry_run"):
                st.session_state["sb_clean_force_confirm"] = False

        def _on_sb_force_confirm_change():
            if st.session_state.get("sb_clean_force_confirm"):
                st.session_state["sb_clean_dry_run"] = False

        c3, c4 = st.columns([1.5, 2.5])
        with c3:
            clean_dry_run = st.checkbox(
                T("仅模拟预览 (dry-run)"),
                key="sb_clean_dry_run",
                on_change=_on_sb_dry_run_change,
                help=T("只扫描比对云端与本地记录，统计待清理与保留项，不执行实际删除")
            )
        with c4:
            clean_force_confirm = st.checkbox(
                T("我已知晓风险，确认从 Supabase 删除已在本地归档的数据"),
                key="sb_clean_force_confirm",
                on_change=_on_sb_force_confirm_change
            )

        btn_clean = st.button(T("开始执行云端已同步数据清理"), type="primary")

        if btn_clean:
            if not clean_dry_run and not clean_force_confirm:
                st.error(T("未开启模拟预览 (dry-run) 时，必须勾选风险确认复选框方可执行实际删除。操作已拦截。"))
                return

            with live_sync_status(
                running_label=T("正在比对云端与本地记录并执行安全清理..."),
                complete_label=T("Supabase 云端清理完成"),
                error_label=T("Supabase 云端清理中断或异常")
            ) as runner:
                from sync.supabase_sync import delete_synced_data
                try:
                    delete_synced_data(
                        db_path=clean_db.strip(),
                        dry_run=clean_dry_run,
                        limit=int(clean_limit)
                    )
                except Exception as e:
                    print(f"[-] 清理执行异常: {e}")

            raw_text = runner.get_text()
            dur = runner.get_duration()
            metrics = {}

            m_scan = re.search(r'云端扫描总数\s*[:：]\s*([\d,]+)', raw_text)
            m_ver = re.search(r'本地已归档\(可清理\)\s*[:：]\s*([\d,]+)', raw_text)
            m_unm = re.search(r'本地未归档\(受保护\)\s*[:：]\s*([\d,]+)', raw_text)
            m_del = re.search(r'实际成功删除\s*[:：]\s*([\d,]+)', raw_text)

            if m_scan:
                metrics["云端扫描"] = f"{m_scan.group(1)} 条"
            if m_ver:
                metrics["已归档项"] = f"{m_ver.group(1)} 条"
            if m_unm:
                metrics["受保护项"] = f"{m_unm.group(1)} 条"
            if m_del:
                metrics["成功删除"] = f"{m_del.group(1)} 条"
            metrics["总耗时"] = f"{dur:.2f} 秒"

            default_lbl = "Supabase 云端清理预览完成" if clean_dry_run else "Supabase 云端已同步数据清理完成"
            render_sync_result(raw_text, default_label=default_lbl, custom_metrics=metrics)



# ===================================================================
# 3. 面板三：Cloudflare R2 对象存储 PDF 管理 (sync/r2_sync.py)
# ===================================================================

def render_tab_r2_sync():
    st.markdown(T("#### Cloudflare R2 对象存储 PDF 管理与生命周期 (r2_sync.py)"))
    st.info(T("Cloudflare R2 对象存储与本地磁盘之间 PDF 的并发下载、断点续传与本地副本智能安全比对清理。"))

    r2_mode = st.radio(
        T("操作模式"),
        [T("并发下载 PDF 到本地"), T("清理 R2 云端文件 (释放 10GB 配额)")],
        horizontal=True,
        key="r2_mode_radio"
    )

    st.markdown("---")

    # ---------------- 模式 1: 并发下载 PDF ----------------
    if "并发下载" in r2_mode:
        c1, c2, c3 = st.columns([1.5, 1, 1])
        with c1:
            dl_year = st.selectbox(T("同步年份"), ["全部年份", "2026", "2025", "2024", "2023"], index=0, key="r2_dl_year")
        with c2:
            dl_workers = st.slider(T("并发下载线程数"), min_value=5, max_value=50, value=30, step=5, key="r2_dl_workers")
        with c3:
            dl_max = st.number_input(T("最大下载数量 (0为不限)"), min_value=0, max_value=100000, value=0, step=100, key="r2_dl_max")

        c4, c5 = st.columns([3, 1], vertical_alignment="bottom")
        with c4:
            default_out = os.path.join(PROJECT_ROOT, "pdf")
            dl_output = st.text_input(T("本地保存路径"), value=default_out, key="r2_dl_output")
        with c5:
            dl_resume = st.checkbox(T("启用断点续传"), value=True, key="r2_dl_resume", help=T("比对本地已有文件大小，完全一致则自动跳过"))

        dl_delete_after = st.checkbox(T("下载完成后安全清理云端已有本地副本的文件"), value=False, key="r2_dl_del_after")

        btn_dl = st.button(T("开始执行 R2 并发下载"), type="primary")

        if btn_dl:
            with live_sync_status(
                running_label=T("正在连接 Cloudflare R2 并检索文件清单..."),
                complete_label=T("R2 PDF 同步下载完成"),
                error_label=T("R2 PDF 同步下载中断或异常")
            ) as runner:
                import importlib
                import sync.r2_sync
                importlib.reload(sync.r2_sync)
                from sync.r2_sync import get_r2_client, list_all_pdfs, download_all_pdfs, list_all_objects, run_delete_flow
                
                selected_year = None if dl_year == "全部年份" else dl_year
                prefix = f"pdf/{selected_year}/" if selected_year else "pdf/"
                max_keys = None if dl_max == 0 else int(dl_max)
                out_dir = dl_output.strip()

                try:
                    client = get_r2_client()
                    print(f"[*] 正在检索 R2 文件清单 (前缀: {prefix})...")
                    pdfs = list_all_pdfs(client, prefix=prefix, max_keys=max_keys) or []
                    print(f"[+] 检索完成，共找到 {len(pdfs)} 个 PDF 文件")

                    if pdfs:
                        download_all_pdfs(out_dir, pdfs, resume=dl_resume, workers=dl_workers)
                    else:
                        print(f"[*] 未在 R2 中检索到符合前缀 '{prefix}' 的 PDF 文件")

                    if dl_delete_after and pdfs:
                        print("\n[*] 正在执行下载后联动删除流程...")
                        args_mock = argparse.Namespace(dry_run=False, delete_force=False)
                        run_delete_flow(client, out_dir, pdfs, prefix, args_mock, show_details=False)

                except Exception as e:
                    print(f"[-] 下载过程中断异常: {e}")

            raw_text = runner.get_text()
            dur = runner.get_duration()
            metrics = {}

            m_succ = re.search(r'成功\s*[:：]\s*(\d+)', raw_text)
            m_skip = re.search(r'跳过\s*[:：]\s*(\d+)', raw_text)
            m_fail = re.search(r'失败\s*[:：]\s*(\d+)', raw_text)
            m_sz = re.search(r'总大小\s*[:：]\s*([^\n\r]+)', raw_text)

            if m_succ:
                metrics["下载成功"] = f"{int(m_succ.group(1)):,} 个"
            if m_skip:
                metrics["断点跳过"] = f"{int(m_skip.group(1)):,} 个"
            if m_fail:
                metrics["下载失败"] = f"{int(m_fail.group(1)):,} 个"
            if m_sz:
                metrics["传输体积"] = m_sz.group(1).strip()
            metrics["总耗时"] = f"{dur:.2f} 秒"

            render_sync_result(raw_text, default_label="R2 PDF 同步下载完成", custom_metrics=metrics)

    # ---------------- 模式 2: 清理 R2 云端文件 ----------------
    else:
        st.warning(T("安全保护机制：系统会自动比对本地磁盘目录，仅自动清理本地已有有效副本的文件。若检测到本地无副本的文件将强制保护或要求人工二次确认。"))

        c1, c2 = st.columns([2, 2])
        with c1:
            del_prefix = st.text_input(T("待清理的 R2 前缀 (如 pdf/2024/ 或 pdf/)"), value="pdf/", key="r2_del_prefix")
        with c2:
            local_check_dir = st.text_input(T("本地比对目录 (用于检测是否存在副本)"), value=os.path.join(PROJECT_ROOT, "pdf"), key="r2_local_check_dir")

        # 保证互斥联动：初始化与状态回调（勾选一个自动取消另一个）
        if "r2_del_dry_run" not in st.session_state:
            st.session_state["r2_del_dry_run"] = True
        if "r2_del_force" not in st.session_state:
            st.session_state["r2_del_force"] = False

        # 若历史状态中两者同时为 True，优先纠正为安全预览模式
        if st.session_state.get("r2_del_dry_run") and st.session_state.get("r2_del_force"):
            st.session_state["r2_del_force"] = False

        def _on_r2_del_dry_run_change():
            if st.session_state.get("r2_del_dry_run"):
                st.session_state["r2_del_force"] = False

        def _on_r2_del_force_change():
            if st.session_state.get("r2_del_force"):
                st.session_state["r2_del_dry_run"] = False

        c3, c4 = st.columns([1.5, 2.5])
        with c3:
            del_dry_run = st.checkbox(
                T("仅模拟预览 (dry-run)"),
                key="r2_del_dry_run",
                on_change=_on_r2_del_dry_run_change,
                help=T("只扫描并列出将删除的文件与大小，不执行真实删除")
            )
        with c4:
            del_force = st.checkbox(
                T("删除本地无副本的孤立文件 (谨慎)"),
                key="r2_del_force",
                on_change=_on_r2_del_force_change,
                help=T("勾选将自动切换为正式清理（自动取消模拟预览），并连同本地无备份的云端孤立文件一并清理；未勾选时将强制保护本地无副本的文件。（与「仅模拟预览」互斥联动）")
            )

        btn_del = st.button(T("执行 R2 云端文件清理"), type="primary")

        if btn_del:
            with live_sync_status(
                running_label=T("正在扫描 R2 对象并比对本地物理文件..."),
                complete_label=T("R2 文件清理完成"),
                error_label=T("R2 文件清理中断或异常")
            ) as runner:
                import importlib
                import sync.r2_sync
                importlib.reload(sync.r2_sync)
                from sync.r2_sync import get_r2_client, list_all_objects, run_delete_flow

                try:
                    client = get_r2_client()
                    p = del_prefix.strip()
                    print(f"[*] 正在扫描前缀 '{p}' 下的全部对象...")
                    cands = list_all_objects(client, prefix=p)
                    print(f"[+] 找到 {len(cands)} 个候选对象")

                    if cands:
                        args_mock = argparse.Namespace(dry_run=del_dry_run, delete_force=del_force)
                        run_delete_flow(
                            client,
                            output_dir=local_check_dir.strip(),
                            delete_candidates=cands,
                            del_prefix=p,
                            args=args_mock,
                            show_details=False,
                            extra_search_dirs=[os.path.join(PROJECT_ROOT, "pdf")]
                        )
                except Exception as e:
                    print(f"[-] 清理执行异常: {e}")

            raw_text = runner.get_text()
            dur = runner.get_duration()
            metrics = {}

            m_tot = re.search(r'找到\s*(\d+)\s*个文件', raw_text)
            m_ok = re.search(r'成功\s*[:：]\s*(\d+)', raw_text)
            m_fail = re.search(r'失败\s*[:：]\s*(\d+)', raw_text)

            if m_tot:
                metrics["候选对象"] = f"{int(m_tot.group(1)):,} 个"
            if m_ok:
                metrics["成功删除"] = f"{int(m_ok.group(1)):,} 个"
            if m_fail:
                metrics["删除失败"] = f"{int(m_fail.group(1)):,} 个"
            metrics["总耗时"] = f"{dur:.2f} 秒"

            render_sync_result(raw_text, default_label="R2 文件清理完成", custom_metrics=metrics)


# ===================================================================
# 4. 面板四：AWS DynamoDB 去重与数据同步 (sync/dynamodb_sync.py)
# ===================================================================

def render_tab_dynamodb_sync():
    st.markdown(T("#### AWS DynamoDB 云端去重与增量数据同步 (dynamodb_sync.py)"))
    st.info(T("负责本地爬虫数据与 AWS DynamoDB (fuli_resources 表) 之间的增量同步、跨域名规范化去重键写入与本地 Bloom Filter 缓存持久化。"))

    ddb_action = st.radio(
        T("同步子功能"),
        [
            T("增量比对上传 (upload - 上传云端缺失的 URL 与磁力)"),
            T("规范化去重键与 Bloom Filter 同步 (sync-keys - 跨域名去重)")
        ],
        key="ddb_action_radio"
    )

    st.markdown("---")

    default_db = get_db_path()

    # ---------------- 功能 1: 增量比对上传 ----------------
    if "upload" in ddb_action:
        st.markdown(T("##### 增量比对上传 (upload)"))
        st.info(T("全量扫描 DynamoDB 已有 URL 集合，与本地 SQLite 进行差集比对，精准定位并批量上传缺失的 URL 与磁力链接。"))

        target_db = st.text_input(T("本地 SQLite 数据库路径"), value=default_db, key="ddb_up_db")
        btn_up = st.button(T("开始增量比对并上传"), type="primary")

        if btn_up:
            with live_sync_status(
                running_label=T("正在扫描云端 DynamoDB 与本地数据表进行差集比对..."),
                complete_label=T("DynamoDB 增量上传完成"),
                error_label=T("DynamoDB 增量上传中断或异常")
            ) as runner:
                from sync.dynamodb_sync import run_upload
                try:
                    args_mock = argparse.Namespace(db=target_db.strip())
                    run_upload(args_mock)
                except Exception as e:
                    print(f"[-] 上传异常: {e}")

            raw_text = runner.get_text()
            dur = runner.get_duration()
            metrics = {}

            m_loc = re.search(r'本地共有\s*([\d,]+)\s*条有效 URL', raw_text)
            m_cld = re.search(r'云端现有\s*([\d,]+)\s*条记录', raw_text)
            m_mis = re.search(r'云端缺失的记录\s*[:：]\s*([\d,]+)\s*条', raw_text)
            m_ins = re.search(r'成功写入\s*[:：]\s*([\d,]+)\s*条', raw_text)

            if m_loc:
                metrics["本地有效记录"] = f"{m_loc.group(1)} 条"
            if m_cld:
                metrics["云端已有记录"] = f"{m_cld.group(1)} 条"
            if m_mis:
                metrics["待增量上传"] = f"{m_mis.group(1)} 条"
            if m_ins:
                metrics["成功写入"] = f"{m_ins.group(1)} 条"
            metrics["总耗时"] = f"{dur:.2f} 秒"

            render_sync_result(raw_text, default_label="DynamoDB 增量上传完成", custom_metrics=metrics)

    # ---------------- 功能 2: 规范化去重键与 Bloom Filter 同步 ----------------
    else:
        st.markdown(T("##### 规范化相对路径键同步与 Bloom Filter (sync-keys)"))
        st.info(T("将绝对 URL 转换为站点无关的规范化相对路径去重键 (如 /article/12345.html)，批量同步写入 DynamoDB 并生成持久化本地 Bloom Filter 二进制缓存。"))

        c1, c2 = st.columns([2, 1])
        with c1:
            target_db = st.text_input(T("本地 SQLite 数据库路径"), value=default_db, key="ddb_sk_db")
        with c2:
            sk_qps = st.number_input(T("写入速率限速 (QPS)"), min_value=0.0, max_value=200.0, value=24.0, step=1.0, key="ddb_sk_qps", help=T("24 QPS 适配 AWS Free Tier 25 WCU 免费限额；0 为无限制极速并发"))

        c3, c4, c5 = st.columns([1.5, 1, 1])
        with c3:
            sk_sources = st.text_input(T("站点来源过滤 (空格分隔，留空处理全部)"), value="", key="ddb_sk_sources", placeholder="如: datang jingpin taose")
        with c4:
            sk_workers = st.slider(T("并发线程数"), min_value=1, max_value=30, value=10, step=1, key="ddb_sk_workers")
        with c5:
            sk_limit = st.number_input(T("限制处理条数 (0为不限)"), min_value=0, max_value=1000000, value=0, step=1000, key="ddb_sk_limit")

        btn_sk = st.button(T("开始同步去重键与 Bloom Filter"), type="primary")

        if btn_sk:
            with live_sync_status(
                running_label=T("正在生成规范化相对路径键并写入 DynamoDB / Bloom Filter..."),
                complete_label=T("去重键与 Bloom Filter 同步完成"),
                error_label=T("去重键与 Bloom Filter 同步中断或异常")
            ) as runner:
                from sync.dynamodb_sync import run_sync_keys

                src_list = sk_sources.strip().split() if sk_sources.strip() else None
                limit_val = int(sk_limit) if sk_limit > 0 else None
                qps_val = float(sk_qps) if sk_qps > 0 else None

                try:
                    args_mock = argparse.Namespace(
                        db=target_db.strip(),
                        batch_size=25,
                        workers=sk_workers,
                        limit=limit_val,
                        sources=src_list,
                        qps=qps_val
                    )
                    run_sync_keys(args_mock)
                except Exception as e:
                    print(f"[-] 同步去重键异常: {e}")

            raw_text = runner.get_text()
            dur = runner.get_duration()
            metrics = {}

            m_ext = re.search(r'共生成\s*([\d,]+)\s*个独立的相对路径去重键', raw_text)
            m_suc = re.search(r'成功写入\s*([\d,]+)\s*个规范化相对路径键', raw_text)

            if m_ext:
                metrics["生成独立去重键"] = f"{m_ext.group(1)} 个"
            if m_suc:
                metrics["成功同步写入"] = f"{m_suc.group(1)} 个"
            metrics["总耗时"] = f"{dur:.2f} 秒"

            render_sync_result(raw_text, default_label="去重键与 Bloom Filter 同步完成", custom_metrics=metrics)


# ===================================================================
# 5. 面板五：本地轻量纯链接独立库导出 (sync/export_urls_magnets.py)
# ===================================================================

def render_tab_export_links():
    st.markdown(T("#### 本地 URL 与磁力链接轻量独立库导出 (export_urls_magnets.py)"))
    st.info(T("从本地主数据库 (resources 表) 中抽取全部有效 url 与 resource_link 磁力链接，生成体积小巧、建有索引的独立轻量 SQLite 数据库，便于分发或外部工具对接。"))

    default_src = get_db_path()
    default_dst = r"D:\urls_only.db"

    c1, c2 = st.columns([1, 1])
    with c1:
        src_path = st.text_input(T("源 SQLite 主数据库路径"), value=default_src, key="exp_src_db")
    with c2:
        dst_path = st.text_input(T("导出目标轻量库文件路径"), value=default_dst, key="exp_dst_db")

    btn_exp = st.button(T("立即导出轻量独立库"), type="primary")

    if btn_exp:
        s_p = src_path.strip()
        d_p = dst_path.strip()

        if not os.path.exists(s_p):
            st.error(T(f"源主数据库文件不存在: {s_p}"))
            return

        total_urls = 0
        total_mags = 0
        with live_sync_status(
            running_label=T("正在抽取 URL 与磁力链接并构建独立轻量数据库..."),
            complete_label=T("轻量独立库导出完成"),
            error_label=T("轻量独立库导出中断或异常")
        ) as runner:
            try:
                # 动态适配 export_urls 函数支持自定义路径
                if os.path.exists(d_p):
                    try:
                        os.remove(d_p)
                        print(f"[*] 已清理旧的导出数据库文件: {d_p}")
                    except Exception as e:
                        print(f"[!] 清理旧文件失败: {e}")

                src_conn = sqlite3.connect(s_p)
                src_cur = src_conn.cursor()

                # 读取 URL
                src_cur.execute("SELECT url FROM resources WHERE url IS NOT NULL AND url != ''")
                url_rows = src_cur.fetchall()
                total_urls = len(url_rows)
                print(f"[+] 读取到 {total_urls} 条有效 url 记录")

                # 创建目标库
                target_parent = os.path.dirname(os.path.abspath(d_p))
                if target_parent:
                    os.makedirs(target_parent, exist_ok=True)
                dst_conn = sqlite3.connect(d_p)
                dst_cur = dst_conn.cursor()

                dst_cur.execute("""
                    CREATE TABLE IF NOT EXISTS urls (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        exported_at TEXT NOT NULL
                    )
                """)
                dst_cur.execute("CREATE INDEX IF NOT EXISTS idx_url ON urls(url)")

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                batch_sz = 1000
                for i in range(0, total_urls, batch_sz):
                    batch = url_rows[i:i + batch_sz]
                    dst_cur.executemany("INSERT INTO urls (url, exported_at) VALUES (?, ?)", [(r[0], now_str) for r in batch])
                    if i % 5000 == 0 or i + batch_sz >= total_urls:
                        print(f"[*] 写入 URL 进度: {min(i + batch_sz, total_urls)}/{total_urls} 条...")
                dst_conn.commit()

                # 读取磁力
                src_cur.execute("SELECT resource_link FROM resources WHERE resource_link IS NOT NULL AND resource_link != ''")
                mag_rows = src_cur.fetchall()
                total_mags = len(mag_rows)
                print(f"[+] 读取到 {total_mags} 条有效 resource_link (磁力链接) 记录")

                dst_cur.execute("""
                    CREATE TABLE IF NOT EXISTS magnets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        resource_link TEXT NOT NULL,
                        exported_at TEXT NOT NULL
                    )
                """)
                dst_cur.execute("CREATE INDEX IF NOT EXISTS idx_resource_link ON magnets(resource_link)")

                for i in range(0, total_mags, batch_sz):
                    batch = mag_rows[i:i + batch_sz]
                    dst_cur.executemany("INSERT INTO magnets (resource_link, exported_at) VALUES (?, ?)", [(r[0], now_str) for r in batch])
                    if i % 5000 == 0 or i + batch_sz >= total_mags:
                        print(f"[*] 写入磁力链接进度: {min(i + batch_sz, total_mags)}/{total_mags} 条...")
                dst_conn.commit()

                dst_conn.close()
                src_conn.close()

                out_size = os.path.getsize(d_p) if os.path.exists(d_p) else 0
                print(f"[✓] 导出成功！目标数据库: {d_p} (体积: {format_bytes_human(out_size)})")
                print(f"[✓] 共导出 {total_urls} 条 url 记录与 {total_mags} 条磁力链接记录")

            except Exception as e:
                print(f"[-] 导出异常: {e}")

        raw_text = runner.get_text()
        dur = runner.get_duration()
        metrics = {}
        metrics["导出 URL 记录"] = f"{total_urls:,} 条"
        metrics["导出磁力链接"] = f"{total_mags:,} 条"
        if os.path.exists(d_p):
            metrics["生成库体积"] = format_bytes_human(os.path.getsize(d_p))
        metrics["总耗时"] = f"{dur:.2f} 秒"

        render_sync_result(raw_text, default_label="轻量独立库导出完成", custom_metrics=metrics)


# ===================================================================
# 云端同步中心主入口函数
# ===================================================================

def render_sync_hub():
    """在 Streamlit 中渲染完整的 5 大云端同步与迁移面板"""
    sync_options = [
        {
            "key": "cloud-stats",
            "badge": "01",
            "title": "多云用量监控",
            "desc": "实时汇总 Supabase / R2 / DynamoDB 存储与记录规模及免费配额水位",
            "tag": "用量监控",
            "tag_type": "default"
        },
        {
            "key": "supabase-sync",
            "badge": "02",
            "title": "Supabase 归档",
            "desc": "云端记录流式拉取、本地无损幂等合并与云端历史数据安全清理",
            "tag": "数据同步",
            "tag_type": "default"
        },
        {
            "key": "r2-sync",
            "badge": "03",
            "title": "R2 PDF 管理",
            "desc": "Cloudflare R2 对象并发下载、断点续传与本地副本智能安全比对清理",
            "tag": "对象存储",
            "tag_type": "warn"
        },
        {
            "key": "dynamodb-sync",
            "badge": "04",
            "title": "DynamoDB 去重",
            "desc": "增量比对上传缺失项、跨域名规范化去重键与本地 Bloom Filter 同步",
            "tag": "去重缓存",
            "tag_type": "sys"
        },
        {
            "key": "export-links",
            "badge": "05",
            "title": "纯链接独立导出",
            "desc": "从主库提取有效 URL 与磁力链接，导出为体积小巧且带索引的独立库",
            "tag": "轻量导出",
            "tag_type": "default"
        },
    ]

    choice = render_sync_card_selector(sync_options, "sync_hub_subtool", cols_per_row=5)

    if choice == "cloud-stats":
        render_tab_cloud_stats()
    elif choice == "supabase-sync":
        render_tab_supabase_sync()
    elif choice == "r2-sync":
        render_tab_r2_sync()
    elif choice == "dynamodb-sync":
        render_tab_dynamodb_sync()
    elif choice == "export-links":
        render_tab_export_links()
