import os
import sys
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

# 导入项目配置
from config import PROJECT_ROOT, PDF_BASE_DIR, get_db_path
from utils.pdf_utils import parse_filename
from utils.fanhao_filter import extract_fanhao
from utils.resource_link_cleaner import clean_resource_link
from utils.ui_compact import T
from viewer_maintenance import render_maintenance_hub


# 设置页面配置（收起侧边栏以最大化内容区域）
st.set_page_config(
    page_title="资源预览器",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 自定义 CSS 样式提升视觉体验
st.markdown("""
<style>
    /* ==================== 全局主题与高能加载/计算状态视觉规范 ==================== */
    
    /* 默认 (深色主题) CSS 变量 */
    :root {
        --loading-mask-bg: rgba(10, 15, 29, 0.45);
        --loading-mask-filter: blur(2.5px) brightness(0.8);
        --loading-hud-bg: rgba(15, 23, 42, 0.96);
        --loading-hud-border: #38bdf8;
        --loading-hud-shadow: 0 20px 60px rgba(0, 0, 0, 0.9), 0 0 35px rgba(56, 189, 248, 0.5);
        --loading-hud-text: #e2e8f0;
        --loading-spinner-track: rgba(56, 189, 248, 0.25);
        --loading-spinner-top: #00e5ff;
        --loading-spinner-right: #6366f1;
        --loading-glow-gradient: linear-gradient(90deg, #00e5ff, #2979ff, #7c4dff, #00e676, #00e5ff);
        --loading-glow-shadow: 0 0 14px rgba(41, 121, 255, 0.9), 0 0 25px rgba(0, 229, 255, 0.7);
    }

    /* 浅色主题 CSS 变量覆盖 */
    :root[data-theme="light"],
    html[data-theme="light"],
    body[data-theme="light"],
    body.theme-light,
    .stApp[data-theme="light"],
    .theme-light {
        --loading-mask-bg: rgba(241, 245, 249, 0.6);
        --loading-mask-filter: blur(2.5px) brightness(1.02);
        --loading-hud-bg: rgba(255, 255, 255, 0.98);
        --loading-hud-border: #0284c7;
        --loading-hud-shadow: 0 20px 60px rgba(0, 0, 0, 0.15), 0 0 30px rgba(2, 132, 199, 0.25);
        --loading-hud-text: #0f172a;
        --loading-spinner-track: rgba(2, 132, 199, 0.2);
        --loading-spinner-top: #0284c7;
        --loading-spinner-right: #38bdf8;
        --loading-glow-gradient: linear-gradient(90deg, #0284c7, #38bdf8, #6366f1, #10b981, #0284c7);
        --loading-glow-shadow: 0 0 14px rgba(2, 132, 199, 0.8), 0 0 22px rgba(56, 189, 248, 0.6);
    }

    /* 顶部流动霓虹光带扫描动画 */
    @keyframes topGlowFlow {
        0% { background-position: 0% 50%; }
        50% { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
    }

    /* 中央悬浮 HUD 深色模式呼吸光晕动画 */
    @keyframes hudPulseGlowDark {
        0% {
            box-shadow: 0 16px 45px rgba(0, 0, 0, 0.7), 0 0 20px rgba(56, 189, 248, 0.25);
            border-color: rgba(56, 189, 248, 0.45);
        }
        50% {
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.8), 0 0 35px rgba(56, 189, 248, 0.6);
            border-color: rgba(56, 189, 248, 0.9);
        }
        100% {
            box-shadow: 0 16px 45px rgba(0, 0, 0, 0.7), 0 0 20px rgba(56, 189, 248, 0.25);
            border-color: rgba(56, 189, 248, 0.45);
        }
    }

    /* 中央悬浮 HUD 浅色模式呼吸光晕动画 */
    @keyframes hudPulseGlowLight {
        0% {
            box-shadow: 0 12px 35px rgba(0, 0, 0, 0.08), 0 0 15px rgba(2, 132, 199, 0.15);
            border-color: rgba(2, 132, 199, 0.4);
        }
        50% {
            box-shadow: 0 18px 45px rgba(0, 0, 0, 0.14), 0 0 25px rgba(2, 132, 199, 0.35);
            border-color: rgba(2, 132, 199, 0.85);
        }
        100% {
            box-shadow: 0 12px 35px rgba(0, 0, 0, 0.08), 0 0 15px rgba(2, 132, 199, 0.15);
            border-color: rgba(2, 132, 199, 0.4);
        }
    }

    /* 环形旋转加载动画 */
    @keyframes spinLoadingRing {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }

    /* 悬浮弹窗平滑浮现 */
    @keyframes hudModalFadeIn {
        0% {
            opacity: 0;
            transform: translate(-50%, -44%) scale(0.95);
        }
        100% {
            opacity: 1;
            transform: translate(-50%, -50%) scale(1);
        }
    }

    /* 彻底消除 Streamlit 默认对主容器粗暴降透明度导致的灰屏发暗问题 */
    .stApp[data-test-script-state="running"] > .stAppViewContainer,
    .stApp[data-test-script-state="running"] [data-testid="stMain"],
    .stApp[data-test-script-state="running"] .stMain,
    .stApp[data-test-script-state="running"] [data-testid="stAppViewBlockContainer"],
    .stApp[data-test-script-state="running"] [data-testid="stMainBlockContainer"],
    .stApp[data-test-script-state="running"] .stMainBlockContainer {
        opacity: 1 !important;
        filter: none !important;
    }

    /* 运行状态：全屏背景磨砂遮罩（独立层仅遮罩底层内容，绝不影响最上层 HUD，自动适配明暗主题） */
    .stApp[data-test-script-state="running"]::after {
        content: "" !important;
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        width: 100vw !important;
        height: 100vh !important;
        background: var(--loading-mask-bg, rgba(10, 15, 29, 0.45)) !important;
        backdrop-filter: var(--loading-mask-filter, blur(2.5px) brightness(0.8)) !important;
        -webkit-backdrop-filter: var(--loading-mask-filter, blur(2.5px) brightness(0.8)) !important;
        z-index: 999990 !important;
        pointer-events: none !important;
    }

    /* 运行状态：顶部高能流动渐变光带（纯 CSS 零延迟兜底，自适应主题色） */
    .stApp[data-test-script-state="running"]::before {
        content: "" !important;
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        width: 100vw !important;
        height: 3.5px !important;
        background: var(--loading-glow-gradient, linear-gradient(90deg, #00e5ff, #2979ff, #7c4dff, #00e676, #00e5ff)) !important;
        background-size: 300% 100% !important;
        animation: topGlowFlow 1.8s infinite linear !important;
        box-shadow: var(--loading-glow-shadow, 0 0 14px rgba(41, 121, 255, 0.9), 0 0 25px rgba(0, 229, 255, 0.7)) !important;
        z-index: 9999999 !important;
        pointer-events: none !important;
    }

    /* 悬浮 HUD 加载器容器样式（最高层级，直接挂载在 body 上，自适应主题） */
    #app-global-loading-hud {
        position: fixed !important;
        top: 40% !important;
        left: 50% !important;
        transform: translate(-50%, -50%) !important;
        z-index: 99999999 !important;
        display: flex !important;
        align-items: center !important;
        gap: 12px !important;
        background: var(--loading-hud-bg, rgba(15, 23, 42, 0.96)) !important;
        backdrop-filter: blur(20px) !important;
        -webkit-backdrop-filter: blur(20px) !important;
        border: 1.5px solid var(--loading-hud-border, #38bdf8) !important;
        border-radius: 40px !important;
        padding: 10px 22px !important;
        box-shadow: var(--loading-hud-shadow, 0 20px 60px rgba(0, 0, 0, 0.9), 0 0 35px rgba(56, 189, 248, 0.5)) !important;
        pointer-events: none !important;
        user-select: none !important;
        opacity: 0 !important;
        visibility: hidden !important;
        transition: opacity 0.18s cubic-bezier(0.16, 1, 0.3, 1), visibility 0.18s, transform 0.18s, background-color 0.25s, border-color 0.25s, box-shadow 0.25s !important;
    }

    /* 当处于运行/计算状态时，中央悬浮 HUD 呈现（默认深色光晕） */
    .stApp[data-test-script-state="running"] #app-global-loading-hud,
    #app-global-loading-hud.is-active {
        opacity: 1 !important;
        visibility: visible !important;
        animation: hudModalFadeIn 0.24s cubic-bezier(0.16, 1, 0.3, 1) forwards, hudPulseGlowDark 2.2s infinite ease-in-out !important;
    }

    /* 浅色主题激活状态下的 HUD 光晕动画与显式覆盖 */
    html[data-theme="light"] #app-global-loading-hud.is-active,
    body[data-theme="light"] #app-global-loading-hud.is-active,
    body.theme-light #app-global-loading-hud.is-active,
    #app-global-loading-hud.hud-theme-light.is-active,
    #app-global-loading-hud[data-theme="light"].is-active,
    .stApp[data-theme="light"][data-test-script-state="running"] #app-global-loading-hud,
    html[data-theme="light"] .stApp[data-test-script-state="running"] #app-global-loading-hud,
    body.theme-light .stApp[data-test-script-state="running"] #app-global-loading-hud {
        animation: hudModalFadeIn 0.24s cubic-bezier(0.16, 1, 0.3, 1) forwards, hudPulseGlowLight 2.2s infinite ease-in-out !important;
    }

    /* 浅色主题下的 HUD 显式 CSS 属性兜底 */
    body.theme-light #app-global-loading-hud,
    body[data-theme="light"] #app-global-loading-hud,
    html[data-theme="light"] #app-global-loading-hud,
    #app-global-loading-hud.hud-theme-light,
    #app-global-loading-hud[data-theme="light"] {
        background: rgba(255, 255, 255, 0.98) !important;
        border: 1.5px solid #0284c7 !important;
        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.15), 0 0 30px rgba(2, 132, 199, 0.25) !important;
    }
    body.theme-light #app-global-loading-hud .hud-spinner-ring,
    body[data-theme="light"] #app-global-loading-hud .hud-spinner-ring,
    html[data-theme="light"] #app-global-loading-hud .hud-spinner-ring,
    #app-global-loading-hud.hud-theme-light .hud-spinner-ring,
    #app-global-loading-hud[data-theme="light"] .hud-spinner-ring {
        border: 2.5px solid rgba(2, 132, 199, 0.2) !important;
        border-top-color: #0284c7 !important;
        border-right-color: #38bdf8 !important;
    }
    body.theme-light #app-global-loading-hud .hud-sub-text,
    body[data-theme="light"] #app-global-loading-hud .hud-sub-text,
    html[data-theme="light"] #app-global-loading-hud .hud-sub-text,
    #app-global-loading-hud.hud-theme-light .hud-sub-text,
    #app-global-loading-hud[data-theme="light"] .hud-sub-text {
        color: #0f172a !important;
        font-weight: 600 !important;
    }

    /* 旋转环形 Spinner 图标（自适应主题色） */
    .hud-spinner-ring {
        width: 20px !important;
        height: 20px !important;
        flex-shrink: 0 !important;
        border: 2.5px solid var(--loading-spinner-track, rgba(56, 189, 248, 0.25)) !important;
        border-top-color: var(--loading-spinner-top, #00e5ff) !important;
        border-right-color: var(--loading-spinner-right, #6366f1) !important;
        border-radius: 50% !important;
        animation: spinLoadingRing 0.75s linear infinite !important;
    }

    /* HUD 文字区域 */
    .hud-text-box {
        display: flex !important;
        align-items: center !important;
    }
    .hud-sub-text {
        font-size: 13px !important;
        font-weight: 600 !important;
        color: var(--loading-hud-text, #e2e8f0) !important;
        letter-spacing: 0.3px !important;
        white-space: nowrap !important;
        margin: 0 !important;
        line-height: normal !important;
    }

    /* 原生顶部状态微件 (stStatusWidget) 强化高亮 */
    [data-testid="stStatusWidget"] {
        visibility: visible !important;
        display: flex !important;
        background: rgba(33, 150, 243, 0.15) !important;
        border: 1px solid rgba(33, 150, 243, 0.4) !important;
        border-radius: 20px !important;
        padding: 2px 8px !important;
        transition: all 0.2s ease !important;
    }
    .stApp[data-test-script-state="running"] [data-testid="stStatusWidget"] {
        background: rgba(33, 150, 243, 0.35) !important;
        border-color: #42a5f5 !important;
        box-shadow: 0 0 14px rgba(33, 150, 243, 0.65) !important;
    }

    /* 优化全局间距与顶部留白：彻底覆盖 Streamlit 所有主内容容器选择器 */
    .block-container,
    .stMainBlockContainer,
    div[data-testid="stMainBlockContainer"],
    div[data-testid="stAppViewBlockContainer"] {
        padding-top: 0.25rem !important;
        padding-bottom: 2.0rem !important;
        padding-left: 1.5rem !important;
        padding-right: 1.5rem !important;
    }
    
    /* 缩减 Streamlit 内部默认的垂直大间隙 */
    div[data-testid="stVerticalBlock"] {
        gap: 0.45rem !important;
    }

    /* Streamlit 原生顶部 Header：与 Deploy 按钮在同一行平铺 */
    header[data-testid="stHeader"],
    .stAppHeader {
        position: relative !important;
        top: 0 !important;
        left: 0 !important;
        right: 0 !important;
        width: 100% !important;
        height: 2.65rem !important;
        min-height: 2.65rem !important;
        background: rgba(14, 17, 23, 0.96);
        border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        display: flex !important;
        align-items: center !important;
        justify-content: space-between !important;
        padding: 0 14px !important;
        margin: 0 !important;
        z-index: 9999 !important;
        overflow: visible !important;
    }
    div[data-testid="stToolbar"] {
        position: static !important;
        display: flex !important;
        align-items: center !important;
        flex-shrink: 0 !important;
        margin-left: auto !important;
        gap: 8px !important;
        margin-top: 0 !important;
        margin-bottom: 0 !important;
        margin-right: 0 !important;
        padding: 0 !important;
        z-index: 10000 !important;
    }
    div[data-testid="stToolbar"] button,
    [data-testid="stMainMenu"] button,
    #MainMenu button {
        visibility: visible !important;
        display: inline-flex !important;
        opacity: 1 !important;
        pointer-events: auto !important;
        cursor: pointer !important;
    }
    #MainMenu {
        visibility: visible !important;
        display: block !important;
    }

    /* 下拉菜单、设置弹窗与模态对话框层级置顶，绝不被遮挡 */
    div[data-baseweb="popover"],
    div[data-baseweb="menu"],
    div[data-baseweb="layer"],
    [data-testid="stDialog"],
    [data-testid="stModal"] {
        z-index: 99999999 !important;
    }

    /* 顶部右上角深浅色模式切换圆形图标按钮：精确固定在桌面右上角 Deploy 按钮前 */
    #header-theme-icon-btn,
    .header-theme-icon-btn {
        position: fixed !important;
        top: 7px !important;
        right: 92px !important;
        z-index: 9999999 !important;
        background: rgba(255, 255, 255, 0.12) !important;
        border: 1px solid rgba(255, 255, 255, 0.25) !important;
        color: #e2e8f0 !important;
        border-radius: 50% !important;
        width: 29px !important;
        height: 29px !important;
        min-width: 29px !important;
        min-height: 29px !important;
        max-width: 29px !important;
        max-height: 29px !important;
        font-size: 14px !important;
        cursor: pointer !important;
        transition: all 0.2s ease !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        padding: 0 !important;
        line-height: 1 !important;
        box-sizing: border-box !important;
        outline: none !important;
        user-select: none !important;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25) !important;
    }
    #header-theme-icon-btn:hover,
    .header-theme-icon-btn:hover {
        background: rgba(56, 189, 248, 0.3) !important;
        border-color: #38bdf8 !important;
        transform: scale(1.1) !important;
        box-shadow: 0 0 12px rgba(56, 189, 248, 0.5) !important;
    }
    
    /* 顶部元数据与 Tab 同行样式：默认向右偏移以容纳左侧主 Tab */
    .header-db-meta-bar {
        display: flex !important;
        flex: 1 1 auto !important;
        align-items: center !important;
        flex-wrap: nowrap !important;
        gap: 8px 14px !important;
        font-size: 11.5px !important;
        color: #cfd8dc !important;
        white-space: nowrap !important;
        line-height: 2.65rem !important;
        overflow: visible !important;
        width: auto !important;
        max-width: none !important;
        margin-left: 325px !important;
    }
    .header-db-meta-bar .meta-item {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        color: #90a4ae;
        font-size: 11px;
    }
    .header-db-meta-bar .meta-val {
        color: #4caf50;
        font-weight: 700;
        font-size: 11.5px;
    }
    .header-db-meta-bar .meta-val-plain {
        color: #38bdf8;
        font-weight: 600;
        font-size: 11px;
    }
    .header-db-meta-bar .meta-val-warn {
        color: #ffb74d;
        font-weight: 700;
        font-size: 11.5px;
    }
    .header-db-meta-bar .meta-divider {
        width: 1px;
        height: 10px;
        background: rgba(255, 255, 255, 0.18);
        display: inline-block;
        margin: 0 2px;
    }

    /* 隐藏注入脚本的空组件容器与占位空元素 */
    iframe[data-testid="stCustomComponentV1"],
    div[data-testid="stCustomComponentV1"],
    iframe[data-testid="stIFrame"],
    div[data-testid="stElementContainer"]:has(iframe[data-testid="stIFrame"]),
    div[data-testid="element-container"]:has(iframe[data-testid="stIFrame"]),
    div[data-testid="stElementContainer"]:has(#app-global-loading-hud),
    div[data-testid="element-container"]:has(#app-global-loading-hud) {
        display: none !important;
        height: 0 !important;
        width: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        position: absolute !important;
    }

    /* 顶层主 Tab 内容面板顶部间距归零（因为顶层 TabList 已提升至顶部 Header，不占用内容区空间） */
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[data-testid="stTabPanel"],
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) div[data-testid="stTabPanel"],
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tabpanel"] {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }

    /* 顶层主 Tab 导航提升至顶部 Header 工具栏左侧（胶囊分段控制条风格） */
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"],
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div[data-baseweb="tab-list"] {
        position: fixed !important;
        top: 6px !important;
        left: 14px !important;
        height: 30px !important;
        min-height: 30px !important;
        max-height: 30px !important;
        width: auto !important;
        display: inline-flex !important;
        align-items: center !important;
        background: rgba(255, 255, 255, 0.08) !important;
        border: 1px solid rgba(255, 255, 255, 0.12) !important;
        border-radius: 8px !important;
        padding: 2px !important;
        gap: 4px !important;
        margin: 0 !important;
        z-index: 100000 !important;
        box-shadow: 0 2px 6px rgba(0, 0, 0, 0.25) !important;
    }
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"],
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-baseweb="tab"] {
        height: 24px !important;
        min-height: 24px !important;
        line-height: 24px !important;
        padding: 0 12px !important;
        font-size: 12px !important;
        font-weight: 500 !important;
        border-radius: 6px !important;
        border: none !important;
        color: rgba(255, 255, 255, 0.75) !important;
        cursor: pointer !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        transition: all 0.15s ease !important;
    }
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"]:hover {
        background: rgba(255, 255, 255, 0.12) !important;
        color: #ffffff !important;
    }
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"][aria-selected="true"] {
        background: #262730 !important;
        color: #ffffff !important;
        font-weight: 600 !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4) !important;
    }
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"][aria-selected="true"] p {
        color: #ffffff !important;
        font-weight: 600 !important;
    }
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-baseweb="tab-highlight"],
    div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) .react-aria-SelectionIndicator {
        display: none !important;
    }

    /* 运维面板内部二级 Sub-Tab 保持内嵌样式 */
    div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab-list"],
    div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tablist"] {
        background: rgba(15, 23, 42, 0.8) !important;
        border: 1px solid rgba(56, 189, 248, 0.25) !important;
        border-radius: 8px !important;
        padding: 3px 6px !important;
        gap: 6px !important;
        margin-bottom: 12px !important;
    }
    div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab"],
    div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"] {
        font-size: 12.5px !important;
        font-weight: 600 !important;
        border-radius: 6px !important;
        padding: 4px 14px !important;
        color: #94a3b8 !important;
        border: none !important;
    }
    div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="true"] {
        background: linear-gradient(135deg, rgba(14, 165, 233, 0.3), rgba(99, 102, 241, 0.3)) !important;
        color: #38bdf8 !important;
        border: 1px solid rgba(56, 189, 248, 0.5) !important;
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
        display: none !important;
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
        padding: 0 4px;
        box-sizing: border-box !important;
        width: 100%;
        margin: 0 !important;
        line-height: 26.5px !important;
        letter-spacing: -0.2px;
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
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button,
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
        padding: 0 2px !important;
        height: 28.5px !important;
        min-height: 28.5px !important;
        max-height: 28.5px !important;
        line-height: 26.5px !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stNumberInput"] input {
        text-align: center !important;
    }

    /* 下拉框内部箭头原生按钮重置（防止被通用 button 样式误伤为黑色方块） */
    div[data-testid="stSelectbox"] button,
    div[data-baseweb="select"] button {
        background: transparent !important;
        background-color: transparent !important;
        border: none !important;
        box-shadow: none !important;
        padding: 0 !important;
        min-height: unset !important;
        height: auto !important;
    }

    /* 工具栏专用翻页按钮 */
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button {
        border-radius: 4px !important;
        padding: 0 !important;
        height: 28.5px !important;
        min-height: 28.5px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        background: rgba(30, 41, 59, 0.8) !important;
        border: 1px solid rgba(255, 255, 255, 0.14) !important;
        color: #e2e8f0 !important;
        transition: all 0.15s ease !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button:not(:disabled):hover {
        background: rgba(56, 189, 248, 0.18) !important;
        border-color: #38bdf8 !important;
        color: #38bdf8 !important;
        box-shadow: 0 0 8px rgba(56, 189, 248, 0.25) !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button:disabled {
        background: rgba(15, 23, 42, 0.45) !important;
        border: 1px solid rgba(255, 255, 255, 0.06) !important;
        color: #64748b !important;
        opacity: 0.45 !important;
        cursor: not-allowed !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button p {
        font-size: 13px !important;
        line-height: normal !important;
        font-weight: 600 !important;
        margin: 0 !important;
        white-space: nowrap !important;
        color: inherit !important;
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
    .badge-orphan { background-color: rgba(255, 152, 0, 0.2); color: #ffb74d; border: 1px solid rgba(255, 152, 0, 0.4); }
    .badge-format { background-color: rgba(106, 27, 154, 0.2); color: #ce93d8; border: 1px solid rgba(106, 27, 154, 0.4); }
    .badge-duplicate { background-color: rgba(233, 30, 99, 0.22); color: #ff80ab; border: 1px solid rgba(233, 30, 99, 0.45); font-weight: 700; }

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

    /* 覆盖卡片内部操作按钮：精致小巧胶囊尺寸 (22px 高度，统一深色主题底色与细致边框) */
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button {
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
        background: rgba(30, 41, 59, 0.8) !important;
        border: 1px solid rgba(255, 255, 255, 0.14) !important;
        color: #e2e8f0 !important;
        transition: all 0.15s ease !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button:not(:disabled):hover,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a:hover,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button:not(:disabled):hover,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a:hover {
        background: rgba(56, 189, 248, 0.18) !important;
        border-color: #38bdf8 !important;
        color: #38bdf8 !important;
        box-shadow: 0 0 8px rgba(56, 189, 248, 0.25) !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] {
        display: inline-flex !important;
        align-items: center !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button:hover,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button:hover {
        background: rgba(239, 68, 68, 0.18) !important;
        border-color: #f87171 !important;
        color: #f87171 !important;
        box-shadow: 0 0 8px rgba(248, 113, 113, 0.3) !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button p,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a p,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button p,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a p,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button p,
    div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button p {
        font-size: 11px !important;
        line-height: 20px !important;
        margin: 0 !important;
        font-weight: 500 !important;
        white-space: nowrap !important;
        word-break: keep-all !important;
        color: inherit !important;
    }

    /* Popover 弹窗内部样式 */
    div[data-testid="stPopoverBody"] {
        padding: 10px 12px !important;
        border-radius: 6px !important;
        min-width: 200px !important;
    }
    div[data-testid="stPopoverBody"] p {
        font-size: 12px !important;
        line-height: 1.4 !important;
        margin: 0 0 6px 0 !important;
    }
    div[data-testid="stPopoverBody"] button {
        min-height: 28px !important;
        height: 28px !important;
        font-size: 12px !important;
        border-radius: 4px !important;
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
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stButton"] button {
        padding: 0 8px !important;
        justify-content: center !important;
        background: rgba(30, 41, 59, 0.8) !important;
        border: 1px solid rgba(255, 255, 255, 0.14) !important;
        color: #e2e8f0 !important;
        transition: all 0.15s ease !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stButton"] button:not(:disabled):hover {
        background: rgba(56, 189, 248, 0.18) !important;
        border-color: #38bdf8 !important;
        color: #38bdf8 !important;
        box-shadow: 0 0 10px rgba(56, 189, 248, 0.2) !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stButton"] button:disabled {
        background: rgba(15, 23, 42, 0.45) !important;
        border: 1px solid rgba(255, 255, 255, 0.06) !important;
        color: #64748b !important;
        opacity: 0.45 !important;
        cursor: not-allowed !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stButton"] button p {
        font-size: 13px !important;
        line-height: normal !important;
        font-weight: 600 !important;
        margin: 0 !important;
        color: inherit !important;
    }

    /* 全局标准通用按钮规范 (深色模式基准) */
    button[data-testid="stBaseButton-secondary"],
    .stButton > button:not([data-testid="stBaseButton-primary"]) {
        background: rgba(30, 41, 59, 0.8) !important;
        color: #f1f5f9 !important;
        border: 1px solid rgba(255, 255, 255, 0.14) !important;
        border-radius: 6px !important;
        transition: all 0.15s ease !important;
    }
    button[data-testid="stBaseButton-secondary"]:not(:disabled):hover,
    .stButton > button:not([data-testid="stBaseButton-primary"]):not(:disabled):hover {
        background: rgba(56, 189, 248, 0.16) !important;
        border-color: #38bdf8 !important;
        color: #38bdf8 !important;
        box-shadow: 0 0 10px rgba(56, 189, 248, 0.2) !important;
    }
    button[data-testid="stBaseButton-secondary"]:disabled,
    .stButton > button:not([data-testid="stBaseButton-primary"]):disabled {
        background: rgba(15, 23, 42, 0.45) !important;
        border-color: rgba(255, 255, 255, 0.06) !important;
        color: #64748b !important;
        opacity: 0.5 !important;
        cursor: not-allowed !important;
    }
    button[data-testid="stBaseButton-primary"] {
        background: linear-gradient(135deg, #0284c7, #2563eb) !important;
        border: 1px solid #38bdf8 !important;
        color: #ffffff !important;
        border-radius: 6px !important;
        box-shadow: 0 2px 10px rgba(56, 189, 248, 0.25) !important;
        transition: all 0.15s ease !important;
    }
    button[data-testid="stBaseButton-primary"]:hover {
        background: linear-gradient(135deg, #0369a1, #1d4ed8) !important;
        box-shadow: 0 0 16px rgba(56, 189, 248, 0.45) !important;
    }
</style>
""", unsafe_allow_html=True)


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


def delete_single_record(
    db_path: str,
    record_id: Union[int, str],
    raw_pdf_path: str = "",
    abs_path: str = ""
) -> Tuple[bool, bool, str]:
    """
    删除单条记录及其对应的本地 PDF 文件
    
    Returns:
        (db_deleted, pdf_deleted, message)
    """
    db_deleted = False
    pdf_deleted = False
    messages = []
    
    # 1. 尝试物理删除 PDF 文件
    target_pdf = ""
    if abs_path and os.path.exists(abs_path):
        target_pdf = abs_path
    elif raw_pdf_path:
        target_pdf = resolve_pdf_path(raw_pdf_path)
        
    if target_pdf and os.path.exists(target_pdf) and os.path.isfile(target_pdf):
        try:
            os.remove(target_pdf)
            pdf_deleted = True
            messages.append("PDF 文件已删除")
            # 清空 resolve_pdf_path 的路径缓存，避免后续查询返回已删除的失效路径
            resolve_pdf_path.cache_clear()
        except Exception as e:
            messages.append(f"PDF 删除失败: {e}")
    elif raw_pdf_path or abs_path:
        messages.append("本地 PDF 物理文件不存在")
    else:
        messages.append("无关联 PDF")
        
    # 2. 如果是数据库记录（非孤儿虚拟记录），从 SQLite 数据库中删除
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

# 激活全局高能加载/计算状态指示器
st.markdown(
    """
    <div id="app-global-loading-hud">
        <div class="hud-spinner-ring"></div>
        <div class="hud-text-box">
            <p class="hud-sub-text">数据检索与视图渲染中，请稍候</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

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

theme_injection_js = r"""
<script>
const metaData = __META_DATA_JSON__;

function applyAppTheme(themeId) {
    try {
        const pDoc = (window.parent && window.parent.document) || document;
        const currentTheme = (themeId === 'light') ? 'light' : 'dark';
        localStorage.setItem('viewer_theme', currentTheme);

        if (pDoc.documentElement) {
            pDoc.documentElement.setAttribute('data-theme', currentTheme);
            pDoc.documentElement.classList.remove('theme-light', 'theme-dark');
            pDoc.documentElement.classList.add('theme-' + currentTheme);
        }
        if (pDoc.body) {
            pDoc.body.setAttribute('data-theme', currentTheme);
            pDoc.body.classList.remove('theme-light', 'theme-dark');
            pDoc.body.classList.add('theme-' + currentTheme);
        }
        const stApp = pDoc.querySelector('.stApp');
        if (stApp) {
            stApp.setAttribute('data-theme', currentTheme);
            stApp.classList.remove('theme-light', 'theme-dark');
            stApp.classList.add('theme-' + currentTheme);
        }

        let styleEl = pDoc.getElementById('viewer-custom-theme-style');
        if (!styleEl) {
            styleEl = pDoc.createElement('style');
            styleEl.id = 'viewer-custom-theme-style';
            pDoc.head.appendChild(styleEl);
        }
        // 确保主题 style 标签永远是 head 最后一个（最高 CSS 优先级）
        if (styleEl !== pDoc.head.lastElementChild) {
            pDoc.head.appendChild(styleEl);
        }

        // 同步加载遮罩组件的主题属性与类名
        const hud = pDoc.getElementById('app-global-loading-hud');
        if (hud) {
            hud.setAttribute('data-theme', currentTheme);
            hud.classList.toggle('hud-theme-light', currentTheme === 'light');
            hud.classList.toggle('hud-theme-dark', currentTheme !== 'light');
            hud.classList.toggle('theme-light', currentTheme === 'light');
            hud.classList.toggle('theme-dark', currentTheme !== 'light');
        }

        // ── 直接通过 JS 内联样式强制覆盖 Header 背景（绕过 CSS 优先级）──
        // 挂载为持久全局函数，以便 injectHeaderMetadata 的 setInterval 也能调用
        pDoc._forceHeaderStyle = function(theme) {
            const headers = pDoc.querySelectorAll(
                'header[data-testid="stHeader"], .stAppHeader, div[data-testid="stHeader"]'
            );
            headers.forEach(h => {
                if (theme === 'light') {
                    h.style.setProperty('background', '#ffffff', 'important');
                    h.style.setProperty('background-color', '#ffffff', 'important');
                    h.style.setProperty('border-bottom', '1px solid #e2e8f0', 'important');
                    h.style.setProperty('box-shadow', '0 1px 4px rgba(0,0,0,0.06)', 'important');
                } else {
                    h.style.removeProperty('background');
                    h.style.removeProperty('background-color');
                    h.style.removeProperty('border-bottom');
                    h.style.removeProperty('box-shadow');
                }
            });
        };
        pDoc._forceHeaderStyle(currentTheme);
        // 延迟补刷（防止 Streamlit 在主题应用后立刻重写 DOM）
        setTimeout(() => pDoc._forceHeaderStyle && pDoc._forceHeaderStyle(currentTheme), 80);
        setTimeout(() => pDoc._forceHeaderStyle && pDoc._forceHeaderStyle(currentTheme), 350);

        // ── 直接通过 JS 内联样式强制覆盖各类 Widget 背景（绕过 Emotion CSS !important 竞争）──
        pDoc._forceWidgetStyles = function(theme) {
            const isLight = theme === 'light';

            // 1. 下拉框（Select）内部箭头/Chevron 按钮 → 透明背景，消除黑色小方块
            pDoc.querySelectorAll(
                'div[data-baseweb="select"] button, div[data-testid="stSelectbox"] button'
            ).forEach(btn => {
                btn.style.setProperty('background', 'transparent', 'important');
                btn.style.setProperty('background-color', 'transparent', 'important');
                btn.style.setProperty('border', 'none', 'important');
                btn.style.setProperty('box-shadow', 'none', 'important');
                btn.style.setProperty('padding', '0', 'important');
                btn.style.setProperty('min-height', 'unset', 'important');
                btn.style.setProperty('height', 'auto', 'important');
            });

            // 2. 下拉框外层容器 → 浅/深色背景
            pDoc.querySelectorAll('div[data-baseweb="select"] > div').forEach(el => {
                if (isLight) {
                    el.style.setProperty('background-color', '#ffffff', 'important');
                    el.style.setProperty('background', '#ffffff', 'important');
                    el.style.setProperty('border', '1px solid #cbd5e1', 'important');
                    el.style.setProperty('color', '#0f172a', 'important');
                } else {
                    el.style.removeProperty('background-color');
                    el.style.removeProperty('background');
                    el.style.removeProperty('border');
                    el.style.removeProperty('color');
                }
            });

            // 3. 下拉框内部所有 div 子元素（StyledEndEnhancer 等）→ 透明背景
            pDoc.querySelectorAll('div[data-baseweb="select"] > div > div').forEach(el => {
                el.style.setProperty('background', 'transparent', 'important');
                el.style.setProperty('background-color', 'transparent', 'important');
            });

            if (isLight) {
                // 4. 顶部工具栏 stButton 按钮（⬅ ➡ 翻页）→ 白底
                pDoc.querySelectorAll(
                    'div[data-testid="stHorizontalBlock"] div[data-testid="stButton"] button'
                ).forEach(btn => {
                    // 排除卡片区域（将单独处理）
                    const isCardRow = btn.closest('[data-testid="stHorizontalBlock"]') &&
                        btn.closest('[data-testid="stHorizontalBlock"]').querySelector('.card-meta-row');
                    if (!isCardRow) {
                        btn.style.setProperty('background-color', '#ffffff', 'important');
                        btn.style.setProperty('background', '#ffffff', 'important');
                        btn.style.setProperty('border', '1px solid #cbd5e1', 'important');
                        btn.style.setProperty('color', '#334155', 'important');
                        btn.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                    }
                });

                // 5. 卡片操作按钮（原网页 / 资源链接 / 本地定位）→ 白底
                pDoc.querySelectorAll(
                    'div[data-testid="stHorizontalBlock"] div[data-testid="stButton"] button, ' +
                    'div[data-testid="stHorizontalBlock"] div[data-testid="stLinkButton"] a'
                ).forEach(el => {
                    const block = el.closest('[data-testid="stHorizontalBlock"]');
                    if (block && block.querySelector('.card-meta-row')) {
                        el.style.setProperty('background-color', '#ffffff', 'important');
                        el.style.setProperty('background', '#ffffff', 'important');
                        el.style.setProperty('border', '1px solid #cbd5e1', 'important');
                        el.style.setProperty('color', '#334155', 'important');
                        el.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                    }
                });

                // 6. Popover 触发按钮（🗑️ 删除）→ 白底，使用更宽泛的选择：stPopover 下一切按钮
                pDoc.querySelectorAll('div[data-testid="stPopover"] button').forEach(btn => {
                    btn.style.setProperty('background-color', '#ffffff', 'important');
                    btn.style.setProperty('background', '#ffffff', 'important');
                    btn.style.setProperty('border', '1px solid #cbd5e1', 'important');
                    btn.style.setProperty('color', '#334155', 'important');
                    btn.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                });
            } else {
                // 深色模式：还原 stButton / stLinkButton / stPopover 按钮的内联样式
                pDoc.querySelectorAll(
                    'div[data-testid="stButton"] button, ' +
                    'div[data-testid="stLinkButton"] a, ' +
                    'div[data-testid="stPopover"] button'
                ).forEach(el => {
                    el.style.removeProperty('background-color');
                    el.style.removeProperty('background');
                    el.style.removeProperty('border');
                    el.style.removeProperty('color');
                    el.style.removeProperty('box-shadow');
                });
            }
        };
        pDoc._forceWidgetStyles(currentTheme);
        setTimeout(() => pDoc._forceWidgetStyles && pDoc._forceWidgetStyles(currentTheme), 80);
        setTimeout(() => pDoc._forceWidgetStyles && pDoc._forceWidgetStyles(currentTheme), 350);
        setTimeout(() => pDoc._forceWidgetStyles && pDoc._forceWidgetStyles(currentTheme), 900);

        if (currentTheme === 'light') {
            styleEl.innerHTML = `
                /* ================= 浅色模式 (Light Theme) 全局视觉样式 ================= */
                html, body, .stApp, [data-testid="stAppViewContainer"], .stMain, [data-testid="stMain"], [data-testid="stAppViewBlockContainer"], [data-testid="stMainBlockContainer"], .stMainBlockContainer {
                    background-color: #f8fafc !important;
                    color: #0f172a !important;
                }
                /* 全局文字颜色兜底：覆盖 Streamlit/BaseWeb 默认浅色文字 */
                .stApp *, .stApp *::before, .stApp *::after {
                    color: inherit;
                }
                
                /* 顶部 Header ── 浅色模式适配（多选择器强制覆盖 Streamlit 默认深色 Header） */
                header[data-testid="stHeader"],
                .stAppHeader,
                div[data-testid="stHeader"],
                [data-testid="stHeader"],
                [class*="stHeader"],
                .stApp > header,
                #stDecoration + header {
                    background: #ffffff !important;
                    background-color: #ffffff !important;
                    border-bottom: 1px solid #e2e8f0 !important;
                    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06) !important;
                }
                header[data-testid="stHeader"] *,
                .stAppHeader *,
                div[data-testid="stHeader"] * {
                    color: #334155 !important;
                }
                header[data-testid="stHeader"] button, .stAppHeader button, div[data-testid="stToolbar"] button, [data-testid="stMainMenu"] button, #MainMenu button {
                    color: #334155 !important;
                    fill: #334155 !important;
                }
                header[data-testid="stHeader"] svg, .stAppHeader svg, div[data-testid="stToolbar"] svg, [data-testid="stMainMenu"] svg, #MainMenu svg {
                    fill: #334155 !important;
                    color: #334155 !important;
                }
                .header-db-meta-bar { color: #334155 !important; }
                .header-db-meta-bar .meta-item { color: #64748b !important; }
                .header-db-meta-bar .meta-val { color: #16a34a !important; }
                .header-db-meta-bar .meta-val-plain { color: #0284c7 !important; }
                .header-db-meta-bar .meta-val-warn { color: #d97706 !important; }
                .header-db-meta-bar .meta-divider { background: #cbd5e1 !important; }

                /* 顶部右上角切换按钮 (浅色模式状态) */
                #header-theme-icon-btn, .header-theme-icon-btn {
                    background: #f1f5f9 !important;
                    border: 1px solid #cbd5e1 !important;
                    color: #0f172a !important;
                    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08) !important;
                }
                #header-theme-icon-btn:hover, .header-theme-icon-btn:hover {
                    background: #e2e8f0 !important;
                    border-color: #0284c7 !important;
                    box-shadow: 0 0 10px rgba(2, 132, 199, 0.3) !important;
                    transform: scale(1.1) !important;
                }

                /* 顶层主 Tab 导航 ── 浅色模式（Header 胶囊条） */
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"],
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div[data-baseweb="tab-list"],
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tablist"] {
                    background: rgba(0, 0, 0, 0.05) !important;
                    border: 1px solid rgba(0, 0, 0, 0.1) !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05) !important;
                }
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-testid="stTab"],
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-baseweb="tab"],
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tab"] {
                    color: #64748b !important;
                    background: transparent !important;
                }
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-testid="stTab"]:hover,
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-baseweb="tab"]:hover,
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tab"]:hover {
                    background: rgba(0, 0, 0, 0.05) !important;
                    color: #0f172a !important;
                }
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-testid="stTab"][aria-selected="true"],
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-baseweb="tab"][aria-selected="true"],
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tab"][aria-selected="true"] {
                    background: #ffffff !important;
                    color: #0284c7 !important;
                    border: 1px solid rgba(0, 0, 0, 0.08) !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1) !important;
                }
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [aria-selected="true"] p,
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-testid="stTab"][aria-selected="true"] p {
                    color: #0284c7 !important;
                    font-weight: 600 !important;
                }
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [aria-selected="false"] p,
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-testid="stTab"][aria-selected="false"] p {
                    color: #64748b !important;
                    font-weight: 500 !important;
                }

                /* 运维中心内嵌二级 Tab ── 浅色模式 */
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab-list"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tablist"] {
                    background: #f1f5f9 !important;
                    border: 1px solid #e2e8f0 !important;
                    border-radius: 8px !important;
                    padding: 3px 6px !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tab"] {
                    background: transparent !important;
                    color: #64748b !important;
                    border: 1px solid transparent !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab"]:hover,
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"]:hover,
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tab"]:hover {
                    color: #0f172a !important;
                    background: rgba(0, 0, 0, 0.04) !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"][aria-selected="true"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tab"][aria-selected="true"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="true"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"][aria-selected="true"] p,
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="true"] p {
                    background: #ffffff !important;
                    color: #0284c7 !important;
                    border: 1px solid #0284c7 !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08) !important;
                    font-weight: 600 !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="false"] p,
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"][aria-selected="false"] p {
                    color: #64748b !important;
                }

                /* 筛选工具栏输入框、下拉框、标签与按钮 */
                div[data-testid="stWidgetLabel"] p, div[data-testid="stWidgetLabel"] label, label[data-testid="stWidgetLabel"] {
                    color: #334155 !important;
                    font-weight: 600 !important;
                }
                /* 控件标签本身永远是无边框的纯文本（防止外层容器边框把标签框成独立盒子） */
                div[data-testid="stWidgetLabel"] {
                    background: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
                }
                div[data-testid="stTextInput"] div[data-baseweb="input"],
                div[data-testid="stNumberInput"] div[data-baseweb="input"],
                div[data-testid="stTextInputRootElement"],
                div[data-testid="stNumberInputContainer"] {
                    background-color: #ffffff !important;
                    border: 1px solid #cbd5e1 !important;
                    color: #0f172a !important;
                }
                div[data-testid="stTextInput"] input,
                div[data-testid="stNumberInput"] input {
                    background-color: #ffffff !important;
                    color: #0f172a !important;
                    border: none !important;
                }
                div[data-testid="stTextInput"] input::placeholder {
                    color: #94a3b8 !important;
                }

                /* Select 下拉框 ── 浅色模式（彻底消除黑色小方块；外层容器一律无边框，仅控件行保留单线边框，避免标签与控件出现多层嵌套边框） */
                div[data-testid="stSelectbox"],
                div[data-testid="stSelectbox"] > div,
                div[data-baseweb="select"] {
                    background: transparent !important;
                    background-color: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
                    color: #0f172a !important;
                }
                div[data-baseweb="select"] > div {
                    background-color: #ffffff !important;
                    background: #ffffff !important;
                    border: 1px solid #cbd5e1 !important;
                    color: #0f172a !important;
                }
                div[data-baseweb="select"] > div > div,
                div[data-baseweb="select"] > div > div *,
                [class*="ValueContainer"],
                [class*="SingleValue"],
                [class*="Placeholder"],
                [class*="placeholder"],
                [class*="singleValue"],
                [class*="value-container"],
                [class*="StyledEndEnhancer"],
                [class*="EndEnhancer"] {
                    background-color: transparent !important;
                    background: transparent !important;
                    color: #0f172a !important;
                }
                div[data-baseweb="select"] svg,
                div[data-testid="stSelectbox"] svg {
                    fill: #64748b !important;
                    color: #64748b !important;
                }

                /* 下拉弹出层 */
                div[data-baseweb="popover"],
                div[data-baseweb="menu"],
                ul[role="listbox"],
                [data-baseweb="popover"] *,
                [data-baseweb="menu"] * {
                    background-color: #ffffff !important;
                    color: #1e293b !important;
                }
                div[data-baseweb="popover"],
                div[data-baseweb="menu"],
                ul[role="listbox"] {
                    border: 1px solid #e2e8f0 !important;
                    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.1) !important;
                }
                li[role="option"],
                li[role="option"] * {
                    background-color: #ffffff !important;
                    color: #1e293b !important;
                }
                li[role="option"]:hover,
                li[role="option"]:hover *,
                li[role="option"][aria-selected="true"],
                li[role="option"][aria-selected="true"] * {
                    background-color: #f1f5f9 !important;
                    color: #0284c7 !important;
                }
                .toolbar-stat-badge {
                    background: #f1f5f9 !important;
                    border: 1px solid #cbd5e1 !important;
                    color: #334155 !important;
                }
                .toolbar-stat-badge .stat-num { color: #16a34a !important; }
                .toolbar-stat-badge .stat-page { color: #0284c7 !important; }

                /* 下拉框内部箭头原生按钮重置（防止被通用 button 样式误伤为黑色方块） */
                div[data-testid="stSelectbox"] button,
                div[data-baseweb="select"] button {
                    background: transparent !important;
                    background-color: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
                    padding: 0 !important;
                    min-height: unset !important;
                    height: auto !important;
                }

                /* 全局所有次要按钮、链接按钮、Popover 触发按钮（浅色模式统一白底） */
                button,
                button[data-testid="stBaseButton-secondary"],
                button[kind="secondary"],
                div[data-testid="stButton"] button,
                .stButton > button,
                div[data-testid="stLinkButton"] a,
                .stLinkButton a,
                div[data-testid="stPopover"] > button,
                div[data-testid="stPopover"] button,
                .stPopover button,
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button {
                    background-color: #ffffff !important;
                    background: #ffffff !important;
                    border: 1px solid #cbd5e1 !important;
                    color: #334155 !important;
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04) !important;
                }
                button:not(:disabled):hover,
                button[data-testid="stBaseButton-secondary"]:not(:disabled):hover,
                div[data-testid="stButton"] button:not(:disabled):hover,
                .stButton > button:not(:disabled):hover,
                div[data-testid="stLinkButton"] a:hover,
                .stLinkButton a:hover,
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button:not(:disabled):hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button:not(:disabled):hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a:hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button:not(:disabled):hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a:hover {
                    background-color: #f0f9ff !important;
                    background: #f0f9ff !important;
                    border-color: #0284c7 !important;
                    color: #0284c7 !important;
                    box-shadow: 0 1px 4px rgba(2, 132, 199, 0.15) !important;
                }
                div[data-testid="stPopover"] > button:hover,
                div[data-testid="stPopover"] button:hover,
                .stPopover button:hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button:hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button:hover {
                    background-color: #fef2f2 !important;
                    background: #fef2f2 !important;
                    border-color: #ef4444 !important;
                    color: #ef4444 !important;
                    box-shadow: 0 1px 4px rgba(239, 68, 68, 0.15) !important;
                }
                button:disabled,
                button[data-testid="stBaseButton-secondary"]:disabled,
                div[data-testid="stButton"] button:disabled,
                .stButton > button:disabled,
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button:disabled {
                    background: #f8fafc !important;
                    background-color: #f8fafc !important;
                    border-color: #e2e8f0 !important;
                    color: #94a3b8 !important;
                    opacity: 0.55 !important;
                    cursor: not-allowed !important;
                }
                button p,
                div[data-testid="stButton"] button p,
                .stButton > button p,
                div[data-testid="stLinkButton"] a p,
                .stLinkButton a p,
                div[data-testid="stPopover"] button p,
                .stPopover button p,
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button p {
                    color: inherit !important;
                }
                .grid-row-divider {
                    background: #e2e8f0 !important;
                }

                /* 卡片画廊与内容区域 */
                div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .card-title-row) > div[data-testid="stColumn"]:not(:last-child) {
                    border-right: 1px solid #e2e8f0 !important;
                }
                .card-title-text {
                    color: #0f172a !important;
                }
                .card-id-badge {
                    background: #e0f2fe !important;
                    color: #0284c7 !important;
                    border: 1px solid #bae6fd !important;
                }
                .card-meta-row {
                    color: #475569 !important;
                }
                .card-meta-item {
                    color: #475569 !important;
                }
                .card-meta-divider {
                    background: #cbd5e1 !important;
                }
                .badge-source {
                    background-color: #e0f2fe !important;
                    color: #0369a1 !important;
                    border: 1px solid #bae6fd !important;
                }
                .badge-category {
                    background-color: #fef3c7 !important;
                    color: #b45309 !important;
                    border: 1px solid #fde68a !important;
                }
                .badge-orphan {
                    background-color: #ffedd5 !important;
                    color: #c2410c !important;
                    border: 1px solid #fed7aa !important;
                }
                .badge-format {
                    background-color: #f3e8ff !important;
                    color: #7e22ce !important;
                    border: 1px solid #e9d5ff !important;
                }
                .badge-duplicate {
                    background-color: #ffe4e6 !important;
                    color: #be123c !important;
                    border: 1px solid #fecdd3 !important;
                }
                div[data-testid="stPopoverBody"] {
                    background: #ffffff !important;
                    border: 1px solid #e2e8f0 !important;
                    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12) !important;
                    color: #0f172a !important;
                }
                div[data-testid="stPopoverBody"] p,
                div[data-testid="stPopoverBody"] span,
                div[data-testid="stPopoverBody"] strong,
                div[data-testid="stPopoverBody"] small {
                    color: #0f172a !important;
                }
                .pdf-scroll-container {
                    background: #f8fafc !important;
                    border: 1px solid #e2e8f0 !important;
                }
                .pdf-scroll-container::-webkit-scrollbar-track {
                    background: #f1f5f9 !important;
                }
                .pdf-scroll-container::-webkit-scrollbar-thumb {
                    background: #cbd5e1 !important;
                }
                .pdf-scroll-container::-webkit-scrollbar-thumb:hover {
                    background: #94a3b8 !important;
                }
                .pdf-empty-placeholder {
                    background: #f8fafc !important;
                    border: 1px dashed #cbd5e1 !important;
                    color: #64748b !important;
                }
                .pdf-empty-placeholder .empty-title {
                    color: #334155 !important;
                }
                .pdf-empty-placeholder .empty-desc {
                    color: #64748b !important;
                }
                .pdf-empty-placeholder code {
                    background: #e2e8f0 !important;
                    color: #0f172a !important;
                }

                /* 运维面板与组件 (Tab 2) */
                .fixes-hub-header-banner {
                    background: linear-gradient(135deg, #f8fafc, #f1f5f9) !important;
                    border: 1.5px solid #bae6fd !important;
                    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.05) !important;
                    border-radius: 12px;
                    padding: 14px 20px;
                    margin-bottom: 16px;
                }
                .fixes-hub-banner-title {
                    color: #0284c7 !important;
                    font-size: 20px;
                    font-weight: 700;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    margin: 0;
                }
                .fixes-hub-banner-desc {
                    color: #64748b !important;
                    font-size: 12.5px;
                    margin: 4px 0 0 0;
                }
                .fixes-hub-badge-blue {
                    background: #e0f2fe !important;
                    border: 1px solid #bae6fd !important;
                    color: #0284c7 !important;
                    border-radius: 20px;
                    padding: 3px 10px;
                    font-size: 11.5px;
                    font-weight: 600;
                }
                .fixes-hub-badge-green {
                    background: #dcfce7 !important;
                    border: 1px solid #bbf7d0 !important;
                    color: #16a34a !important;
                    border-radius: 20px;
                    padding: 3px 10px;
                    font-size: 11.5px;
                    font-weight: 600;
                }
                div[data-testid="stExpander"] {
                    background: #ffffff !important;
                    border: 1px solid #e2e8f0 !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04) !important;
                }
                div[data-testid="stExpander"] summary,
                div[data-testid="stExpander"] summary * {
                    color: #0f172a !important;
                }
                div[data-testid="stExpander"] summary:hover,
                div[data-testid="stExpander"] summary:hover * {
                    color: #0284c7 !important;
                }
                div[data-testid="stMetric"] {
                    background: #f8fafc !important;
                    border: 1px solid #e2e8f0 !important;
                    border-radius: 6px !important;
                }
                div[data-testid="stMetricValue"] {
                    color: #0f172a !important;
                }
                div[data-testid="stMetricLabel"] {
                    color: #64748b !important;
                }
                
                /* 全局标准通用按钮 (浅色模式) */
                button[data-testid="stBaseButton-secondary"],
                .stButton > button:not([data-testid="stBaseButton-primary"]) {
                    background: #ffffff !important;
                    color: #1e293b !important;
                    border: 1px solid #cbd5e1 !important;
                    border-radius: 6px !important;
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04) !important;
                    transition: all 0.15s ease !important;
                }
                button[data-testid="stBaseButton-secondary"]:not(:disabled):hover,
                .stButton > button:not([data-testid="stBaseButton-primary"]):not(:disabled):hover {
                    background: #f0f9ff !important;
                    border-color: #0284c7 !important;
                    color: #0284c7 !important;
                    box-shadow: 0 2px 6px rgba(2, 132, 199, 0.15) !important;
                }
                button[data-testid="stBaseButton-secondary"]:disabled,
                .stButton > button:not([data-testid="stBaseButton-primary"]):disabled {
                    background: #f8fafc !important;
                    border-color: #e2e8f0 !important;
                    color: #94a3b8 !important;
                    opacity: 0.55 !important;
                    cursor: not-allowed !important;
                }
                button[data-testid="stBaseButton-primary"] {
                    background: linear-gradient(135deg, #0284c7, #0369a1) !important;
                    border: 1px solid #0284c7 !important;
                    color: #ffffff !important;
                    border-radius: 6px !important;
                    box-shadow: 0 2px 6px rgba(2, 132, 199, 0.25) !important;
                    transition: all 0.15s ease !important;
                }
                button[data-testid="stBaseButton-primary"]:hover {
                    background: linear-gradient(135deg, #0369a1, #075985) !important;
                    border-color: #0369a1 !important;
                    box-shadow: 0 4px 12px rgba(2, 132, 199, 0.35) !important;
                }
                div[data-testid="stCodeBlock"], pre, code {
                    background: #f1f5f9 !important;
                    color: #0f172a !important;
                    border-color: #e2e8f0 !important;
                }
                div[data-testid="stDataFrame"], [data-testid="stTable"] {
                    background: #ffffff !important;
                }
                .pagination-text {
                    color: #334155 !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button {
                    background: #ffffff !important;
                    border: 1px solid #cbd5e1 !important;
                    color: #334155 !important;
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04) !important;
                    transition: all 0.15s ease !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button:not(:disabled):hover {
                    background: #f0f9ff !important;
                    border-color: #0284c7 !important;
                    color: #0284c7 !important;
                    box-shadow: 0 1px 4px rgba(2, 132, 199, 0.15) !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button:disabled {
                    background: #f8fafc !important;
                    border-color: #e2e8f0 !important;
                    color: #94a3b8 !important;
                    opacity: 0.55 !important;
                    cursor: not-allowed !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button p {
                    color: inherit !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) input {
                    background: #ffffff !important;
                    color: #0f172a !important;
                    border: 1px solid #cbd5e1 !important;
                }

                /* 浅色模式全局加载遮罩微调与全屏遮罩 */
                .stApp[data-test-script-state="running"]::after {
                    background: rgba(241, 245, 249, 0.6) !important;
                    backdrop-filter: blur(2.5px) brightness(1.02) !important;
                    -webkit-backdrop-filter: blur(2.5px) brightness(1.02) !important;
                }
                .stApp[data-test-script-state="running"]::before {
                    background: linear-gradient(90deg, #0284c7, #38bdf8, #6366f1, #10b981, #0284c7) !important;
                    box-shadow: 0 0 14px rgba(2, 132, 199, 0.8), 0 0 22px rgba(56, 189, 248, 0.6) !important;
                }
                #app-global-loading-hud,
                body.theme-light #app-global-loading-hud,
                body[data-theme="light"] #app-global-loading-hud,
                html[data-theme="light"] #app-global-loading-hud,
                #app-global-loading-hud.hud-theme-light,
                #app-global-loading-hud[data-theme="light"] {
                    background: rgba(255, 255, 255, 0.98) !important;
                    border: 1.5px solid #0284c7 !important;
                    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.15), 0 0 30px rgba(2, 132, 199, 0.25) !important;
                }
                #app-global-loading-hud .hud-spinner-ring,
                body.theme-light #app-global-loading-hud .hud-spinner-ring,
                body[data-theme="light"] #app-global-loading-hud .hud-spinner-ring,
                #app-global-loading-hud.hud-theme-light .hud-spinner-ring,
                #app-global-loading-hud[data-theme="light"] .hud-spinner-ring {
                    border: 2.5px solid rgba(2, 132, 199, 0.2) !important;
                    border-top-color: #0284c7 !important;
                    border-right-color: #38bdf8 !important;
                }
                #app-global-loading-hud .hud-sub-text,
                body.theme-light #app-global-loading-hud .hud-sub-text,
                body[data-theme="light"] #app-global-loading-hud .hud-sub-text,
                #app-global-loading-hud.hud-theme-light .hud-sub-text,
                #app-global-loading-hud[data-theme="light"] .hud-sub-text {
                    color: #0f172a !important;
                    font-weight: 600 !important;
                }
                .stApp[data-test-script-state="running"] #app-global-loading-hud,
                #app-global-loading-hud.is-active,
                body.theme-light #app-global-loading-hud.is-active,
                body[data-theme="light"] #app-global-loading-hud.is-active,
                #app-global-loading-hud.hud-theme-light.is-active {
                    animation: hudModalFadeIn 0.24s cubic-bezier(0.16, 1, 0.3, 1) forwards, hudPulseGlowLight 2.2s infinite ease-in-out !important;
                }
                [data-testid="stStatusWidget"] {
                    background: #ffffff !important;
                    border-color: #cbd5e1 !important;
                    color: #0f172a !important;
                }
                [data-testid="stStatusWidget"] * {
                    color: #0f172a !important;
                }

                /* 浅色模式 stDecoration 顶部装饰条适配 */
                #stDecoration, [data-testid="stDecoration"] {
                    display: none !important;
                }

                /* 浅色模式通用文字输入控件颜色兜底 */
                .stApp input, .stApp textarea, .stApp select {
                    color: #0f172a !important;
                    background-color: #ffffff !important;
                }

                /* 浅色模式页面过渡动画 */
                html, body, .stApp {
                    transition: background-color 0.25s ease, color 0.25s ease !important;
                }
            `;
        } else {
            styleEl.innerHTML = `
                /* ================= 深色模式 (Dark Theme) 增强视觉样式 ================= */
                html, body, .stApp, [data-testid="stAppViewContainer"], .stMain, [data-testid="stMain"], [data-testid="stAppViewBlockContainer"], [data-testid="stMainBlockContainer"], .stMainBlockContainer {
                    background-color: #0e1117 !important;
                    color: #f8fafc !important;
                }
                
                /* 顶部 Header */
                header[data-testid="stHeader"], .stAppHeader, div[data-testid="stHeader"], [data-testid="stHeader"] {
                    background: rgba(14, 17, 23, 0.96) !important;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.1) !important;
                    box-shadow: none !important;
                }
                header[data-testid="stHeader"] button, .stAppHeader button, div[data-testid="stToolbar"] button, [data-testid="stMainMenu"] button, #MainMenu button {
                    color: #e2e8f0 !important;
                    fill: #e2e8f0 !important;
                }
                header[data-testid="stHeader"] svg, .stAppHeader svg, div[data-testid="stToolbar"] svg, [data-testid="stMainMenu"] svg, #MainMenu svg {
                    fill: #e2e8f0 !important;
                    color: #e2e8f0 !important;
                }
                .header-db-meta-bar { color: #cfd8dc !important; }
                .header-db-meta-bar .meta-item { color: #90a4ae !important; }
                .header-db-meta-bar .meta-val { color: #4caf50 !important; }
                .header-db-meta-bar .meta-val-plain { color: #38bdf8 !important; }
                .header-db-meta-bar .meta-val-warn { color: #ffb74d !important; }
                .header-db-meta-bar .meta-divider { background: rgba(255, 255, 255, 0.18) !important; }

                /* 顶部右上角切换按钮 (深色模式状态) */
                #header-theme-icon-btn, .header-theme-icon-btn {
                    background: rgba(255, 255, 255, 0.12) !important;
                    border: 1px solid rgba(255, 255, 255, 0.25) !important;
                    color: #e2e8f0 !important;
                    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25) !important;
                }
                #header-theme-icon-btn:hover, .header-theme-icon-btn:hover {
                    background: rgba(56, 189, 248, 0.3) !important;
                    border-color: #38bdf8 !important;
                    box-shadow: 0 0 12px rgba(56, 189, 248, 0.5) !important;
                    transform: scale(1.1) !important;
                }

                /* 顶层主 Tab 导航 ── 深色模式（Header 胶囊条） */
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"],
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div[data-baseweb="tab-list"] {
                    background: rgba(255, 255, 255, 0.08) !important;
                    border: 1px solid rgba(255, 255, 255, 0.12) !important;
                    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.25) !important;
                }
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"],
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-baseweb="tab"] {
                    color: rgba(255, 255, 255, 0.75) !important;
                }
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"]:hover {
                    background: rgba(255, 255, 255, 0.12) !important;
                    color: #ffffff !important;
                }
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"][aria-selected="true"] {
                    background: #262730 !important;
                    color: #ffffff !important;
                    border: 1px solid rgba(255, 255, 255, 0.15) !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4) !important;
                }
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"][aria-selected="true"] p {
                    color: #ffffff !important;
                    font-weight: 600 !important;
                }
                div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"][aria-selected="false"] p {
                    color: rgba(255, 255, 255, 0.75) !important;
                    font-weight: 500 !important;
                }

                /* 运维中心内嵌二级 Tab ── 深色模式 */
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab-list"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tablist"] {
                    background: rgba(15, 23, 42, 0.8) !important;
                    border: 1px solid rgba(56, 189, 248, 0.25) !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tab"] {
                    color: #94a3b8 !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="true"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="true"] * {
                    background: linear-gradient(135deg, rgba(14, 165, 233, 0.3), rgba(99, 102, 241, 0.3)) !important;
                    color: #38bdf8 !important;
                    border: 1px solid rgba(56, 189, 248, 0.5) !important;
                }

                /* 深色模式 Select 文字（防止切换后残留浅色文字样式） */
                div[data-baseweb="select"] span,
                div[data-baseweb="select"] div,
                div[data-baseweb="select"] p,
                div[data-baseweb="select"] *,
                div[data-testid="stSelectbox"] *,
                [class*="ValueContainer"],
                [class*="SingleValue"],
                [class*="Placeholder"],
                [class*="placeholder"],
                [class*="singleValue"] {
                    color: #f1f5f9 !important;
                }
                div[data-baseweb="select"] > div,
                div[data-testid="stSelectbox"] > div > div {
                    background-color: rgba(30, 41, 59, 0.8) !important;
                    border: 1px solid rgba(255, 255, 255, 0.12) !important;
                    color: #f1f5f9 !important;
                }
                div[data-baseweb="select"] svg,
                div[data-testid="stSelectbox"] svg {
                    fill: #94a3b8 !important;
                    color: #94a3b8 !important;
                }
                li[role="option"],
                li[role="option"] * {
                    background-color: #1e293b !important;
                    color: #f1f5f9 !important;
                }
                li[role="option"]:hover,
                li[role="option"]:hover *,
                li[role="option"][aria-selected="true"],
                li[role="option"][aria-selected="true"] * {
                    background-color: rgba(56, 189, 248, 0.15) !important;
                    color: #38bdf8 !important;
                }

                /* 深色模式 PDF 预览容器 */
                .pdf-scroll-container {
                    background: #1a1f2e !important;
                    border: 1px solid rgba(255, 255, 255, 0.1) !important;
                }

                /* 下拉框内部箭头原生按钮重置（防止被通用 button 样式误伤为黑色方块） */
                div[data-testid="stSelectbox"] button,
                div[data-baseweb="select"] button {
                    background: transparent !important;
                    background-color: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
                    padding: 0 !important;
                    min-height: unset !important;
                    height: auto !important;
                }

                /* 深色模式分页与工具栏按钮 */
                .pagination-text { color: #cfd8dc !important; }
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button {
                    background: rgba(30, 41, 59, 0.8) !important;
                    border: 1px solid rgba(255, 255, 255, 0.14) !important;
                    color: #e2e8f0 !important;
                    transition: all 0.15s ease !important;
                }
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button:not(:disabled):hover {
                    background: rgba(56, 189, 248, 0.18) !important;
                    border-color: #38bdf8 !important;
                    color: #38bdf8 !important;
                    box-shadow: 0 0 8px rgba(56, 189, 248, 0.25) !important;
                }
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button:disabled {
                    background: rgba(15, 23, 42, 0.45) !important;
                    border: 1px solid rgba(255, 255, 255, 0.06) !important;
                    color: #64748b !important;
                    opacity: 0.45 !important;
                    cursor: not-allowed !important;
                }
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button p {
                    color: inherit !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stButton"] button {
                    background: rgba(30, 41, 59, 0.8) !important;
                    border: 1px solid rgba(255, 255, 255, 0.14) !important;
                    color: #e2e8f0 !important;
                    transition: all 0.15s ease !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button:not(:disabled):hover {
                    background: rgba(56, 189, 248, 0.18) !important;
                    border-color: #38bdf8 !important;
                    color: #38bdf8 !important;
                    box-shadow: 0 0 10px rgba(56, 189, 248, 0.2) !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button:disabled {
                    background: rgba(15, 23, 42, 0.45) !important;
                    border: 1px solid rgba(255, 255, 255, 0.06) !important;
                    color: #64748b !important;
                    opacity: 0.45 !important;
                    cursor: not-allowed !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button p {
                    color: inherit !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) input {
                    background: rgba(30, 41, 59, 0.8) !important;
                    color: #e2e8f0 !important;
                    border: 1px solid rgba(255, 255, 255, 0.12) !important;
                }

                /* 卡片操作按钮 ── 深色模式统一风格（暗色半透底色 + 细致边框 + 天蓝/玫瑰悬浮光晕） */
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button {
                    background: rgba(30, 41, 59, 0.8) !important;
                    border: 1px solid rgba(255, 255, 255, 0.14) !important;
                    color: #e2e8f0 !important;
                    transition: all 0.15s ease !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button:not(:disabled):hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a:hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button:not(:disabled):hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a:hover {
                    background: rgba(56, 189, 248, 0.18) !important;
                    border-color: #38bdf8 !important;
                    color: #38bdf8 !important;
                    box-shadow: 0 0 8px rgba(56, 189, 248, 0.25) !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] {
                    display: inline-flex !important;
                    align-items: center !important;
                    margin: 0 !important;
                    padding: 0 !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button:hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button:hover {
                    background: rgba(239, 68, 68, 0.18) !important;
                    border-color: #f87171 !important;
                    color: #f87171 !important;
                    box-shadow: 0 0 8px rgba(248, 113, 113, 0.3) !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button p {
                    color: inherit !important;
                }

                /* 全局标准按钮 (深色模式) */
                button[data-testid="stBaseButton-secondary"],
                .stButton > button:not([data-testid="stBaseButton-primary"]) {
                    background: rgba(30, 41, 59, 0.8) !important;
                    color: #f1f5f9 !important;
                    border: 1px solid rgba(255, 255, 255, 0.14) !important;
                    border-radius: 6px !important;
                    transition: all 0.15s ease !important;
                }
                button[data-testid="stBaseButton-secondary"]:not(:disabled):hover,
                .stButton > button:not([data-testid="stBaseButton-primary"]):not(:disabled):hover {
                    background: rgba(56, 189, 248, 0.16) !important;
                    border-color: #38bdf8 !important;
                    color: #38bdf8 !important;
                    box-shadow: 0 0 10px rgba(56, 189, 248, 0.2) !important;
                }
                button[data-testid="stBaseButton-secondary"]:disabled,
                .stButton > button:not([data-testid="stBaseButton-primary"]):disabled {
                    background: rgba(15, 23, 42, 0.45) !important;
                    border-color: rgba(255, 255, 255, 0.06) !important;
                    color: #64748b !important;
                    opacity: 0.5 !important;
                    cursor: not-allowed !important;
                }
                button[data-testid="stBaseButton-primary"] {
                    background: linear-gradient(135deg, #0284c7, #2563eb) !important;
                    border: 1px solid #38bdf8 !important;
                    color: #ffffff !important;
                    border-radius: 6px !important;
                    box-shadow: 0 2px 10px rgba(56, 189, 248, 0.25) !important;
                    transition: all 0.15s ease !important;
                }
                button[data-testid="stBaseButton-primary"]:hover {
                    background: linear-gradient(135deg, #0369a1, #1d4ed8) !important;
                    box-shadow: 0 0 16px rgba(56, 189, 248, 0.45) !important;
                }

                /* 运维面板 Header Banner (深色模式) */
                .fixes-hub-header-banner {
                    background: linear-gradient(135deg, rgba(30, 41, 59, 0.8), rgba(15, 23, 42, 0.95)) !important;
                    border: 1.5px solid rgba(56, 189, 248, 0.4) !important;
                    border-radius: 12px;
                    padding: 14px 20px;
                    margin-bottom: 16px;
                    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.6) !important;
                }
                .fixes-hub-banner-title {
                    color: #38bdf8 !important;
                    font-size: 20px;
                    font-weight: 700;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    margin: 0;
                }
                .fixes-hub-banner-desc {
                    color: #94a3b8 !important;
                    font-size: 12.5px;
                    margin: 4px 0 0 0;
                }
                .fixes-hub-badge-blue {
                    background: rgba(56, 189, 248, 0.15) !important;
                    border: 1px solid rgba(56, 189, 248, 0.3) !important;
                    color: #38bdf8 !important;
                    border-radius: 20px;
                    padding: 3px 10px;
                    font-size: 11.5px;
                    font-weight: 600;
                }
                .fixes-hub-badge-green {
                    background: rgba(34, 197, 94, 0.15) !important;
                    border: 1px solid rgba(34, 197, 94, 0.3) !important;
                    color: #4ade80 !important;
                    border-radius: 20px;
                    padding: 3px 10px;
                    font-size: 11.5px;
                    font-weight: 600;
                }

                /* 深色模式全局加载遮罩微调与全屏遮罩 */
                .stApp[data-test-script-state="running"]::after {
                    background: rgba(10, 15, 29, 0.45) !important;
                    backdrop-filter: blur(2.5px) brightness(0.8) !important;
                    -webkit-backdrop-filter: blur(2.5px) brightness(0.8) !important;
                }
                .stApp[data-test-script-state="running"]::before {
                    background: linear-gradient(90deg, #00e5ff, #2979ff, #7c4dff, #00e676, #00e5ff) !important;
                    box-shadow: 0 0 14px rgba(41, 121, 255, 0.9), 0 0 25px rgba(0, 229, 255, 0.7) !important;
                }
                #app-global-loading-hud,
                body.theme-dark #app-global-loading-hud,
                body[data-theme="dark"] #app-global-loading-hud,
                html[data-theme="dark"] #app-global-loading-hud,
                #app-global-loading-hud.hud-theme-dark,
                #app-global-loading-hud[data-theme="dark"] {
                    background: rgba(15, 23, 42, 0.96) !important;
                    border: 1.5px solid #38bdf8 !important;
                    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.9), 0 0 35px rgba(56, 189, 248, 0.5) !important;
                }
                #app-global-loading-hud .hud-spinner-ring,
                body.theme-dark #app-global-loading-hud .hud-spinner-ring,
                body[data-theme="dark"] #app-global-loading-hud .hud-spinner-ring,
                #app-global-loading-hud.hud-theme-dark .hud-spinner-ring,
                #app-global-loading-hud[data-theme="dark"] .hud-spinner-ring {
                    border: 2.5px solid rgba(56, 189, 248, 0.25) !important;
                    border-top-color: #00e5ff !important;
                    border-right-color: #6366f1 !important;
                }
                #app-global-loading-hud .hud-sub-text,
                body.theme-dark #app-global-loading-hud .hud-sub-text,
                body[data-theme="dark"] #app-global-loading-hud .hud-sub-text,
                #app-global-loading-hud.hud-theme-dark .hud-sub-text,
                #app-global-loading-hud[data-theme="dark"] .hud-sub-text {
                    color: #e2e8f0 !important;
                    font-weight: 600 !important;
                }
                .stApp[data-test-script-state="running"] #app-global-loading-hud,
                #app-global-loading-hud.is-active,
                body.theme-dark #app-global-loading-hud.is-active,
                body[data-theme="dark"] #app-global-loading-hud.is-active,
                #app-global-loading-hud.hud-theme-dark.is-active {
                    animation: hudModalFadeIn 0.24s cubic-bezier(0.16, 1, 0.3, 1) forwards, hudPulseGlowDark 2.2s infinite ease-in-out !important;
                }

                /* 深色模式页面过渡动画 */
                html, body, .stApp {
                    transition: background-color 0.25s ease, color 0.25s ease !important;
                }
            `;
        }

        const btn = pDoc.getElementById('header-theme-icon-btn');
        if (btn) {
            btn.textContent = (currentTheme === 'light') ? '🌙' : '☀️';
            btn.title = (currentTheme === 'light') ? '切换为深色模式' : '切换为浅色模式';
        }
    } catch (e) {}
}

try {
    const pDocGlobal = (window.parent && window.parent.document) || document;
    pDocGlobal._toggleTheme = function() {
        const cur = localStorage.getItem('viewer_theme') || 'dark';
        applyAppTheme(cur === 'dark' ? 'light' : 'dark');
    };
} catch(e) {}

function injectHeaderMetadata() {
    try {
        const pDoc = (window.parent && window.parent.document) || document;
        const header = pDoc.querySelector('header[data-testid="stHeader"], .stAppHeader, div[data-testid="stHeader"]');
        if (!header) return;

        const curTheme = localStorage.getItem('viewer_theme') || 'dark';

        // 精简图标模式：剔除 meta-item 中的 emoji 图标
        const compactIcons = !!metaData.compactIcons;
        const iconStripped = compactIcons
            ? (metaText) => metaText.replace(/[\u{1F000}-\u{1FAFF}\u2190-\u27BF\u2B00-\u2BFF\uFE0F]/gu, '')
            : (metaText) => metaText;

        // 1. 左侧：数据库统计元数据条
        const metaHtml = iconStripped(`
            <span class="meta-item">数据库 <b class="meta-val-plain">${metaData.dbName}</b></span>
            <span class="meta-item">PDF <b class="meta-val-plain">${metaData.pdfDir}</b></span>
            <span class="meta-divider"></span>
            <span class="meta-item">数据库记录 <b class="meta-val">${metaData.total}</b></span>
            <span class="meta-divider"></span>
            <span class="meta-item">含 PDF 路径 <b class="meta-val">${metaData.hasPdf}</b></span>
            <span class="meta-divider"></span>
            <span class="meta-item">磁盘孤儿 PDF <b class="meta-val-warn">${metaData.orphans}</b></span>
            <span class="meta-divider"></span>
            <span class="meta-item">来源渠道 <b class="meta-val">${metaData.sources}</b></span>
            <span class="meta-divider"></span>
            <span class="meta-item">内容分类 <b class="meta-val">${metaData.categories}</b></span>
        `);

        let metaBar = pDoc.getElementById('header-db-metadata-bar');
        if (!metaBar) {
            metaBar = pDoc.createElement('div');
            metaBar.id = 'header-db-metadata-bar';
            metaBar.className = 'header-db-meta-bar';
            header.insertBefore(metaBar, header.firstChild);
        }
        metaBar.innerHTML = metaHtml;

        // 动态校准左侧主 Tab 占位宽度，确保元数据条紧随其后且永不重叠
        const topTabList = pDoc.querySelector('div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tablist"]');
        if (topTabList) {
            const rect = topTabList.getBoundingClientRect();
            if (rect.width > 50 && rect.width < 600) {
                metaBar.style.marginLeft = Math.ceil(rect.left + rect.width + 18) + 'px';
            }
        }

        // 2. 右上角：在 body 顶层创建并挂载固定在桌面右上角的切换图标按钮
        let themeBtn = pDoc.getElementById('header-theme-icon-btn');
        if (!themeBtn) {
            themeBtn = pDoc.createElement('button');
            themeBtn.id = 'header-theme-icon-btn';
            themeBtn.className = 'header-theme-icon-btn';
            themeBtn.onclick = function(e) {
                e.preventDefault();
                e.stopPropagation();
                try {
                    (pDoc._toggleTheme || window._toggleTheme || pDocGlobal._toggleTheme)();
                } catch(e) {}
            };
            pDoc.body.appendChild(themeBtn);
        }
        themeBtn.textContent = (curTheme === 'light') ? '🌙' : '☀️';
        themeBtn.title = (curTheme === 'light') ? '切换为深色模式' : '切换为浅色模式';

        // 每次 metadata 刷新时同步修正 Header 颜色（防止 Streamlit 重渲后恢复深色）
        if (pDoc._forceHeaderStyle) {
            pDoc._forceHeaderStyle(curTheme);
        }

        // ── 内联修正 Select / Toolbar / Popover 控件样式（每 800ms 随 metadata 刷新执行，防止 Streamlit 重渲染回深色） ──
        (function fixWidgetStyles(theme) {
            const isLight = theme === 'light';

            // 1. 所有 BaseWeb Select 外层容器（包含值+箭头那个 div） → 浅/深色背景
            pDoc.querySelectorAll('div[data-baseweb="select"] > div').forEach(function(el) {
                if (isLight) {
                    el.style.setProperty('background-color', '#ffffff', 'important');
                    el.style.setProperty('background', '#ffffff', 'important');
                    el.style.setProperty('border', '1px solid #cbd5e1', 'important');
                    el.style.setProperty('color', '#0f172a', 'important');
                } else {
                    el.style.removeProperty('background-color');
                    el.style.removeProperty('background');
                    el.style.removeProperty('border');
                    el.style.removeProperty('color');
                }
            });

            // 2. BaseWeb Select 内第二层所有子 div（ValueContainer, EndEnhancer 等） → 透明背景
            pDoc.querySelectorAll('div[data-baseweb="select"] > div > div').forEach(function(el) {
                el.style.setProperty('background', 'transparent', 'important');
                el.style.setProperty('background-color', 'transparent', 'important');
            });

            // 3. BaseWeb Select 内所有 button（Chevron 箭头按钮）→ 透明背景，无边框
            pDoc.querySelectorAll('div[data-baseweb="select"] button').forEach(function(btn) {
                btn.style.setProperty('background', 'transparent', 'important');
                btn.style.setProperty('background-color', 'transparent', 'important');
                btn.style.setProperty('border', 'none', 'important');
                btn.style.setProperty('box-shadow', 'none', 'important');
            });

            // 4. stSelectbox 内所有 button（兜底） → 透明背景
            pDoc.querySelectorAll('div[data-testid="stSelectbox"] button').forEach(function(btn) {
                btn.style.setProperty('background', 'transparent', 'important');
                btn.style.setProperty('background-color', 'transparent', 'important');
                btn.style.setProperty('border', 'none', 'important');
                btn.style.setProperty('box-shadow', 'none', 'important');
            });

            if (isLight) {
                // 5. stButton 翻页按钮 → 白底（排除卡片区域）
                pDoc.querySelectorAll('div[data-testid="stButton"] button').forEach(function(btn) {
                    var block = btn.closest('[data-testid="stHorizontalBlock"]');
                    var isCard = block && block.querySelector('.card-meta-row');
                    if (!isCard) {
                        btn.style.setProperty('background-color', '#ffffff', 'important');
                        btn.style.setProperty('background', '#ffffff', 'important');
                        btn.style.setProperty('border', '1px solid #cbd5e1', 'important');
                        btn.style.setProperty('color', '#334155', 'important');
                        btn.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                    }
                });

                // 6. stLinkButton → 白底
                pDoc.querySelectorAll('div[data-testid="stLinkButton"] a').forEach(function(a) {
                    a.style.setProperty('background-color', '#ffffff', 'important');
                    a.style.setProperty('background', '#ffffff', 'important');
                    a.style.setProperty('border', '1px solid #cbd5e1', 'important');
                    a.style.setProperty('color', '#334155', 'important');
                    a.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                });

                // 7. stPopover 触发按钮（🗑️ 删除等）→ 白底
                pDoc.querySelectorAll('div[data-testid="stPopover"] button').forEach(function(btn) {
                    btn.style.setProperty('background-color', '#ffffff', 'important');
                    btn.style.setProperty('background', '#ffffff', 'important');
                    btn.style.setProperty('border', '1px solid #cbd5e1', 'important');
                    btn.style.setProperty('color', '#334155', 'important');
                    btn.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                });
            } else {
                // 深色模式：去除内联样式还原默认深色
                pDoc.querySelectorAll(
                    'div[data-testid="stButton"] button, div[data-testid="stLinkButton"] a, div[data-testid="stPopover"] button'
                ).forEach(function(el) {
                    el.style.removeProperty('background-color');
                    el.style.removeProperty('background');
                    el.style.removeProperty('border');
                    el.style.removeProperty('color');
                    el.style.removeProperty('box-shadow');
                });
            }
        })(curTheme);

        applyAppTheme(curTheme);
    } catch (e) {}
}

function initGlobalLoadingIndicator() {
    try {
        const pDoc = (window.parent && window.parent.document) || document;
        if (!pDoc || !pDoc.body) return;

        const curTheme = localStorage.getItem('viewer_theme') || 'dark';

        // 1. 创建或获取全局悬浮加载 HUD 容器（强制挂载在 body 顶层，彻底杜绝被页面容器磨砂虚化）
        let hud = pDoc.getElementById('app-global-loading-hud');
        const hudContent = `
            <div class="hud-spinner-ring"></div>
            <div class="hud-text-box">
                <div class="hud-sub-text">数据检索与视图渲染中，请稍候</div>
            </div>
        `;
        if (hud) {
            hud.setAttribute('data-theme', curTheme);
            hud.classList.toggle('hud-theme-light', curTheme === 'light');
            hud.classList.toggle('hud-theme-dark', curTheme !== 'light');
            hud.classList.toggle('theme-light', curTheme === 'light');
            hud.classList.toggle('theme-dark', curTheme !== 'light');
            if (hud.parentElement !== pDoc.body) {
                pDoc.body.appendChild(hud);
            }
            hud.innerHTML = hudContent;
        } else {
            hud = pDoc.createElement('div');
            hud.id = 'app-global-loading-hud';
            hud.setAttribute('data-theme', curTheme);
            hud.classList.add(curTheme === 'light' ? 'hud-theme-light' : 'hud-theme-dark');
            hud.classList.add(curTheme === 'light' ? 'theme-light' : 'theme-dark');
            hud.innerHTML = hudContent;
            pDoc.body.appendChild(hud);
        }

        function updateLoadingState(isRunning) {
            if (hud) {
                if (isRunning) {
                    hud.classList.add('is-active');
                } else {
                    hud.classList.remove('is-active');
                }
            }
        }

        // 2. 监听 Streamlit 根容器的 running 状态属性
        const stApp = pDoc.querySelector('.stApp, [data-testid="stApp"], [data-test-script-state]');
        if (stApp) {
            const state = stApp.getAttribute('data-test-script-state');
            updateLoadingState(state === 'running');

            if (!window._stLoadingObserver) {
                window._stLoadingObserver = new MutationObserver((mutations) => {
                    for (const mutation of mutations) {
                        if (mutation.type === 'attributes' && mutation.attributeName === 'data-test-script-state') {
                            const curState = mutation.target.getAttribute('data-test-script-state');
                            updateLoadingState(curState === 'running');
                        }
                    }
                });
                window._stLoadingObserver.observe(stApp, { attributes: true, attributeFilter: ['data-test-script-state'] });
            }
        } else {
            updateLoadingState(false);
        }

        // 3. 用户交互事件劫持：点击按钮、下拉选项、翻页跳转、回车搜索时瞬间唤起加载态（0ms 即时响应）
        // 明确排除主题切换按钮 (#header-theme-icon-btn)，防止纯前端主题切换时加载遮罩永久卡死
        if (!pDoc._loadingEventsBound) {
            pDoc._loadingEventsBound = true;
            pDoc.addEventListener('click', (e) => {
                const themeBtnEl = e.target.closest('#header-theme-icon-btn, .header-theme-icon-btn');
                if (themeBtnEl) {
                    return; // 点击主题切换按钮时不触发加载遮罩
                }
                const interactiveEl = e.target.closest('button, [role="button"], [data-baseweb="select"], [data-baseweb="option"], [data-baseweb="tab"], [data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-primary"]');
                if (interactiveEl) {
                    if (interactiveEl.id === 'header-theme-icon-btn' || interactiveEl.classList.contains('header-theme-icon-btn')) {
                        return;
                    }
                    updateLoadingState(true);
                }
            }, true);

            pDoc.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    const input = e.target.closest('input');
                    if (input) {
                        updateLoadingState(true);
                    }
                }
            }, true);
        }
    } catch (e) {}
}

