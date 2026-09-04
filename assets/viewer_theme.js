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

                // 6. Popover 触发按钮（🗑️ 删除）→ 白底，使用更宽泛的选择：stPopover 下一切按钮（排除 Tooltip 提示图标）
                pDoc.querySelectorAll('div[data-testid="stPopover"] button').forEach(btn => {
                    if (btn.closest('[data-testid="stTooltipIcon"]')) {
                        btn.style.setProperty('background', 'transparent', 'important');
                        btn.style.setProperty('background-color', 'transparent', 'important');
                        btn.style.setProperty('border', 'none', 'important');
                        btn.style.setProperty('box-shadow', 'none', 'important');
                        return;
                    }
                    btn.style.setProperty('background-color', '#ffffff', 'important');
                    btn.style.setProperty('background', '#ffffff', 'important');
                    btn.style.setProperty('border', '1px solid #cbd5e1', 'important');
                    btn.style.setProperty('color', '#334155', 'important');
                    btn.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                });

                // Tooltip 提示图标按钮内联样式重置
                pDoc.querySelectorAll('div[data-testid="stTooltipIcon"] button').forEach(btn => {
                    btn.style.setProperty('background', 'transparent', 'important');
                    btn.style.setProperty('background-color', 'transparent', 'important');
                    btn.style.setProperty('border', 'none', 'important');
                    btn.style.setProperty('box-shadow', 'none', 'important');
                });

                // 7. Checkbox 复选框与 Toggle 开关 ── 浅色模式适配（彻底消除黑色小方块）
                pDoc.querySelectorAll('div[data-testid="stCheckbox"]').forEach(cb => {
                    const isSelected = cb.querySelector('label[data-selected="true"], label[data-checked="true"], div[data-baseweb="checkbox"][aria-checked="true"], input[type="checkbox"]:checked');
                    const box = cb.querySelector('[class*="e1e6q2zh4"], div[data-baseweb="checkbox"] > div, label > div:first-of-type');
                    const polyline = cb.querySelector('svg polyline');
                    const toggleTrack = cb.querySelector('[class*="e1e6q2zh5"], div[role="switch"]');
                    const toggleThumb = cb.querySelector('[class*="e1e6q2zh6"], div[role="switch"] > div');

                    cb.querySelectorAll('label, p, span, div[data-testid="stWidgetLabel"] *').forEach(el => {
                        el.style.setProperty('color', '#1e293b', 'important');
                    });
                    if (box && !toggleTrack) {
                        if (isSelected) {
                            box.style.setProperty('background-color', '#0284c7', 'important');
                            box.style.setProperty('background', '#0284c7', 'important');
                            box.style.setProperty('border', '1.5px solid #0284c7', 'important');
                            if (polyline) polyline.style.setProperty('stroke', '#ffffff', 'important');
                        } else {
                            box.style.setProperty('background-color', '#ffffff', 'important');
                            box.style.setProperty('background', '#ffffff', 'important');
                            box.style.setProperty('border', '1.5px solid #cbd5e1', 'important');
                        }
                    }
                    if (toggleTrack) {
                        toggleTrack.style.setProperty('background-color', isSelected ? '#0284c7' : '#cbd5e1', 'important');
                        toggleTrack.style.setProperty('background', isSelected ? '#0284c7' : '#cbd5e1', 'important');
                    }
                    if (toggleThumb) {
                        toggleThumb.style.setProperty('background-color', '#ffffff', 'important');
                        toggleThumb.style.setProperty('background', '#ffffff', 'important');
                    }
                });

                // 8. Radio 单选框 ── 浅色模式适配（彻底消除黑色实心圆）
                pDoc.querySelectorAll('div[data-testid="stRadio"]').forEach(rg => {
                    rg.querySelectorAll('div[data-testid="stWidgetLabel"] p, div[data-testid="stWidgetLabel"] label, div[data-testid="stWidgetLabel"] *').forEach(el => {
                        el.style.setProperty('color', '#334155', 'important');
                    });
                });
                pDoc.querySelectorAll('label[data-testid="stRadioOption"], div[data-testid="stRadioOption"], div[data-baseweb="radio"]').forEach(opt => {
                    const isSelected = opt.hasAttribute('data-selected') || opt.getAttribute('data-selected') === 'true' || opt.getAttribute('aria-checked') === 'true' || opt.querySelector('[data-selected="true"], input[type="radio"]:checked');
                    const outerCircle = opt.querySelector('[class*="etak9234"], label > div > div > div:first-child, div[data-baseweb="radio"] > div');
                    const innerDot = opt.querySelector('[class*="etak9235"], label > div > div > div:first-child > div, div[data-baseweb="radio"] > div > div');

                    opt.querySelectorAll('label, p, span').forEach(el => el.style.setProperty('color', '#1e293b', 'important'));
                    if (outerCircle) {
                        if (isSelected) {
                            outerCircle.style.setProperty('background-color', '#0284c7', 'important');
                            outerCircle.style.setProperty('background', '#0284c7', 'important');
                            outerCircle.style.setProperty('border', '1.5px solid #0284c7', 'important');
                        } else {
                            outerCircle.style.setProperty('background-color', '#ffffff', 'important');
                            outerCircle.style.setProperty('background', '#ffffff', 'important');
                            outerCircle.style.setProperty('border', '1.5px solid #cbd5e1', 'important');
                        }
                    }
                    if (innerDot) {
                        innerDot.style.setProperty('background-color', '#ffffff', 'important');
                        innerDot.style.setProperty('background', '#ffffff', 'important');
                    }
                });
                // FormSubmitButton 次要按钮浅色模式适配
                pDoc.querySelectorAll(
                    'div[data-testid="stFormSubmitButton"] button[kind="secondaryFormSubmit"], ' +
                    'div[data-testid="stFormSubmitButton"] button[data-testid="stBaseButton-secondaryFormSubmit"], ' +
                    'div[data-testid="stFormSubmitButton"] button:not([data-testid="stBaseButton-primary"]):not([data-testid="stBaseButton-primaryFormSubmit"])'
                ).forEach(btn => {
                    btn.style.setProperty('background-color', '#ffffff', 'important');
                    btn.style.setProperty('background', '#ffffff', 'important');
                    btn.style.setProperty('border', '1px solid #cbd5e1', 'important');
                    btn.style.setProperty('color', '#334155', 'important');
                    btn.style.setProperty('box-shadow', '0 1px 2px rgba(0,0,0,0.04)', 'important');
                });
            } else {
                // 深色模式：还原 stButton / stFormSubmitButton / stLinkButton / stPopover / stCheckbox / stRadio 按钮的内联样式
                pDoc.querySelectorAll(
                    'div[data-testid="stButton"] button, ' +
                    'div[data-testid="stFormSubmitButton"] button, ' +
                    'div[data-testid="stLinkButton"] a, ' +
                    'div[data-testid="stPopover"] button, ' +
                    'div[data-testid="stTooltipIcon"] button'
                ).forEach(el => {
                    el.style.removeProperty('background-color');
                    el.style.removeProperty('background');
                    el.style.removeProperty('border');
                    el.style.removeProperty('color');
                    el.style.removeProperty('box-shadow');
                });
                pDoc.querySelectorAll('div[data-testid="stCheckbox"]').forEach(cb => {
                    cb.querySelectorAll('label, p, span, div[data-testid="stWidgetLabel"] *').forEach(el => el.style.removeProperty('color'));
                    const box = cb.querySelector('[class*="e1e6q2zh4"], div[data-baseweb="checkbox"] > div, label > div:first-of-type');
                    if (box) {
                        box.style.removeProperty('background-color');
                        box.style.removeProperty('background');
                        box.style.removeProperty('border');
                    }
                    const polyline = cb.querySelector('svg polyline');
                    if (polyline) polyline.style.removeProperty('stroke');
                    const toggleTrack = cb.querySelector('[class*="e1e6q2zh5"], div[role="switch"]');
                    if (toggleTrack) {
                        toggleTrack.style.removeProperty('background-color');
                        toggleTrack.style.removeProperty('background');
                    }
                    const toggleThumb = cb.querySelector('[class*="e1e6q2zh6"], div[role="switch"] > div');
                    if (toggleThumb) toggleThumb.style.removeProperty('background-color');
                });
                pDoc.querySelectorAll('div[data-testid="stRadio"] div[data-testid="stWidgetLabel"] *').forEach(el => el.style.removeProperty('color'));
                pDoc.querySelectorAll('label[data-testid="stRadioOption"], div[data-testid="stRadioOption"], div[data-baseweb="radio"]').forEach(opt => {
                    opt.querySelectorAll('label, p, span').forEach(el => el.style.removeProperty('color'));
                    const outerCircle = opt.querySelector('[class*="etak9234"], label > div > div > div:first-child, div[data-baseweb="radio"] > div');
                    if (outerCircle) {
                        outerCircle.style.removeProperty('background-color');
                        outerCircle.style.removeProperty('background');
                        outerCircle.style.removeProperty('border');
                    }
                    const innerDot = opt.querySelector('[class*="etak9235"], label > div > div > div:first-child > div, div[data-baseweb="radio"] > div > div');
                    if (innerDot) innerDot.style.removeProperty('background-color');
                });
            }

            // 列间分割线（居中与明暗模式适配）
            pDoc.querySelectorAll(
                'div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .card-title-row)'
            ).forEach(row => {
                row.style.setProperty('gap', '0', 'important');
                const cols = row.querySelectorAll(':scope > div[data-testid="stColumn"]');
                cols.forEach((col, idx) => {
                    if (idx < cols.length - 1) {
                        col.style.setProperty('border-right', isLight ? '1px solid rgba(15, 23, 42, 0.14)' : '1px solid rgba(255, 255, 255, 0.12)', 'important');
                        col.style.setProperty('padding-right', '18px', 'important');
                    }
                    if (idx > 0) {
                        col.style.setProperty('padding-left', '18px', 'important');
                    }
                });
            });
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
                .header-db-meta-bar { color: #64748b !important; }
                .header-db-meta-bar .meta-item { color: #64748b !important; }
                .header-db-meta-bar .meta-val { color: #0f172a !important; font-weight: 600 !important; }
                .header-db-meta-bar .meta-val-plain { color: #334155 !important; font-weight: 500 !important; }
                .header-db-meta-bar .meta-val-warn { color: #0f172a !important; font-weight: 600 !important; }
                .header-db-meta-bar .meta-divider { background: transparent !important; color: rgba(0, 0, 0, 0.2) !important; width: auto !important; height: auto !important; }

                /* 顶部右上角视图与主题设置 Popover 按钮 (浅色模式状态) */
                html[data-theme="light"] .header-settings-fixed-popover > button,
                html[data-theme="light"] div[data-testid="stElementContainer"]:has(> div > .header-settings-popover-marker) + div[data-testid="stElementContainer"] > div[data-testid="stPopover"]:not(div[data-testid="stHorizontalBlock"] *) > button,
                body.theme-light .header-settings-fixed-popover > button,
                body.theme-light div[data-testid="stElementContainer"]:has(> div > .header-settings-popover-marker) + div[data-testid="stElementContainer"] > div[data-testid="stPopover"]:not(div[data-testid="stHorizontalBlock"] *) > button {
                    background: #ffffff !important;
                    border: 1px solid #cbd5e1 !important;
                    color: #1e293b !important;
                    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08) !important;
                }
                html[data-theme="light"] .header-settings-fixed-popover > button:hover,
                html[data-theme="light"] div[data-testid="stElementContainer"]:has(> div > .header-settings-popover-marker) + div[data-testid="stElementContainer"] > div[data-testid="stPopover"]:not(div[data-testid="stHorizontalBlock"] *) > button:hover,
                body.theme-light .header-settings-fixed-popover > button:hover,
                body.theme-light div[data-testid="stElementContainer"]:has(> div > .header-settings-popover-marker) + div[data-testid="stElementContainer"] > div[data-testid="stPopover"]:not(div[data-testid="stHorizontalBlock"] *) > button:hover {
                    background: #f0f9ff !important;
                    border-color: #0284c7 !important;
                    color: #0284c7 !important;
                    box-shadow: 0 2px 10px rgba(2, 132, 199, 0.25) !important;
                    transform: rotate(45deg) scale(1.08) !important;
                }
                html[data-theme="light"] .header-settings-fixed-popover > button p,
                html[data-theme="light"] div[data-testid="stElementContainer"]:has(> div > .header-settings-popover-marker) + div[data-testid="stElementContainer"] > div[data-testid="stPopover"]:not(div[data-testid="stHorizontalBlock"] *) > button p,
                body.theme-light .header-settings-fixed-popover > button p,
                body.theme-light div[data-testid="stElementContainer"]:has(> div > .header-settings-popover-marker) + div[data-testid="stElementContainer"] > div[data-testid="stPopover"]:not(div[data-testid="stHorizontalBlock"] *) > button p {
                    color: #334155 !important;
                }

                /* 浅色模式彻底隐藏所有 Popover 按钮倒三角并强制居中 */
                button[data-testid="stPopoverButton"] div[aria-hidden="true"],
                button[data-testid="stPopoverButton"] [data-testid="stIconMaterial"],
                button[data-testid="stPopoverButton"] > div > div:last-child:not(:first-child),
                div[data-testid="stPopover"]:not(div[data-testid="stTooltipIcon"] *) button div[aria-hidden="true"],
                div[data-testid="stPopover"]:not(div[data-testid="stTooltipIcon"] *) button [data-testid="stIconMaterial"],
                div[data-testid="stPopover"]:not(div[data-testid="stTooltipIcon"] *) button > div > div:last-child:not(:first-child) {
                    display: none !important;
                    width: 0 !important;
                    height: 0 !important;
                    visibility: hidden !important;
                    opacity: 0 !important;
                }
                button[data-testid="stPopoverButton"] > div,
                div[data-testid="stPopover"]:not(div[data-testid="stTooltipIcon"] *) button > div {
                    display: flex !important;
                    align-items: center !important;
                    justify-content: center !important;
                    width: 100% !important;
                    height: 100% !important;
                    margin: 0 auto !important;
                    text-align: center !important;
                }
                html[data-theme="light"] div[data-testid="stPopoverBody"]:has(.settings-popover-panel),
                body.theme-light div[data-testid="stPopoverBody"]:has(.settings-popover-panel) {
                    background: #ffffff !important;
                    border: 1px solid #e2e8f0 !important;
                    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.08) !important;
                }
                html[data-theme="light"] .settings-panel-header,
                body.theme-light .settings-panel-header {
                    color: #0f172a !important;
                    border-bottom: 1px solid #f1f5f9 !important;
                }
                html[data-theme="light"] .settings-section-title,
                body.theme-light .settings-section-title {
                    color: #64748b !important;
                }
                html[data-theme="light"] .settings-theme-btn-group,
                body.theme-light .settings-theme-btn-group {
                    background: #f1f5f9 !important;
                    border: 1px solid #e2e8f0 !important;
                }
                html[data-theme="light"] .settings-theme-btn,
                body.theme-light .settings-theme-btn {
                    background: transparent !important;
                    border: none !important;
                    color: #64748b !important;
                }
                html[data-theme="light"] .settings-theme-btn:hover,
                body.theme-light .settings-theme-btn:hover {
                    color: #0f172a !important;
                    background: rgba(0, 0, 0, 0.04) !important;
                }
                html[data-theme="light"] .settings-theme-btn.is-active,
                body.theme-light .settings-theme-btn.is-active,
                html[data-theme="light"] .settings-theme-btn.theme-btn-light,
                body.theme-light .settings-theme-btn.theme-btn-light,
                body[data-theme="light"] .settings-theme-btn.theme-btn-light,
                .stApp[data-theme="light"] .settings-theme-btn.theme-btn-light {
                    background: #ffffff !important;
                    color: #0f172a !important;
                    font-weight: 600 !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08) !important;
                }
                /* 浅色模式下深色按钮明确为未选中透明状态 */
                html[data-theme="light"] .settings-theme-btn.theme-btn-dark,
                body.theme-light .settings-theme-btn.theme-btn-dark,
                body[data-theme="light"] .settings-theme-btn.theme-btn-dark,
                .stApp[data-theme="light"] .settings-theme-btn.theme-btn-dark {
                    background: transparent !important;
                    color: #64748b !important;
                    font-weight: 500 !important;
                    box-shadow: none !important;
                }

                /* 浅色模式 Popover 内部下拉框箭头居中保障 */
                div[data-testid="stPopoverBody"] div[data-baseweb="select"] button,
                div[data-testid="stPopoverBody"] div[data-testid="stSelectbox"] button {
                    background: transparent !important;
                    background-color: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
                    padding: 0 !important;
                    min-height: unset !important;
                    height: auto !important;
                    max-height: none !important;
                    display: inline-flex !important;
                    align-items: center !important;
                    justify-content: center !important;
                }
                html[data-theme="light"] .settings-divider,
                body.theme-light .settings-divider {
                    background: #f1f5f9 !important;
                }

                /* 顶层主 Tab 导航 ── 浅色模式（极简分段控制条风格） */
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"],
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div[data-baseweb="tab-list"],
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tablist"] {
                    background: rgba(0, 0, 0, 0.04) !important;
                    border: 1px solid rgba(0, 0, 0, 0.08) !important;
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04) !important;
                }
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"],
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div[data-baseweb="tab-list"] > div[data-baseweb="tab"],
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tab"] {
                    color: #64748b !important;
                    background: transparent !important;
                }
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"]:hover,
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div[data-baseweb="tab-list"] > div[data-baseweb="tab"]:hover,
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tab"]:hover {
                    background: rgba(0, 0, 0, 0.04) !important;
                    color: #0f172a !important;
                }
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"][aria-selected="true"],
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div[data-baseweb="tab-list"] > div[data-baseweb="tab"][aria-selected="true"],
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tab"][aria-selected="true"] {
                    background: #ffffff !important;
                    color: #0f172a !important;
                    border: none !important;
                    outline: none !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08) !important;
                }
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"][aria-selected="true"] p,
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [aria-selected="true"] p {
                    color: #0f172a !important;
                    font-weight: 600 !important;
                }
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) > div > div[role="tablist"] > div[data-testid="stTab"][aria-selected="false"] p,
                html[data-theme="light"] div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [aria-selected="false"] p {
                    color: #64748b !important;
                    font-weight: 500 !important;
                }

                /* 运维中心内嵌二级 Tab ── 浅色模式（iOS/macOS 现代分段控制风格） */
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab-list"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tablist"] {
                    background: #f1f5f9 !important;
                    border: 1px solid #e2e8f0 !important;
                    border-radius: 8px !important;
                    padding: 3px !important;
                    gap: 2px !important;
                    box-shadow: none !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tab"] {
                    background: transparent !important;
                    color: #64748b !important;
                    border: none !important;
                    outline: none !important;
                    border-radius: 6px !important;
                    transition: all 0.15s ease !important;
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
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="true"] {
                    background: #ffffff !important;
                    color: #0f172a !important;
                    border: none !important;
                    outline: none !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08) !important;
                    font-weight: 600 !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"][aria-selected="true"] p,
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="true"] p {
                    color: #0f172a !important;
                    font-weight: 600 !important;
                    background: transparent !important;
                    border: none !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="false"] p,
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"][aria-selected="false"] p {
                    color: #64748b !important;
                    background: transparent !important;
                    border: none !important;
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

                /* React Aria 下拉框 (Streamlit >= 1.55 使用 data-rac 而非 data-baseweb) ── 浅色模式彻底消除黑色小方块 */
                div[data-testid="stSelectbox"] div[data-rac][role="group"] {
                    background-color: #ffffff !important;
                    background: #ffffff !important;
                    border: 1px solid #cbd5e1 !important;
                    color: #0f172a !important;
                    box-shadow: none !important;
                }
                div[data-testid="stSelectbox"] div[data-rac][role="group"] > div,
                div[data-testid="stSelectbox"] div[data-rac][role="group"] > div * {
                    background-color: transparent !important;
                    background: transparent !important;
                }
                div[data-testid="stSelectbox"] div[data-rac][role="group"] input {
                    background-color: #ffffff !important;
                    background: #ffffff !important;
                    color: #0f172a !important;
                }
                div[data-testid="stSelectbox"] div[data-rac][role="group"] input::placeholder {
                    color: #94a3b8 !important;
                }
                div[data-testid="stSelectbox"] div[data-rac][role="group"] button {
                    background: transparent !important;
                    background-color: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
                    color: #64748b !important;
                }
                div[data-testid="stSelectbox"] div[data-rac][role="group"] svg {
                    fill: #64748b !important;
                    color: #64748b !important;
                }
                /* React Aria 下拉弹出层 ── 浅色模式 */
                div[data-testid="stSelectboxVirtualDropdown"],
                div[data-testid="stSelectboxVirtualDropdown"] * {
                    background-color: #ffffff !important;
                    background: #ffffff !important;
                }
                div[data-testid="stSelectboxVirtualDropdown"] {
                    border: 1px solid #e2e8f0 !important;
                    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.1) !important;
                }
                div[data-testid="stSelectboxVirtualDropdown"] * {
                    color: #1e293b !important;
                }
                div[data-testid="stSelectboxVirtualDropdown"] [role="option"]:hover,
                div[data-testid="stSelectboxVirtualDropdown"] [role="option"]:hover *,
                div[data-testid="stSelectboxVirtualDropdown"] [role="option"][aria-selected="true"],
                div[data-testid="stSelectboxVirtualDropdown"] [role="option"][aria-selected="true"] * {
                    background-color: #f1f5f9 !important;
                    color: #0284c7 !important;
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
                    background: #f8fafc !important;
                    border: 1px solid #e2e8f0 !important;
                    color: #64748b !important;
                }
                .toolbar-stat-badge .stat-num { color: #0f172a !important; font-weight: 600 !important; }
                .toolbar-stat-badge .stat-page { color: #334155 !important; font-weight: 500 !important; }

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

                /* ================= 复选框 (Checkbox) 浅色模式完整适配 ================= */
                div[data-testid="stCheckbox"] label,
                div[data-testid="stCheckbox"] label *,
                div[data-testid="stCheckbox"] [data-testid="stWidgetLabel"] *,
                div[data-baseweb="checkbox"] label,
                div[data-baseweb="checkbox"] label * {
                    color: #1e293b !important;
                }

                /* 未选中状态复选框方块 (白底 + 浅灰边框，彻底消除黑色方块) */
                div[data-testid="stCheckbox"] label:not([data-selected="true"]):not([data-checked="true"]) > div:first-of-type,
                div[data-testid="stCheckbox"] label:not([data-selected="true"]):not([data-checked="true"]) [class*="e1e6q2zh4"],
                div[data-testid="stCheckbox"] div[data-baseweb="checkbox"]:not([aria-checked="true"]) > div:first-child,
                div[data-testid="stCheckbox"] input[type="checkbox"]:not(:checked) + div {
                    background-color: #ffffff !important;
                    background: #ffffff !important;
                    border: 1.5px solid #cbd5e1 !important;
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04) !important;
                }

                /* 未选中复选框 Hover 状态 */
                div[data-testid="stCheckbox"] label:hover:not([data-selected="true"]):not([data-checked="true"]) > div:first-of-type,
                div[data-testid="stCheckbox"] label:hover:not([data-selected="true"]):not([data-checked="true"]) [class*="e1e6q2zh4"] {
                    border-color: #0284c7 !important;
                    background-color: #f0f9ff !important;
                    background: #f0f9ff !important;
                }

                /* 选中状态复选框方块 (天蓝底色 + 白色边框) */
                div[data-testid="stCheckbox"] label[data-selected="true"] > div:first-of-type,
                div[data-testid="stCheckbox"] label[data-checked="true"] > div:first-of-type,
                div[data-testid="stCheckbox"] label[data-selected="true"] [class*="e1e6q2zh4"],
                div[data-testid="stCheckbox"] div[data-baseweb="checkbox"][aria-checked="true"] > div:first-child,
                div[data-testid="stCheckbox"] input[type="checkbox"]:checked + div {
                    background-color: #0284c7 !important;
                    background: #0284c7 !important;
                    border: 1.5px solid #0284c7 !important;
                    box-shadow: 0 1px 3px rgba(2, 132, 199, 0.25) !important;
                }

                /* 选中复选框内的对勾 SVG 图标（严格限定在复选框方块内部，坚决不误伤 Tooltip 等其他 SVG） */
                div[data-testid="stCheckbox"] label[data-selected="true"] > div:first-of-type svg,
                div[data-testid="stCheckbox"] label[data-selected="true"] [class*="e1e6q2zh4"] svg,
                div[data-testid="stCheckbox"] label[data-checked="true"] > div:first-of-type svg,
                div[data-testid="stCheckbox"] label[data-checked="true"] [class*="e1e6q2zh4"] svg,
                div[data-testid="stCheckbox"] div[data-baseweb="checkbox"][aria-checked="true"] > div:first-child svg,
                div[data-testid="stCheckbox"] label[data-selected="true"] svg polyline,
                div[data-testid="stCheckbox"] label[data-checked="true"] svg polyline,
                div[data-testid="stCheckbox"] div[data-baseweb="checkbox"][aria-checked="true"] svg polyline {
                    stroke: #ffffff !important;
                    color: #ffffff !important;
                    fill: none !important;
                }

                /* ================= 单选框 (Radio Button) 浅色模式完整适配 ================= */
                div[data-testid="stRadio"] label,
                div[data-testid="stRadio"] label *,
                div[data-testid="stRadioOption"] label,
                div[data-testid="stRadioOption"] label *,
                div[data-testid="stRadio"] [data-testid="stWidgetLabel"] *,
                div[data-baseweb="radio"] label,
                div[data-baseweb="radio"] label * {
                    color: #1e293b !important;
                }

                /* 未选中状态单选框 (白底 + 浅灰边框外圈，白色/透明内点，彻底消除黑圆圈) */
                div[data-testid="stRadioOption"]:not([data-selected="true"]) [class*="etak9234"],
                div[data-testid="stRadioOption"]:not([data-selected="true"]) label > div > div > div:first-child,
                div[data-testid="stRadio"] label:not([data-selected="true"]) [class*="etak9234"],
                div[data-testid="stRadio"] div[data-baseweb="radio"]:not([aria-checked="true"]) > div:first-child {
                    background-color: #ffffff !important;
                    background: #ffffff !important;
                    border: 1.5px solid #cbd5e1 !important;
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04) !important;
                }

                /* 未选中状态内部圆点 (消除黑色实心圆) */
                div[data-testid="stRadioOption"]:not([data-selected="true"]) [class*="etak9235"],
                div[data-testid="stRadioOption"]:not([data-selected="true"]) label > div > div > div:first-child > div,
                div[data-testid="stRadio"] label:not([data-selected="true"]) [class*="etak9235"],
                div[data-testid="stRadio"] div[data-baseweb="radio"]:not([aria-checked="true"]) > div:first-child > div {
                    background-color: #ffffff !important;
                    background: #ffffff !important;
                }

                /* 未选中单选框 Hover 状态 */
                div[data-testid="stRadioOption"]:not([data-selected="true"]):hover [class*="etak9234"],
                div[data-testid="stRadioOption"]:not([data-selected="true"]):hover label > div > div > div:first-child,
                div[data-testid="stRadio"] label:not([data-selected="true"]):hover [class*="etak9234"] {
                    border-color: #0284c7 !important;
                }

                /* 选中状态单选框 (天蓝外圈 + 白色同心圆点) */
                div[data-testid="stRadioOption"][data-selected="true"] [class*="etak9234"],
                div[data-testid="stRadioOption"][data-selected="true"] label > div > div > div:first-child,
                div[data-testid="stRadio"] label[data-selected="true"] [class*="etak9234"],
                div[data-testid="stRadio"] div[data-baseweb="radio"][aria-checked="true"] > div:first-child {
                    background-color: #0284c7 !important;
                    background: #0284c7 !important;
                    border: 1.5px solid #0284c7 !important;
                    box-shadow: 0 1px 3px rgba(2, 132, 199, 0.25) !important;
                }

                div[data-testid="stRadioOption"][data-selected="true"] [class*="etak9235"],
                div[data-testid="stRadioOption"][data-selected="true"] label > div > div > div:first-child > div,
                div[data-testid="stRadio"] label[data-selected="true"] [class*="etak9235"],
                div[data-testid="stRadio"] div[data-baseweb="radio"][aria-checked="true"] > div:first-child > div {
                    background-color: #ffffff !important;
                    background: #ffffff !important;
                }

                /* ================= 开关控件 (Toggle Switch) 浅色模式适配 ================= */
                div[data-testid="stCheckbox"] label:not([data-selected="true"]) div[class*="e1e6q2zh5"],
                div[data-testid="stCheckbox"] label:not([data-selected="true"]) div[role="switch"] {
                    background-color: #cbd5e1 !important;
                    background: #cbd5e1 !important;
                }
                div[data-testid="stCheckbox"] label[data-selected="true"] div[class*="e1e6q2zh5"],
                div[data-testid="stCheckbox"] label[data-selected="true"] div[role="switch"] {
                    background-color: #0284c7 !important;
                    background: #0284c7 !important;
                }
                div[data-testid="stCheckbox"] div[class*="e1e6q2zh6"],
                div[data-testid="stCheckbox"] div[role="switch"] > div {
                    background-color: #ffffff !important;
                    background: #ffffff !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.15) !important;
                }

                /* 全局所有次要按钮、链接按钮、Popover 触发按钮（浅色模式统一白底，坚决排除 Tooltip 提示图标） */
                button[data-testid="stBaseButton-secondary"],
                button[kind="secondary"],
                div[data-testid="stButton"] button,
                .stButton > button,
                div[data-testid="stLinkButton"] a,
                .stLinkButton a,
                div[data-testid="stPopover"]:not(div[data-testid="stTooltipIcon"] *) > button:not(div[data-testid="stTooltipIcon"] *),
                div[data-testid="stPopover"]:not(div[data-testid="stTooltipIcon"] *) button:not(div[data-testid="stTooltipIcon"] *),
                .stPopover:not(div[data-testid="stTooltipIcon"] *) button:not(div[data-testid="stTooltipIcon"] *),
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
                    background-color: #f1f5f9 !important;
                    background: #f1f5f9 !important;
                    border-color: #94a3b8 !important;
                    color: #0f172a !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06) !important;
                }
                div[data-testid="stPopover"]:not(div[data-testid="stTooltipIcon"] *) > button:not(div[data-testid="stTooltipIcon"] *):hover,
                div[data-testid="stPopover"]:not(div[data-testid="stTooltipIcon"] *) button:not(div[data-testid="stTooltipIcon"] *):hover,
                .stPopover:not(div[data-testid="stTooltipIcon"] *) button:not(div[data-testid="stTooltipIcon"] *):hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button:hover,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button:hover {
                    background-color: #fef2f2 !important;
                    background: #fef2f2 !important;
                    border-color: #ef4444 !important;
                    color: #ef4444 !important;
                    box-shadow: 0 1px 4px rgba(239, 68, 68, 0.15) !important;
                }
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
                button[data-testid="stBaseButton-secondary"] p,
                div[data-testid="stButton"] button p,
                .stButton > button p,
                div[data-testid="stLinkButton"] a p,
                .stLinkButton a p,
                div[data-testid="stPopover"] button p,
                .stPopover button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"],
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button {
                    position: static !important;
                    top: auto !important;
                    right: auto !important;
                    bottom: auto !important;
                    left: auto !important;
                    z-index: auto !important;
                    transform: none !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) a p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stButton"] button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stLinkButton"] a p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] > button p,
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) div[data-testid="stPopover"] button p {
                    color: inherit !important;
                }
                .grid-row-divider {
                    background: rgba(15, 23, 42, 0.14) !important;
                }

                /* 卡片画廊与内容区域：清除 gap 保持居中，高对比度清晰分割线 */
                div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .card-title-row) {
                    gap: 0 !important;
                }
                div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .card-title-row) > div[data-testid="stColumn"]:not(:last-child) {
                    border-right: 1px solid rgba(15, 23, 42, 0.14) !important;
                    padding-right: 18px !important;
                }
                div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] .card-title-row) > div[data-testid="stColumn"]:not(:first-child) {
                    padding-left: 18px !important;
                }
                .card-title-text {
                    color: #0f172a !important;
                }
                .card-id-badge {
                    background: #f1f5f9 !important;
                    color: #475569 !important;
                    border: 1px solid #e2e8f0 !important;
                }
                .card-meta-row {
                    color: #64748b !important;
                }
                .card-meta-item {
                    color: #64748b !important;
                }
                .card-meta-divider {
                    background: #e2e8f0 !important;
                }
                .badge-source {
                    background-color: #f1f5f9 !important;
                    color: #334155 !important;
                    border: 1px solid #cbd5e1 !important;
                }
                .badge-category {
                    background-color: #f8fafc !important;
                    color: #64748b !important;
                    border: 1px solid #e2e8f0 !important;
                }
                .badge-orphan {
                    background-color: #fffbeb !important;
                    color: #b45309 !important;
                    border: 1px solid #fde68a !important;
                }
                .badge-format {
                    background-color: #f8fafc !important;
                    color: #64748b !important;
                    border: 1px solid #e2e8f0 !important;
                }
                .badge-duplicate {
                    background-color: #fff1f2 !important;
                    color: #e11d48 !important;
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
                    border: 1px solid #e2e8f0 !important;
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

                /* 浅色模式卡片底部两行信息 */
                .card-bottom-line {
                    color: #64748b !important;
                }
                .card-bottom-label {
                    color: #94a3b8 !important;
                }
                .card-bottom-value {
                    color: #334155 !important;
                }
                .card-bottom-link {
                    color: #0284c7 !important;
                }
                .card-bottom-link:hover {
                    color: #0369a1 !important;
                }
                /* 浅色模式底部链接颜色（优先级需高于深色重置规则与按钮胶囊覆盖规则） */
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) .card-bottom-info a.card-bottom-link {
                    color: #0284c7 !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.card-meta-row) .card-bottom-info a.card-bottom-link:hover {
                    color: #0369a1 !important;
                }
                .card-bottom-empty {
                    color: #94a3b8 !important;
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
                button[data-testid="stBaseButton-secondaryFormSubmit"],
                button[kind="secondaryFormSubmit"],
                div[data-testid="stFormSubmitButton"] button:not([data-testid="stBaseButton-primary"]):not([data-testid="stBaseButton-primaryFormSubmit"]):not([kind="primaryFormSubmit"]),
                .stButton > button:not([data-testid="stBaseButton-primary"]) {
                    background: #ffffff !important;
                    color: #1e293b !important;
                    border: 1px solid #cbd5e1 !important;
                    border-radius: 6px !important;
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04) !important;
                    transition: all 0.15s ease !important;
                    height: 30px !important;
                    min-height: 30px !important;
                    max-height: 30px !important;
                    padding: 0 14px !important;
                    font-size: 12px !important;
                    line-height: 28px !important;
                    width: auto !important;
                    min-width: unset !important;
                    display: inline-flex !important;
                    align-items: center !important;
                    justify-content: center !important;
                    white-space: nowrap !important;
                    box-sizing: border-box !important;
                }
                button[data-testid="stBaseButton-secondary"]:not(:disabled):hover,
                button[data-testid="stBaseButton-secondaryFormSubmit"]:not(:disabled):hover,
                div[data-testid="stFormSubmitButton"] button:not([data-testid="stBaseButton-primary"]):not([data-testid="stBaseButton-primaryFormSubmit"]):not([kind="primaryFormSubmit"]):not(:disabled):hover,
                .stButton > button:not([data-testid="stBaseButton-primary"]):not(:disabled):hover {
                    background: #f0f9ff !important;
                    border-color: #0284c7 !important;
                    color: #0284c7 !important;
                    box-shadow: 0 2px 6px rgba(2, 132, 199, 0.15) !important;
                }
                button[data-testid="stBaseButton-secondary"]:disabled,
                button[data-testid="stBaseButton-secondaryFormSubmit"]:disabled,
                div[data-testid="stFormSubmitButton"] button:disabled,
                .stButton > button:not([data-testid="stBaseButton-primary"]):disabled {
                    background: #f8fafc !important;
                    border-color: #e2e8f0 !important;
                    color: #94a3b8 !important;
                    opacity: 0.55 !important;
                    cursor: not-allowed !important;
                }
                button[data-testid="stBaseButton-primary"],
                button[data-testid="stBaseButton-primaryFormSubmit"],
                button[kind="primaryFormSubmit"] {
                    background: linear-gradient(135deg, #0284c7, #0369a1) !important;
                    border: 1px solid #0284c7 !important;
                    color: #ffffff !important;
                    border-radius: 6px !important;
                    box-shadow: 0 2px 6px rgba(2, 132, 199, 0.25) !important;
                    transition: all 0.15s ease !important;
                    height: 30px !important;
                    min-height: 30px !important;
                    padding: 0 16px !important;
                    font-size: 12px !important;
                    display: inline-flex !important;
                    align-items: center !important;
                    justify-content: center !important;
                    white-space: nowrap !important;
                    box-sizing: border-box !important;
                }
                button[data-testid="stBaseButton-primary"]:hover,
                button[data-testid="stBaseButton-primaryFormSubmit"]:hover {
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
                    color: #475569 !important;
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04) !important;
                    transition: all 0.15s ease !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button:not(:disabled):hover {
                    background: #f1f5f9 !important;
                    border-color: #94a3b8 !important;
                    color: #0f172a !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06) !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button:disabled {
                    background: #f8fafc !important;
                    border-color: #e2e8f0 !important;
                    color: #cbd5e1 !important;
                    opacity: 0.45 !important;
                    cursor: not-allowed !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button p {
                    font-size: 15px !important;
                    font-weight: 700 !important;
                    line-height: 1 !important;
                    color: inherit !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stNumberInput"] input {
                    background: #ffffff !important;
                    color: #0f172a !important;
                    border: 1px solid #cbd5e1 !important;
                }

                /* 浅色模式全局加载遮罩微调与全屏遮罩（彻底移除磨砂蒙层） */
                .stApp[data-test-script-state="running"]::after {
                    display: none !important;
                    content: none !important;
                }
                .stApp[data-test-script-state="running"]::before {
                    background: linear-gradient(90deg, rgba(2, 132, 199, 0.2), #0284c7, rgba(2, 132, 199, 0.2)) !important;
                    box-shadow: 0 0 8px rgba(2, 132, 199, 0.2) !important;
                }
                #app-global-loading-hud,
                body.theme-light #app-global-loading-hud,
                body[data-theme="light"] #app-global-loading-hud,
                html[data-theme="light"] #app-global-loading-hud,
                #app-global-loading-hud.hud-theme-light,
                #app-global-loading-hud[data-theme="light"] {
                    background: rgba(255, 255, 255, 0.98) !important;
                    border: 1px solid #e2e8f0 !important;
                    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.08) !important;
                }
                #app-global-loading-hud .hud-spinner-ring,
                body.theme-light #app-global-loading-hud .hud-spinner-ring,
                body[data-theme="light"] #app-global-loading-hud .hud-spinner-ring,
                #app-global-loading-hud.hud-theme-light .hud-spinner-ring,
                #app-global-loading-hud[data-theme="light"] .hud-spinner-ring {
                    border: 2px solid #f1f5f9 !important;
                    border-top-color: #0284c7 !important;
                    border-right-color: rgba(2, 132, 199, 0.5) !important;
                }
                #app-global-loading-hud .hud-sub-text,
                body.theme-light #app-global-loading-hud .hud-sub-text,
                body[data-theme="light"] #app-global-loading-hud .hud-sub-text,
                #app-global-loading-hud.hud-theme-light .hud-sub-text,
                #app-global-loading-hud[data-theme="light"] .hud-sub-text {
                    color: #0f172a !important;
                    font-weight: 500 !important;
                }
                #app-global-loading-hud.is-active,
                body.theme-light #app-global-loading-hud.is-active,
                body[data-theme="light"] #app-global-loading-hud.is-active,
                #app-global-loading-hud.hud-theme-light.is-active {
                    animation: hudPulseGlowLight 2.2s infinite ease-in-out !important;
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
                .header-db-meta-bar { color: #94a3b8 !important; }
                .header-db-meta-bar .meta-item { color: #94a3b8 !important; }
                .header-db-meta-bar .meta-val { color: #f1f5f9 !important; font-weight: 600 !important; }
                .header-db-meta-bar .meta-val-plain { color: #cbd5e1 !important; font-weight: 500 !important; }
                .header-db-meta-bar .meta-val-warn { color: #f1f5f9 !important; font-weight: 600 !important; }
                .header-db-meta-bar .meta-divider { background: transparent !important; color: rgba(255, 255, 255, 0.25) !important; width: auto !important; height: auto !important; }

                /* 顶部右上角视图与主题设置 Popover 按钮 (深色模式状态) */
                .header-settings-fixed-popover > button,
                div[data-testid="stElementContainer"]:has(> div > .header-settings-popover-marker) + div[data-testid="stElementContainer"] > div[data-testid="stPopover"]:not(div[data-testid="stHorizontalBlock"] *) > button {
                    background: rgba(255, 255, 255, 0.08) !important;
                    border: 1px solid rgba(255, 255, 255, 0.15) !important;
                    color: #e2e8f0 !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25) !important;
                }
                .header-settings-fixed-popover > button:hover,
                div[data-testid="stElementContainer"]:has(> div > .header-settings-popover-marker) + div[data-testid="stElementContainer"] > div[data-testid="stPopover"]:not(div[data-testid="stHorizontalBlock"] *) > button:hover {
                    background: rgba(255, 255, 255, 0.14) !important;
                    border-color: rgba(255, 255, 255, 0.25) !important;
                    color: #ffffff !important;
                    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2) !important;
                    transform: none !important;
                }
                div[data-testid="stPopoverBody"]:has(.settings-popover-panel) {
                    background: rgba(15, 23, 42, 0.96) !important;
                    border: 1px solid rgba(255, 255, 255, 0.14) !important;
                }
                /* 深色模式设置按钮激活态 */
                html[data-theme="dark"] .settings-theme-btn.theme-btn-dark,
                body.theme-dark .settings-theme-btn.theme-btn-dark,
                body[data-theme="dark"] .settings-theme-btn.theme-btn-dark,
                .stApp[data-theme="dark"] .settings-theme-btn.theme-btn-dark {
                    background: rgba(255, 255, 255, 0.14) !important;
                    color: #ffffff !important;
                    font-weight: 600 !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25) !important;
                }
                html[data-theme="dark"] .settings-theme-btn.theme-btn-light,
                body.theme-dark .settings-theme-btn.theme-btn-light,
                body[data-theme="dark"] .settings-theme-btn.theme-btn-light,
                .stApp[data-theme="dark"] .settings-theme-btn.theme-btn-light {
                    background: transparent !important;
                    color: #94a3b8 !important;
                    font-weight: 500 !important;
                    box-shadow: none !important;
                }

                /* 深色模式 Popover 内部下拉框箭头居中保障 */
                div[data-testid="stPopoverBody"] div[data-baseweb="select"] button,
                div[data-testid="stPopoverBody"] div[data-testid="stSelectbox"] button {
                    background: transparent !important;
                    background-color: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
                    padding: 0 !important;
                    min-height: unset !important;
                    height: auto !important;
                    max-height: none !important;
                    display: inline-flex !important;
                    align-items: center !important;
                    justify-content: center !important;
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
                    background: rgba(15, 23, 42, 0.75) !important;
                    border: 1px solid rgba(255, 255, 255, 0.08) !important;
                    border-radius: 8px !important;
                    padding: 3px 4px !important;
                    gap: 4px !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tab"] {
                    color: #94a3b8 !important;
                    border: none !important;
                    outline: none !important;
                    background: transparent !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"][aria-selected="true"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [role="tab"][aria-selected="true"],
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="true"] {
                    background: rgba(56, 189, 248, 0.15) !important;
                    color: #38bdf8 !important;
                    border: none !important;
                    outline: none !important;
                    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3) !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="true"] p,
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"][aria-selected="true"] p {
                    color: #38bdf8 !important;
                    font-weight: 600 !important;
                    background: transparent !important;
                    border: none !important;
                }
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [aria-selected="false"] p,
                div[data-testid="stTabPanel"] div[data-testid="stTabs"] [data-testid="stTab"][aria-selected="false"] p {
                    color: #94a3b8 !important;
                    background: transparent !important;
                    border: none !important;
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
                /* React Aria 下拉框 (data-rac) ── 深色模式 */
                div[data-testid="stSelectbox"] div[data-rac][role="group"] {
                    background-color: rgba(30, 41, 59, 0.8) !important;
                    background: rgba(30, 41, 59, 0.8) !important;
                    border: 1px solid rgba(255, 255, 255, 0.12) !important;
                    color: #f1f5f9 !important;
                }
                div[data-testid="stSelectbox"] div[data-rac][role="group"] input {
                    background-color: transparent !important;
                    background: transparent !important;
                    color: #f1f5f9 !important;
                }
                div[data-testid="stSelectbox"] div[data-rac][role="group"] input::placeholder {
                    color: #64748b !important;
                }
                div[data-testid="stSelectbox"] div[data-rac][role="group"] button {
                    background: transparent !important;
                    background-color: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
                    color: #94a3b8 !important;
                }
                div[data-testid="stSelectbox"] div[data-rac][role="group"] svg {
                    fill: #94a3b8 !important;
                    color: #94a3b8 !important;
                }
                /* React Aria 下拉弹出层 ── 深色模式 */
                div[data-testid="stSelectboxVirtualDropdown"] {
                    background-color: #1e293b !important;
                    background: #1e293b !important;
                    border: 1px solid rgba(255, 255, 255, 0.12) !important;
                    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.6) !important;
                }
                div[data-testid="stSelectboxVirtualDropdown"] * {
                    color: #f1f5f9 !important;
                }
                div[data-testid="stSelectboxVirtualDropdown"] [role="option"]:hover,
                div[data-testid="stSelectboxVirtualDropdown"] [role="option"]:hover *,
                div[data-testid="stSelectboxVirtualDropdown"] [role="option"][aria-selected="true"],
                div[data-testid="stSelectboxVirtualDropdown"] [role="option"][aria-selected="true"] * {
                    background-color: rgba(56, 189, 248, 0.15) !important;
                    color: #38bdf8 !important;
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
                .pagination-text { color: #94a3b8 !important; }
                .toolbar-stat-badge {
                    background: rgba(255, 255, 255, 0.04) !important;
                    border: 1px solid rgba(255, 255, 255, 0.1) !important;
                    color: #94a3b8 !important;
                }
                .toolbar-stat-badge .stat-num { color: #f1f5f9 !important; font-weight: 600 !important; }
                .toolbar-stat-badge .stat-page { color: #cbd5e1 !important; font-weight: 500 !important; }
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button {
                    background: rgba(30, 41, 59, 0.8) !important;
                    border: 1px solid rgba(255, 255, 255, 0.14) !important;
                    color: #cbd5e1 !important;
                    transition: all 0.15s ease !important;
                }
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button:not(:disabled):hover {
                    background: rgba(255, 255, 255, 0.12) !important;
                    border-color: rgba(255, 255, 255, 0.28) !important;
                    color: #ffffff !important;
                    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2) !important;
                }
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button:disabled {
                    background: rgba(15, 23, 42, 0.45) !important;
                    border: 1px solid rgba(255, 255, 255, 0.06) !important;
                    color: #475569 !important;
                    opacity: 0.35 !important;
                    cursor: not-allowed !important;
                }
                div[data-testid="stHorizontalBlock"]:has(div[data-testid="stTextInput"]) div[data-testid="stButton"] button p {
                    font-size: 15px !important;
                    line-height: 1 !important;
                    font-weight: 700 !important;
                    color: inherit !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stButton"] button {
                    background: rgba(30, 41, 59, 0.8) !important;
                    border: 1px solid rgba(255, 255, 255, 0.14) !important;
                    color: #cbd5e1 !important;
                    transition: all 0.15s ease !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button:not(:disabled):hover {
                    background: rgba(255, 255, 255, 0.12) !important;
                    border-color: rgba(255, 255, 255, 0.28) !important;
                    color: #ffffff !important;
                    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2) !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button:disabled {
                    background: rgba(15, 23, 42, 0.45) !important;
                    border: 1px solid rgba(255, 255, 255, 0.06) !important;
                    color: #475569 !important;
                    opacity: 0.35 !important;
                    cursor: not-allowed !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) button p {
                    font-size: 15px !important;
                    line-height: 1 !important;
                    font-weight: 700 !important;
                    color: inherit !important;
                }
                div[data-testid="stHorizontalBlock"]:has(.pagination-text) div[data-testid="stNumberInput"] input {
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
                    height: 30px !important;
                    min-height: 30px !important;
                    max-height: 30px !important;
                    padding: 0 14px !important;
                    font-size: 12px !important;
                    line-height: 28px !important;
                    width: auto !important;
                    min-width: unset !important;
                    display: inline-flex !important;
                    align-items: center !important;
                    justify-content: center !important;
                    white-space: nowrap !important;
                    box-sizing: border-box !important;
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
                    height: 30px !important;
                    min-height: 30px !important;
                    padding: 0 16px !important;
                    font-size: 12px !important;
                    display: inline-flex !important;
                    align-items: center !important;
                    justify-content: center !important;
                    white-space: nowrap !important;
                    box-sizing: border-box !important;
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

                /* 深色模式全局加载遮罩微调与全屏遮罩（彻底移除磨砂蒙层） */
                .stApp[data-test-script-state="running"]::after {
                    display: none !important;
                    content: none !important;
                }
                .stApp[data-test-script-state="running"]::before {
                    background: linear-gradient(90deg, rgba(56, 189, 248, 0.2), #38bdf8, rgba(56, 189, 248, 0.2)) !important;
                    box-shadow: 0 0 8px rgba(56, 189, 248, 0.3) !important;
                }
                #app-global-loading-hud,
                body.theme-dark #app-global-loading-hud,
                body[data-theme="dark"] #app-global-loading-hud,
                html[data-theme="dark"] #app-global-loading-hud,
                #app-global-loading-hud.hud-theme-dark,
                #app-global-loading-hud[data-theme="dark"] {
                    background: rgba(15, 23, 42, 0.94) !important;
                    border: 1px solid rgba(255, 255, 255, 0.12) !important;
                    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5) !important;
                }
                #app-global-loading-hud .hud-spinner-ring,
                body.theme-dark #app-global-loading-hud .hud-spinner-ring,
                body[data-theme="dark"] #app-global-loading-hud .hud-spinner-ring,
                #app-global-loading-hud.hud-theme-dark .hud-spinner-ring,
                #app-global-loading-hud[data-theme="dark"] .hud-spinner-ring {
                    border: 2px solid rgba(255, 255, 255, 0.1) !important;
                    border-top-color: #38bdf8 !important;
                    border-right-color: rgba(56, 189, 248, 0.5) !important;
                }
                #app-global-loading-hud .hud-sub-text,
                body.theme-dark #app-global-loading-hud .hud-sub-text,
                body[data-theme="dark"] #app-global-loading-hud .hud-sub-text,
                #app-global-loading-hud.hud-theme-dark .hud-sub-text,
                #app-global-loading-hud[data-theme="dark"] .hud-sub-text {
                    color: #f1f5f9 !important;
                    font-weight: 500 !important;
                }
                #app-global-loading-hud.is-active,
                body.theme-dark #app-global-loading-hud.is-active,
                body[data-theme="dark"] #app-global-loading-hud.is-active,
                #app-global-loading-hud.hud-theme-dark.is-active {
                    animation: hudPulseGlowDark 2.2s infinite ease-in-out !important;
                }

                /* 彻底隐藏所有 Popover 按钮内的倒三角图标容器并居中文字 */
                button[data-testid="stPopoverButton"] div[aria-hidden="true"],
                button[data-testid="stPopoverButton"] [data-testid="stIconMaterial"],
                button[data-testid="stPopoverButton"] > div > div:last-child:not(:first-child),
                div[data-testid="stPopover"] button div[aria-hidden="true"],
                div[data-testid="stPopover"] button [data-testid="stIconMaterial"],
                div[data-testid="stPopover"] button > div > div:last-child:not(:first-child) {
                    display: none !important;
                    width: 0 !important;
                    height: 0 !important;
                    visibility: hidden !important;
                    opacity: 0 !important;
                }
                button[data-testid="stPopoverButton"] > div,
                div[data-testid="stPopover"] button > div {
                    display: flex !important;
                    align-items: center !important;
                    justify-content: center !important;
                    width: 100% !important;
                    height: 100% !important;
                    margin: 0 auto !important;
                    text-align: center !important;
                }

                /* 深色模式页面过渡动画 */
                html, body, .stApp {
                    transition: background-color 0.25s ease, color 0.25s ease !important;
                }
            `;
        }

        // 同步设置面板中的深浅色模式按钮激活态
        pDoc.querySelectorAll('.settings-theme-btn').forEach(function(btn) {
            if (btn.classList.contains('theme-btn-dark')) {
                btn.classList.toggle('is-active', currentTheme !== 'light');
            } else if (btn.classList.contains('theme-btn-light')) {
                btn.classList.toggle('is-active', currentTheme === 'light');
            }
        });

        const btn = pDoc.getElementById('header-theme-icon-btn');
        if (btn) {
            btn.style.display = 'none';
        }
    } catch (e) {}
}

// 动态高频清除 Popover 按钮倒三角并居中文字
function cleanAllPopoverButtons() {
    try {
        const pDoc = (window.parent && window.parent.document) || document;
        pDoc.querySelectorAll('button[data-testid="stPopoverButton"], div[data-testid="stPopover"] button').forEach(function(btn) {
            // 隐藏倒三角容器
            const arrowDiv = btn.querySelector('div[aria-hidden="true"]') || btn.querySelector('[data-testid="stIconMaterial"]') || (btn.firstElementChild && btn.firstElementChild.children && btn.firstElementChild.children[1]);
            if (arrowDiv) {
                arrowDiv.style.setProperty('display', 'none', 'important');
                arrowDiv.style.setProperty('width', '0px', 'important');
                arrowDiv.style.setProperty('height', '0px', 'important');
                arrowDiv.style.setProperty('visibility', 'hidden', 'important');
                arrowDiv.style.setProperty('opacity', '0', 'important');
            }
            if (btn.firstElementChild) {
                btn.firstElementChild.style.setProperty('display', 'flex', 'important');
                btn.firstElementChild.style.setProperty('align-items', 'center', 'important');
                btn.firstElementChild.style.setProperty('justify-content', 'center', 'important');
                btn.firstElementChild.style.setProperty('width', '100%', 'important');
                btn.firstElementChild.style.setProperty('height', '100%', 'important');
                btn.firstElementChild.style.setProperty('margin', '0 auto', 'important');
            }
            const markdownDiv = btn.querySelector('div[data-testid="stMarkdownContainer"]') || (btn.firstElementChild && btn.firstElementChild.children && btn.firstElementChild.children[0]);
            if (markdownDiv) {
                markdownDiv.style.setProperty('display', 'flex', 'important');
                markdownDiv.style.setProperty('align-items', 'center', 'important');
                markdownDiv.style.setProperty('justify-content', 'center', 'important');
                markdownDiv.style.setProperty('margin', '0 auto', 'important');
                markdownDiv.style.setProperty('text-align', 'center', 'important');
            }
        });
    } catch(e) {}
}

setInterval(cleanAllPopoverButtons, 100);

try {
    const pWin = window.parent || window;
    const pDoc = (window.parent && window.parent.document) || document;
    pWin._setTheme = pDoc._setTheme = window._setTheme = document._setTheme = function(theme) {
        applyAppTheme(theme);
    };
    pWin._toggleTheme = pDoc._toggleTheme = window._toggleTheme = document._toggleTheme = function() {
        const cur = localStorage.getItem('viewer_theme') || 'dark';
        applyAppTheme(cur === 'dark' ? 'light' : 'dark');
    };

    if (!pDoc._themeEventsBound) {
        pDoc._themeEventsBound = true;
        pDoc.addEventListener('click', function(e) {
            const btn = e.target.closest && e.target.closest('.settings-theme-btn');
            if (!btn) return;
            e.preventDefault();
            e.stopPropagation();
            if (btn.classList.contains('theme-btn-dark')) {
                applyAppTheme('dark');
            } else if (btn.classList.contains('theme-btn-light')) {
                applyAppTheme('light');
            }
        }, true);
    }

    if (!pDoc._popoverEventsBound) {
        pDoc._popoverEventsBound = true;
        pDoc.addEventListener('click', function(e) {
            const popBtn = e.target.closest && e.target.closest('button[data-testid="stPopoverButton"], div[data-testid="stPopover"] button');
            if (popBtn) {
                const triggerSync = function() {
                    try {
                        const curTheme = (pDoc.documentElement && pDoc.documentElement.getAttribute('data-theme'))
                            || (pDoc.body && pDoc.body.getAttribute('data-theme'))
                            || localStorage.getItem('viewer_theme') || 'dark';
                        if (typeof applyAppTheme === 'function') applyAppTheme(curTheme);
                        if (pDoc._forceWidgetStyles) pDoc._forceWidgetStyles(curTheme);
                        if (typeof applyWidgetFix === 'function') applyWidgetFix();
                    } catch(err) {}
                };
                requestAnimationFrame(triggerSync);
                setTimeout(triggerSync, 15);
                setTimeout(triggerSync, 60);
                setTimeout(triggerSync, 150);
            }
        }, true);
    }
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

        // 移除旧品牌标识（若存在）
        const existingBrand = pDoc.getElementById('header-platform-brand');
        if (existingBrand) existingBrand.remove();

        // 2. 数据库统计元数据条
        const metaHtml = iconStripped(`
            <span class="meta-item">库 <b class="meta-val-plain">${metaData.dbName}</b></span>
            <span class="meta-divider">·</span>
            <span class="meta-item">总记录 <b class="meta-val">${metaData.total}</b></span>
            <span class="meta-divider">·</span>
            <span class="meta-item">PDF归档 <b class="meta-val">${metaData.hasPdf}</b></span>
            <span class="meta-divider">·</span>
            <span class="meta-item">渠道 <b class="meta-val">${metaData.sources}</b></span>
            <span class="meta-divider">·</span>
            <span class="meta-item">分类 <b class="meta-val">${metaData.categories}</b></span>
        `);

        let metaBar = pDoc.getElementById('header-db-metadata-bar');
        if (!metaBar) {
            metaBar = pDoc.createElement('div');
            metaBar.id = 'header-db-metadata-bar';
            metaBar.className = 'header-db-meta-bar';
            header.appendChild(metaBar);
        }
        metaBar.innerHTML = metaHtml;

        // 动态校准左侧主 Tab 占位宽度，确保元数据条紧随其后且永不重叠
        const topTabList = pDoc.querySelector('div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tablist"], div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-baseweb="tab-list"]');
        if (topTabList) {
            const rect = topTabList.getBoundingClientRect();
            if (rect.width > 50 && rect.width < 800) {
                metaBar.style.setProperty('left', Math.ceil(rect.left + rect.width + 12) + 'px', 'important');
            }
            if (!topTabList._metaBarObserverAttached) {
                topTabList._metaBarObserverAttached = true;
                try {
                    const ro = new ResizeObserver(() => {
                        const r = topTabList.getBoundingClientRect();
                        if (r.width > 50 && r.width < 800 && metaBar) {
                            metaBar.style.setProperty('left', Math.ceil(r.left + r.width + 12) + 'px', 'important');
                        }
                    });
                    ro.observe(topTabList);
                } catch(e) {}
            }
        }
        if (!pDoc._metaBarResizeAttached) {
            pDoc._metaBarResizeAttached = true;
            try {
                (pDoc.defaultView || window).addEventListener('resize', () => {
                    const tl = pDoc.querySelector('div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [role="tablist"], div[data-testid="stTabs"]:not(div[data-testid="stTabPanel"] div[data-testid="stTabs"]) [data-baseweb="tab-list"]');
                    const mb = pDoc.getElementById('header-db-metadata-bar');
                    if (tl && mb) {
                        const r = tl.getBoundingClientRect();
                        if (r.width > 50 && r.width < 800) {
                            mb.style.setProperty('left', Math.ceil(r.left + r.width + 12) + 'px', 'important');
                        }
                    }
                });
            } catch(e) {}
        }

        // 隐藏旧的独立主题按钮
        const oldThemeBtn = pDoc.getElementById('header-theme-icon-btn');
        if (oldThemeBtn) {
            oldThemeBtn.style.display = 'none';
        }

        // 标记右上角设置 Popover 专属 class，确保与卡片内删除 Popover 物理隔离
        const marker = pDoc.querySelector('.header-settings-popover-marker');
        if (marker) {
            const container = marker.closest('[data-testid="stElementContainer"]');
            if (container && container.nextElementSibling) {
                const pop = container.nextElementSibling.querySelector('[data-testid="stPopover"]');
                if (pop && !pop.classList.contains('header-settings-fixed-popover')) {
                    pop.classList.add('header-settings-fixed-popover');
                }
            }
        }

        // 彻底隐藏所有 Popover 按钮内的倒三角 expand_more 图标，并居中按钮文字
        pDoc.querySelectorAll('button[data-testid="stPopoverButton"], [data-testid="stPopover"] button').forEach(function(btn) {
            btn.querySelectorAll('[data-testid="stIconMaterial"], div[aria-hidden="true"], [class*="el0gnfx2"], [class*="ewh6kot2"]').forEach(function(icon) {
                icon.style.setProperty('display', 'none', 'important');
                icon.style.setProperty('width', '0px', 'important');
                icon.style.setProperty('height', '0px', 'important');
                icon.style.setProperty('visibility', 'hidden', 'important');
                icon.style.setProperty('opacity', '0', 'important');
            });
            if (btn.firstElementChild) {
                btn.firstElementChild.style.setProperty('justify-content', 'center', 'important');
                btn.firstElementChild.style.setProperty('width', '100%', 'important');
            }
        });

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
            if (pDoc._forceWidgetStyles) {
                pDoc._forceWidgetStyles(theme);
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
        const hudContent = [
            '<div class="hud-spinner-ring"></div>',
            '<div class="hud-text-box">',
            '    <div class="hud-sub-text">数据检索与视图渲染中，请稍候</div>',
            '</div>'
        ].join('');

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

        let safetyClearTimer = null;

        function checkStreamlitRunning() {
            const stApp = pDoc.querySelector('.stApp, [data-testid="stApp"], [data-test-script-state]');
            if (stApp && stApp.getAttribute('data-test-script-state') === 'running') {
                return true;
            }
            return false;
        }

        function checkContentReady() {
            // 当主视觉内容（卡片标题、PDF预览容器或表格）已就绪渲染到 DOM 中时即刻视为就绪
            return !!pDoc.querySelector('.card-title-row, .pdf-scroll-container, div[data-testid="stDataFrame"], .pdf-empty-placeholder');
        }

        function updateLoadingState(isRunning, isPending) {
            if (!hud) return;
            if (safetyClearTimer) {
                clearTimeout(safetyClearTimer);
                safetyClearTimer = null;
            }

            // 如果主内容已经呈现，立即隐藏 HUD，绝不遮挡视野
            if (checkContentReady() && !isPending) {
                hud.classList.remove('is-active');
                return;
            }

            if (isRunning) {
                hud.classList.add('is-active');
                if (isPending) {
                    safetyClearTimer = setTimeout(() => {
                        if (!checkStreamlitRunning() || checkContentReady()) {
                            hud.classList.remove('is-active');
                        }
                    }, 500);
                } else {
                    safetyClearTimer = setTimeout(() => {
                        if (!checkStreamlitRunning() || checkContentReady()) {
                            hud.classList.remove('is-active');
                        }
                    }, 6000);
                }
            } else {
                hud.classList.remove('is-active');
            }
        }

        // 2. 监听 Streamlit 根容器的 running 状态属性与 DOM 内容生成
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
                    if (checkContentReady()) {
                        hud.classList.remove('is-active');
                    }
                });
                window._stLoadingObserver.observe(stApp, { attributes: true, attributeFilter: ['data-test-script-state'], childList: true, subtree: true });
            }
        } else {
            updateLoadingState(false);
        }

        // 3. 安全的即时交互预唤起（仅在输入框按回车搜索时预激活，绝不拦截下拉框展开、Tab切换及普通按钮）
        if (!pDoc._loadingEventsBound) {
            pDoc._loadingEventsBound = true;
            pDoc.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    const input = e.target.closest('input');
                    if (input && !e.target.closest('[data-baseweb="select"]')) {
                        updateLoadingState(true, true);
                    }
                }
            }, true);
        }

        // 4. 定时兜底状态校准：若 Streamlit 处于 idle 状态或主内容已就绪，确保移除 is-active 遮罩
        if (!window._stLoadingInterval) {
            window._stLoadingInterval = setInterval(() => {
                if (hud && hud.classList.contains('is-active')) {
                    if (!checkStreamlitRunning() || checkContentReady()) {
                        hud.classList.remove('is-active');
                    }
                }
            }, 150);
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

                // 3. BaseWeb Select 内所有 button（Chevron 按钮）→ 透明并重置尺寸以垂直居中
                pDoc.querySelectorAll('div[data-baseweb="select"] button, div[data-testid="stSelectbox"] button').forEach(function(btn) {
                    btn.style.setProperty('background', 'transparent', 'important');
                    btn.style.setProperty('background-color', 'transparent', 'important');
                    btn.style.setProperty('border', 'none', 'important');
                    btn.style.setProperty('box-shadow', 'none', 'important');
                    btn.style.setProperty('padding', '0', 'important');
                    btn.style.setProperty('min-height', 'unset', 'important');
                    btn.style.setProperty('height', 'auto', 'important');
                });

                // 同步设置面板中的深浅色模式按钮激活态
                pDoc.querySelectorAll('.settings-theme-btn').forEach(function(btn) {
                    if (btn.classList.contains('theme-btn-dark')) {
                        btn.classList.toggle('is-active', !isLight);
                    } else if (btn.classList.contains('theme-btn-light')) {
                        btn.classList.toggle('is-active', isLight);
                    }
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

                // 7. 下拉选择菜单选项（来源/分类等筛选项）→ 为每个选项补充完整文本悬停提示，
                //    使被截断（text-overflow: ellipsis）的选项在鼠标悬停时展示全部内容。
                pDoc.querySelectorAll(
                    'ul[data-baseweb="menu"] li[role="option"], [data-baseweb="popover"] li[role="option"], li[role="option"]'
                ).forEach(function(opt) {
                    if (!opt.getAttribute('title')) {
                        const fullText = (opt.innerText || '').replace(/\s+/g, ' ').trim();
                        if (fullText) {
                            opt.setAttribute('title', fullText);
                        }
                    }
                });
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
            const observeTarget = pDoc.body || pDoc.documentElement;
            if (observeTarget) {
                widgetFixObserver = new MutationObserver(function(mutations) {
                    let hasNewNodes = false;
                    for (let i = 0; i < mutations.length; i++) {
                        const m = mutations[i];
                        if (m.type === 'childList' && m.addedNodes && m.addedNodes.length) {
                            hasNewNodes = true;
                            break;
                        }
                    }
                    if (hasNewNodes) {
                        try { applyWidgetFix(); } catch(err) {}
                    }
                });
                widgetFixObserver.observe(observeTarget, { childList: true, subtree: true });
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
