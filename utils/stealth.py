"""
统一的 Playwright 反检测 stealth 模块。

替代原来分散在各文件中的内联 JS 注入，提供更难以检测的伪装手段。
stealth JS 脚本从独立的 utils/stealth.js 文件加载，便于单独编辑和语法高亮。

改进点:
1. navigator.webdriver — 在原型链上操作而非自身属性
2. navigator.plugins — 使用真实的 PluginArray 子类，而非普通 Array
3. navigator.mimeTypes — 补全 MimeTypeArray
4. cdc_ 变量 — 动态检测运行时实际存在的变量名，不硬编码
5. chrome.runtime — 补全缺失的 chrome 对象属性
6. Permissions API — 返回 granted 避免常见检测
7. WebGL vendor/renderer — 伪装 GPU 指纹
8. 支持多浏览器类型 (chromium/firefox/webkit) 切换
9. headful 模式推荐
"""

import os
from typing import Optional
from utils.logger import get_logger

logger = get_logger(__name__)

# stealth.js 文件路径（与当前模块同目录）
_STEALTH_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stealth.js")

# 缓存已加载的脚本内容，避免重复 IO
_STEALTH_JS_CACHE: Optional[str] = None


def _load_stealth_js() -> str:
    """从外部 stealth.js 文件加载脚本内容"""
    global _STEALTH_JS_CACHE
    if _STEALTH_JS_CACHE is not None:
        return _STEALTH_JS_CACHE
    try:
        with open(_STEALTH_JS_PATH, "r", encoding="utf-8") as f:
            _STEALTH_JS_CACHE = f.read()
        return _STEALTH_JS_CACHE
    except Exception as e:
        logger.error("加载 stealth.js 文件失败 (%s): %s", _STEALTH_JS_PATH, e)
        return ""


def get_stealth_script(browser_type: str = "chromium") -> str:
    """获取适用于指定浏览器的 stealth JS 注入脚本"""
    return _load_stealth_js()


def apply_stealth(context, browser_type: str = "chromium") -> bool:
    """
    向 Playwright browser context 注入 stealth 脚本。
    
    Args:
        context: Playwright BrowserContext 实例
        browser_type: 'chromium' | 'firefox' | 'webkit'
    
    Returns:
        bool: 是否注入成功
    """
    try:
        script = get_stealth_script(browser_type)
        if not script:
            logger.warning("stealth 脚本为空，跳过注入")
            return False
        context.add_init_script(script)
        return True
    except Exception as e:
        logger.error("注入 stealth 脚本失败: %s", e)
        return False


def get_browser_launch_args(
    browser_type: str = "chromium",
    headless: bool = True,
    extra_args: Optional[list] = None
) -> list:
    """
    获取适用于指定浏览器的启动参数，移除可能暴露自动化检测的参数。
    
    Args:
        browser_type: 'chromium' | 'firefox' | 'webkit'
        headless: 是否无头模式
        extra_args: 额外需要添加的启动参数
    
    Returns:
        list: 启动参数列表
    """
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
    ]

    if browser_type == "chromium":
        # 注意: 移除 --disable-blink-features=AutomationControlled
        # 该参数本身就是一个公开的自动化标记
        args.extend([
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-features=UserAgentClientHint",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-breakpad",
            "--disable-component-extensions-with-background-pages",
            "--disable-component-update",
            "--disable-extensions",
            "--disable-features=TranslateUI",
            "--disable-ipc-flooding-protection",
            "--disable-renderer-backgrounding",
            "--enable-features=NetworkService,NetworkServiceInProcess",
            "--force-color-profile=srgb",
            "--hide-scrollbars",
            "--metrics-recording-only",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
        ])
        if headless:
            args.extend([
                "--headless=new",  # 使用新的 headless 模式，更难检测
            ])

    if extra_args:
        args.extend(extra_args)

    return args


# ============================================================
# 爬虫级别的 stealth 配置（每个爬虫可按需 override）
# ============================================================

# 默认配置：启用所有 stealth 项
STEALTH_ENABLE_WEBDRIVER = True
STEALTH_ENABLE_PLUGINS = True
STEALTH_ENABLE_MIMETYPES = True
STEALTH_ENABLE_LANGUAGES = True
STEALTH_ENABLE_CHROME_RUNTIME = True
STEALTH_ENABLE_PERMISSIONS = True
STEALTH_ENABLE_WEBGL = True
STEALTH_ENABLE_SCREEN = True
STEALTH_ENABLE_CDC_CLEANUP = True