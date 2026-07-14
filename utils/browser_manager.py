import random
import warnings
from config import USER_AGENTS

def create_browser_context(playwright, user_agent=None, viewport=None):
    """
    已废弃：请使用 crawlers/base_crawler.py 中的 PlaywrightBaseCrawler._get_thread_resources() 替代。

    启动 Chromium 浏览器并创建配置好的 context。
    自动检测和配置代理、设置 user_agent 以及注入 webdriver 屏蔽脚本。
    返回: (browser, context)
    """
    warnings.warn(
        "utils.browser_manager.create_browser_context 已废弃，"
        "请使用 PlaywrightBaseCrawler._get_thread_resources() 替代",
        DeprecationWarning,
        stacklevel=2
    )
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-features=UserAgentClientHint",
    ]
    
    playwright_proxy = None
    try:
        from config import get_effective_proxy_string
        proxy_url = get_effective_proxy_string(exclusive=True)
        if proxy_url:
            playwright_proxy = {"server": proxy_url}
    except Exception as ex:
        print(f"[!] 获取自动代理失败: {ex}")
            
    if playwright_proxy:
        print(f"[*] Playwright 启动代理: {playwright_proxy['server']}")
    else:
        print("[*] Playwright 未启用代理")
        
    browser = playwright.chromium.launch(headless=True, args=launch_args, proxy=playwright_proxy)
    
    ctx_args = {
        "locale": "zh-CN",
        "user_agent": user_agent or random.choice(USER_AGENTS)
    }
    if viewport:
        ctx_args["viewport"] = viewport
    else:
        ctx_args["viewport"] = {'width': 1920, 'height': 1080}
        
    context = browser.new_context(**ctx_args)
    
    # 使用统一 stealth 模块注入伪装脚本
    try:
        from utils.stealth import apply_stealth
        apply_stealth(context)
    except Exception as stealth_err:
        print(f"[-] 应用 stealth 失败: {stealth_err}")
    
    return browser, context
