import random
from config import USER_AGENTS, get_crawler_proxy, is_proxy_manager_enabled

def create_browser_context(playwright, user_agent=None, viewport=None):
    """
    启动 Chromium 浏览器并创建配置好的 context。
    自动检测和配置代理、设置 user_agent 以及注入 webdriver 屏蔽脚本。
    返回: (browser, context)
    """
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-web-security",
        "--ignore-certificate-errors",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-features=UserAgentClientHint",
    ]
    
    playwright_proxy = None
    crawler_proxy = get_crawler_proxy()
    if crawler_proxy:
        playwright_proxy = {"server": crawler_proxy}
    elif is_proxy_manager_enabled():
        try:
            from utils.proxy_manager import get_proxy_string
            proxy_url = get_proxy_string()
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
    
    _STEALTH_JS = """
    () => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
    }
    """
    context.add_init_script(_STEALTH_JS)
    
    return browser, context