injectHeaderMetadata();
initGlobalLoadingIndicator();
setTimeout(() => { injectHeaderMetadata(); initGlobalLoadingIndicator(); }, 50);
setTimeout(() => { injectHeaderMetadata(); initGlobalLoadingIndicator(); }, 200);
setTimeout(() => { injectHeaderMetadata(); initGlobalLoadingIndicator(); }, 600);
setTimeout(() => { injectHeaderMetadata(); initGlobalLoadingIndicator(); }, 1200);
setInterval(injectHeaderMetadata, 3000);

// 监听 Streamlit 重渲染时对 head 中 style 标签的重新注入，确保主题 CSS 始终居最后（最高优先级）
;(() => {
    try {
        const pDoc = (window.parent && window.parent.document) || document;
        if (!pDoc || !pDoc.head) return;
        // 当 Streamlit 向 head 注入新 style 时，将主题 style 标签移到最后以确保其覆盖优先级最高
        const observer = new MutationObserver((mutations) => {
            for (const m of mutations) {
                for (const node of m.addedNodes) {
                    if (node.nodeName === 'STYLE' && node.id !== 'viewer-custom-theme-style') {
                        // Streamlit 重新注入了新的 style，将主题 style 移到最后
                        const themeStyle = pDoc.getElementById('viewer-custom-theme-style');
                        if (themeStyle && themeStyle !== pDoc.head.lastElementChild) {
                            pDoc.head.appendChild(themeStyle); // 移到最后位置就是最高优先级
                        }
                        break;
                    }
                }
            }
        });
        observer.observe(pDoc.head, { childList: true });
    } catch(e) {}
})();

