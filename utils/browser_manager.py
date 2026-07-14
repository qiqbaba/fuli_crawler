from utils.browser_factory import browser_factory


def create_browser_context(playwright, user_agent=None, viewport=None):
    """
    启动 Chromium 浏览器并创建配置好的上下文（兼容旧接口的包装函数）。
    自动检测和配置代理、设置 user_agent 以及注入 stealth 伪装脚本。
    返回: (browser, context)
    """
    _, browser, context = browser_factory.create_browser_context(
        headless=True,
        browser_type='chromium',
        enable_stealth=True,
        use_persistent_context=False,
        user_agent=user_agent,
        viewport=viewport or {"width": 1920, "height": 1080}
    )
    return browser, context