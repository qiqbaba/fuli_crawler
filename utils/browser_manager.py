import random
from config import USER_AGENTS


def create_browser_context(playwright, user_agent=None, viewport=None):
    """
    启动 Chromium 浏览器并创建配置好的 context。
    自动检测和配置代理、设置 user_agent 以及注入 stealth 伪装脚本。
    返回: (browser, context)
    """
    from utils.stealth import get_browser_launch_args, apply_stealth
    launch_args = get_browser_launch_args(browser_type='chromium', headless=True)
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
    ctx_args = {"locale": "zh-CN", "user_agent": user_agent or random.choice(USER_AGENTS)}
    ctx_args["viewport"] = viewport or {"width": 1920, "height": 1080}
    context = browser.new_context(**ctx_args)

    # 使用统一 stealth 模块注入伪装脚本
    apply_stealth(context)

    return browser, context