// ── 独立 Widget 样式修复定时器（每 400ms 执行，与 injectHeaderMetadata 完全独立，确保 Select/Button/Popover 始终维持正确的主题样式）──
;(function startWidgetStyleInterval() {
    try {
        const pDoc = (window.parent && window.parent.document) || document;

        function applyWidgetFix() {
            try {
                // 以页面真实主题（DOM data-theme）为准，避免 localStorage 与页面主题脱节导致控件配色错乱
                const domThemeEl = (pDoc.documentElement && pDoc.documentElement.getAttribute('data-theme'))
                    || (pDoc.body && pDoc.body.getAttribute('data-theme'))
                    || (pDoc.querySelector('.stApp') && pDoc.querySelector('.stApp').getAttribute('data-theme'));
                const theme = domThemeEl || localStorage.getItem('viewer_theme') || 'dark';
                const isLight = theme === 'light';

                // 1. BaseWeb Select 外层容器 → 浅/深色背景
                pDoc.querySelectorAll('div[data-baseweb="select"] > div').forEach(function(el) {
                    if (isLight) {
                        el.style.setProperty('background-color', '#ffffff', 'important');
                        el.style.setProperty('background', '#ffffff', 'important');
                        el.style.setProperty('border', '1px solid #cbd5e1', 'important');
                        el.style.setProperty('color', '#0f172a', 'important');
                    } else {
                        el.style.removeProperty('background-color');
                        el.style.removeProperty('background');
                        el.style.removeProperty('border');
                        el.style.removeProperty('color');
                    }
                });

                // 2. BaseWeb Select 内第二层子 div（ValueContainer, EndEnhancer 等）→ 透明
                pDoc.querySelectorAll('div[data-baseweb="select"] > div > div').forEach(function(el) {
                    el.style.setProperty('background', 'transparent', 'important');
                    el.style.setProperty('background-color', 'transparent', 'important');
                });

                // 3. BaseWeb Select 内所有 button（Chevron 按钮）→ 透明
                pDoc.querySelectorAll('div[data-baseweb="select"] button, div[data-testid="stSelectbox"] button').forEach(function(btn) {
                    btn.style.setProperty('background', 'transparent', 'important');
                    btn.style.setProperty('background-color', 'transparent', 'important');
                    btn.style.setProperty('border', 'none', 'important');
                    btn.style.setProperty('box-shadow', 'none', 'important');
                });

                if (isLight) {
                    // 4. stButton 按钮（工具栏/翻页）→ 白底（排除卡片区域）
                    pDoc.querySelectorAll('div[data-testid="stButton"] button').forEach(function(btn) {
                        var block = btn.closest('[data-testid="stHorizontalBlock"]');
                        var isCard = block && block.querySelector('.card-meta-row');
                        if (!isCard) {
                            btn.style.setProperty('background-color', '#ffffff', 'important');
                            btn.style.setProperty('background', '#ffffff', 'important');
                            btn.style.setProperty('border', '1px solid #cbd5e1', 'important');
                            btn.style.setProperty('color', '#334155', 'important');
                            btn.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                        }
                    });

                    // 5. stLinkButton → 白底
                    pDoc.querySelectorAll('div[data-testid="stLinkButton"] a').forEach(function(a) {
                        a.style.setProperty('background-color', '#ffffff', 'important');
                        a.style.setProperty('background', '#ffffff', 'important');
                        a.style.setProperty('border', '1px solid #cbd5e1', 'important');
                        a.style.setProperty('color', '#334155', 'important');
                        a.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                    });

                    // 6. stPopover 触发按钮（🗑️ 删除等）→ 白底
                    pDoc.querySelectorAll('div[data-testid="stPopover"] button').forEach(function(btn) {
                        btn.style.setProperty('background-color', '#ffffff', 'important');
                        btn.style.setProperty('background', '#ffffff', 'important');
                        btn.style.setProperty('border', '1px solid #cbd5e1', 'important');
                        btn.style.setProperty('color', '#334155', 'important');
                        btn.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                    });
                } else {
                    // 深色模式：去除内联样式
                    pDoc.querySelectorAll(
                        'div[data-testid="stButton"] button, div[data-testid="stLinkButton"] a, div[data-testid="stPopover"] button'
                    ).forEach(function(el) {
                        el.style.removeProperty('background-color');
                        el.style.removeProperty('background');
                        el.style.removeProperty('border');
                        el.style.removeProperty('color');
                        el.style.removeProperty('box-shadow');
                    });
                }
            } catch(err) {}
        }

        // 立即执行一次，随后在关键时机事件驱动重试（MutationObserver 监听 Streamlit 重渲染），
        // 仅保留低频兜底定时器（2s）确保兜底生效，彻底告别 400ms 高频 DOM 全量扫描
        applyWidgetFix();
        setTimeout(applyWidgetFix, 200);
        setTimeout(applyWidgetFix, 600);
        setTimeout(applyWidgetFix, 1500);

        // 事件驱动：Streamlit 每次 rerun 完成后 DOM 会有新节点出现，用观察器精准触发修复
        let widgetFixObserver = null;
        try {
            const appRoot = pDoc.querySelector('.stApp') || pDoc.body;
            if (appRoot) {
                widgetFixObserver = new MutationObserver(function(mutations) {
                    let hasNewButtons = false;
                    for (let i = 0; i < mutations.length; i++) {
                        const m = mutations[i];
                        if (m.type === 'childList' && m.addedNodes && m.addedNodes.length) {
                            hasNewButtons = true;
                            break;
                        }
                    }
                    if (hasNewButtons) {
                        try { applyWidgetFix(); } catch(err) {}
                    }
                });
                widgetFixObserver.observe(appRoot, { childList: true, subtree: true });
            }
        } catch(err) {}

        // 低频兜底：每 2s 巡检一次（仅当页面存在交互控件时才执行样式修复）
        setInterval(function() {
            if (pDoc.querySelector('[data-baseweb="select"], [data-testid="stButton"] button, [data-testid="stPopover"] button, [data-testid="stLinkButton"] a')) {
                try { applyWidgetFix(); } catch(err) {}
            }
        }, 2000);
    } catch(e) {}
})();
</script>
"""

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

# 如果查询条件或页码发生变动，或内存无数据，则执行数据库真实分页查询
if st.session_state.get("cached_query_sig") != current_query_sig or "cached_df" not in st.session_state:
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

# 如果页码超界，重查第 1 页
if total_records > 0 and st.session_state.current_page > total_pages:
    st.session_state.current_page = 1
    st.session_state.top_jump = 1
    st.session_state.bottom_jump = 1
    st.session_state.cached_query_sig = ""
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
    badge_list = [f'<span class="badge badge-source">{source_esc}</span>']
    if category and category != "-" and category != "未分类":
        badge_cls = "badge-orphan" if "孤儿" in category else "badge-category"
        category_disp = html.escape(T(str(category)), quote=True)
        badge_list.append(f'<span class="badge {badge_cls}">{category_disp}</span>')
    if fmt and fmt != "-":
        badge_list.append(f'<span class="badge badge-format">{fmt_esc}</span>')
    
    dup_cnt = row.get("dup_cnt", 1)
    if dup_cnt and int(dup_cnt) > 1:
        badge_list.append(f'<span class="badge badge-duplicate" title="包含 {dup_cnt} 条相同重复记录（已按组相邻排列）">重复 ({dup_cnt}条)</span>')
    
    badges_str = "".join(badge_list)
    meta_html = f'''
    <div class="card-meta-row">
        <div>{badges_str}</div>
        <span class="card-meta-divider"></span>
        <span class="card-meta-item">{T('发布')}: {publish_time_esc}</span>
        <span class="card-meta-item">{T('大小')}: {size_esc}</span>
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
                    if st.button(b_label, key=f"loc_{item_id}_{card_index}", use_container_width=False):
                        open_in_system(b_val)
                elif b_type == "delete":
                    with st.popover(b_label, key=f"del_pop_{item_id}", use_container_width=False):
                        st.markdown(f"**确认删除记录 #{item_id}？**")
                        pdf_info = T("包含关联本地 PDF 物理文件") if resolved_pdf else T("无关联本地 PDF")
                        st.caption(T(f"将永久删除数据库记录及对应文件（{pdf_info}），此操作不可撤销！"))
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
        except Exception as e:
            st.error(f"读取 PDF 文件失败: {e}")
    else:
        # 空状态占位高度与 PDF 预览完全一致，保持网格完美对齐
        placeholder_height = iframe_height
        if raw_pdf_path:
            status_title = "PDF 路径未找到"
            status_desc = f"登记路径: <code>{html.escape(str(raw_pdf_path), quote=True)}</code>"
        else:
            status_title = "未关联本地 PDF 文件"
            status_desc = "可通过上方「原网页」或「资源链接」查看详情"
        
        placeholder_html = f'''
        <div class="pdf-empty-placeholder" style="height: {placeholder_height}px;">
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
        st.button(T("上一页"), key=f"{key_prefix}_prev", disabled=(st.session_state.current_page <= 1), use_container_width=True, on_click=prev_page)
    with p_col4:
        st.number_input(
            "跳转页码",
            min_value=1,
            max_value=total_pages,
            key=f"{key_prefix}_jump",
            label_visibility="collapsed",
            on_change=on_bottom_jump_change
        )
    with p_col5:
        st.button(T("下一页"), key=f"{key_prefix}_next", disabled=(st.session_state.current_page >= total_pages), use_container_width=True, on_click=next_page)


# ==================== 顶部主导航 Tab 与多维筛选工具栏 ====================

main_tab_gallery, main_tab_maintenance = st.tabs([T("资源画廊浏览"), T("数据与系统维护中心")])

with main_tab_gallery:
    f_cols = st.columns([1.4, 0.6, 0.6, 0.85, 0.85, 1.0, 1.1, 0.72, 0.80, 0.24, 0.28, 0.24], vertical_alignment="bottom")
    with f_cols[0]:
        st.text_input(T("关键词搜索"), placeholder="输入标题 / 链接 / 磁力...", key="f_keyword", on_change=reset_page)
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
        st.selectbox(T("排版模式"), ["双列画廊 (推荐)", "单列大图", "三列紧凑", "纯表格视图"], key="f_layout", format_func=T)
    with f_cols[7]:
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
    with f_cols[8]:
        st.markdown(
            f"""
            <div class="toolbar-stat-badge" title="共 {total_records:,} 条数据 (第 {st.session_state.current_page} / {total_pages} 页)">
                共 <span class="stat-num">{total_records:,}</span> 条 (<span class="stat-page">{st.session_state.current_page}</span>/{total_pages}页)
            </div>
            """,
            unsafe_allow_html=True
        )
    with f_cols[9]:
        st.button("上一页", key="top_prev", disabled=(st.session_state.current_page <= 1), use_container_width=True, on_click=prev_page)
    with f_cols[10]:
        st.number_input(
            "跳转页码",
            min_value=1,
            max_value=total_pages,
            key="top_jump",
            label_visibility="collapsed",
            on_change=on_top_jump_change
        )
    with f_cols[11]:
        st.button("下一页", key="top_next", disabled=(st.session_state.current_page >= total_pages), use_container_width=True, on_click=next_page)

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
st.iframe(
    """
    <script>
    (function() {
        function initPdfViewportLazyLoading() {
            try {
                const pDoc = (window.parent && window.parent.document) || document;
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
                    if (!container) return;
                    const curItemId = container.dataset.itemId || "";
                    if (container.dataset.renderedItemId !== curItemId) {
                        container.dataset.renderedItemId = curItemId;
                        container.dataset.initScrolled = "0";
                    }
                    if (container.dataset.initScrolled === "1") return;
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
                lazyImages.forEach(img => {
                    if (img.dataset.src && img.src !== img.dataset.src) {
                        window._pdfImgObserver.observe(img);
                    }
                });

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
            const pDoc = (window.parent && window.parent.document) || document;
            if (!window._pdfMutationObserver && pDoc && pDoc.body) {
                window._pdfMutationObserver = new MutationObserver(initPdfViewportLazyLoading);
                window._pdfMutationObserver.observe(pDoc.body, { childList: true, subtree: true });
            }
        } catch(e) {}
    })();
    </script>
    """,
    height=1,
    width=1
)


