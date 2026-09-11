import os
import time
import random
import threading
from typing import Optional, Tuple
from playwright.sync_api import Playwright, Browser, BrowserContext

from config import USER_AGENTS, get_effective_proxy_string, is_local_mode, get_browser_engine
from utils.logger import get_logger
from utils.stealth import get_browser_launch_args, apply_stealth

logger = get_logger(__name__)

class BrowserFactory:
    """统一的浏览器工厂类，负责创建和管理浏览器资源"""
    
    def __init__(self):
        self._thread_local = threading.local()
        self._resources_lock = threading.Lock()
        self._active_resources = []
        
    def create_browser_context(
        self, 
        headless: Optional[bool] = None,
        browser_type: str = "chromium",
        enable_stealth: bool = True,
        use_persistent_context: bool = False,
        no_proxy: bool = False,
        user_agent: Optional[str] = None,
        viewport: Optional[dict] = None,
        locale: str = "zh-CN",
        timezone_id: str = "Asia/Shanghai",
        source: Optional[str] = None
    ) -> Tuple[Playwright, Browser, BrowserContext]:
        """创建浏览器上下文，支持线程隔离和持久化模式"""
        
        # 检查线程本地资源是否已存在
        if hasattr(self._thread_local, "context"):
            return self._thread_local.playwright, self._thread_local.browser, self._thread_local.context

        # Camoufox 引擎分支（原有 Chromium 方案保留，见下方分支）
        if get_browser_engine() == "camoufox":
            return self._create_camoufox_context(headless=headless, no_proxy=no_proxy, source=source)

        from playwright.sync_api import sync_playwright
        p = sync_playwright().start()
        
        # 针对 Python 3.11+ 的 Playwright CancelledError 异常静默
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            
            def playwright_exception_handler(loop, context):
                exc = context.get("exception")
                if isinstance(exc, asyncio.CancelledError):
                    # 静默 CancelledError
                    return
                message = context.get("message", "")
                if "Connection.dispatch" in message or "_done_callback" in message:
                    # 静默 Connection 调度回调中的错误
                    return
                # 使用默认异常处理器
                loop.default_exception_handler(context)
                
            loop.set_exception_handler(playwright_exception_handler)
        except Exception as handler_err:
            logger.debug("设置事件循环异常处理器失败: %s", handler_err)
        
        # 确定 headless 模式
        if headless is None:
            local_mode = is_local_mode()
            headless = not local_mode
            
        # 获取启动参数
        launch_args = get_browser_launch_args(browser_type=browser_type, headless=headless)
        
        # 配置代理
        playwright_proxy = None
        if not no_proxy:
            try:
                proxy_url = get_effective_proxy_string(exclusive=True, source=source)
                if proxy_url:
                    playwright_proxy = {"server": proxy_url}
            except Exception as ex:
                logger.warning("获取自动代理失败: %s", ex)
        
        # 配置 User-Agent
        ua = user_agent or random.choice(USER_AGENTS)
        
        # 配置视口
        viewport = viewport or {"width": 1280, "height": 900}
        
        browser: Optional[Browser] = None
        context: Optional[BrowserContext] = None
        profile_dir: Optional[str] = None
        
        try:
            if use_persistent_context:
                # 创建持久化上下文
                profile_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "temp_profiles",
                    f"profile_{threading.get_ident()}_{random.randint(1000, 9999)}_{int(time.time())}"
                )
                os.makedirs(profile_dir, exist_ok=True)
                
                try:
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=headless,
                        channel="chrome",
                        args=launch_args,
                        user_agent=ua,
                        viewport=viewport,
                        bypass_csp=True,
                        proxy=playwright_proxy,
                        locale=locale,
                        timezone_id=timezone_id
                    )
                    browser = context.browser
                    logger.debug("[+] 线程 %s 成功启动真实 Chrome 持久化上下文", threading.get_ident())
                except Exception as e:
                    logger.info("[*] 启动真实 Chrome 失败，回退到内置 Chromium: %s", e)
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=headless,
                        args=launch_args,
                        user_agent=ua,
                        viewport=viewport,
                        bypass_csp=True,
                        proxy=playwright_proxy,
                        locale=locale,
                        timezone_id=timezone_id
                    )
                    browser = context.browser
                    logger.info("[+] 线程 %s 成功启动内置 Chromium 持久化上下文", threading.get_ident())
            else:
                # 创建临时上下文 — 代理仅在 launch() 层设置，new_context() 不再重复传递
                # 避免 Playwright 双层代理可能导致连接冲突或认证失败，优先使用本地已安装的 Chrome
                try:
                    browser = p.chromium.launch(headless=headless, channel="chrome", args=launch_args, proxy=playwright_proxy)
                except Exception:
                    browser = p.chromium.launch(headless=headless, args=launch_args, proxy=playwright_proxy)
                context = browser.new_context(
                    viewport=viewport,
                    locale=locale,
                    timezone_id=timezone_id,
                    user_agent=ua
                )
                logger.debug("[+] 线程 %s 成功启动临时浏览器上下文", threading.get_ident())
                
            # 注入 stealth 伪装
            if enable_stealth:
                apply_stealth(context, browser_type=browser_type)
                logger.debug("[+] 已注入 stealth 伪装脚本")
                
            # 保存线程本地资源
            self._thread_local.playwright = p
            self._thread_local.browser = browser
            self._thread_local.context = context
            self._thread_local.profile_dir = profile_dir
            
            with self._resources_lock:
                self._active_resources.append((p, browser, context, profile_dir, None))
                
            return p, browser, context
            
        except Exception as e:
            logger.error("创建浏览器上下文失败: %s", e)
            # 清理已创建的资源
            if context:
                context.close()
            if browser:
                browser.close()
            if p:
                p.stop()
            raise
            
    def _create_camoufox_context(
        self,
        headless: Optional[bool] = None,
        no_proxy: bool = False,
        source: Optional[str] = None,
    ) -> Tuple[Playwright, Browser, BrowserContext]:
        """使用 Camoufox（反检测定制 Firefox）创建浏览器上下文。

        Camoufox 自带指纹伪装与反检测能力，无需再注入 stealth 脚本，
        也不支持自定义 UA（UA 由 Camoufox 按指纹一致性自动生成）。
        返回 (None, None, context) 以兼容现有调用方签名；
        Camoufox 实例保存在线程本地并在清理时关闭。
        """
        from camoufox.sync_api import Camoufox

        if headless is None:
            headless = not is_local_mode()

        # 配置代理（复用原有代理逻辑）
        camoufox_proxy = None
        if not no_proxy:
            try:
                proxy_url = get_effective_proxy_string(exclusive=True, source=source)
                if proxy_url:
                    camoufox_proxy = {"server": proxy_url}
            except Exception as ex:
                logger.warning("获取自动代理失败: %s", ex)

        fox = Camoufox(
            headless=headless,
            proxy=camoufox_proxy,
            geoip=bool(camoufox_proxy),  # 有代理时按代理 IP 伪造地理位置/时区/语言
            humanize=True,               # 模拟真人鼠标移动
            i_know_what_im_doing=True,   # 允许上述自定义配置
        )
        try:
            context = fox.start()
        except Exception as e:
            logger.error("启动 Camoufox 失败: %s", e)
            raise

        logger.info("[+] 线程 %s 成功启动 Camoufox 浏览器上下文 (headless=%s, proxy=%s)",
                    threading.get_ident(), headless, "是" if camoufox_proxy else "否")

        # 保存线程本地资源：p/browser 为 None，fox 单独保存供清理
        self._thread_local.playwright = None
        self._thread_local.browser = None
        self._thread_local.context = context
        self._thread_local.profile_dir = None
        self._thread_local.camoufox = fox

        with self._resources_lock:
            self._active_resources.append((None, None, context, None, fox))

        return None, None, context

    def _cleanup_resources(self, p, browser, context, profile_dir, camoufox=None):
        """通用资源清理方法"""
        def _safe_close(name, action):
            try:
                action()
            except Exception as e:
                err_msg = str(e)
                if "cannot switch to a different thread" in err_msg:
                    logger.debug("%s静默忽略(跨线程): %s", name, e)
                else:
                    logger.warning("%s失败: %s", name, e)

        if context:
            # 在关闭 context 前，先对所有 page 逐一 unroute + close，
            # 避免 Playwright 内部待处理的 _on_route 协程在事件循环关闭后
            # 尝试 asyncio.create_task() 而触发 RuntimeError: no running event loop
            try:
                for page in list(context.pages):
                    try:
                        page.unroute("**/*")
                    except Exception:
                        pass
                    try:
                        page.close()
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("清理 context pages 失败（已忽略）: %s", e)
            _safe_close("关闭浏览器上下文", context.close)
            
        if browser:
            _safe_close("关闭浏览器实例", browser.close)
            
        if p:
            _safe_close("停止 Playwright 实例", p.stop)
            
        if profile_dir and os.path.exists(profile_dir):
            try:
                import shutil
                shutil.rmtree(profile_dir)
            except Exception as e:
                logger.warning("删除临时用户数据目录失败: %s", e)

        if camoufox:
            def _stop_fox():
                try:
                    camoufox.__exit__(None, None, None)
                except AttributeError:
                    camoufox.stop()
            _safe_close("停止 Camoufox 实例", _stop_fox)

    def destroy_thread_resources(self):
        """清理当前线程的浏览器资源"""
        p = getattr(self._thread_local, "playwright", None)
        browser = getattr(self._thread_local, "browser", None)
        context = getattr(self._thread_local, "context", None)
        profile_dir = getattr(self._thread_local, "profile_dir", None)
        camoufox = getattr(self._thread_local, "camoufox", None)
        
        self._cleanup_resources(p, browser, context, profile_dir, camoufox=camoufox)
        
        # 从活跃资源列表中移除（按 context 精确匹配，Camoufox 模式下 p 为 None）
        with self._resources_lock:
            self._active_resources = [
                item for item in self._active_resources
                if item[2] is not context
            ]
                
        # 清除线程本地属性
        for attr in ["playwright", "browser", "context", "profile_dir", "camoufox"]:
            if hasattr(self._thread_local, attr):
                delattr(self._thread_local, attr)
                
        # 清除代理绑定
        try:
            from utils.proxy_manager import get_proxy_manager
            from config import is_proxy_manager_enabled
            if is_proxy_manager_enabled():
                mgr = get_proxy_manager()
                if mgr:
                    tid = threading.get_ident()
                    with mgr._lock:
                        if tid in mgr._thread_proxy_map:
                            del mgr._thread_proxy_map[tid]
        except Exception as e:
            logger.warning("清除线程代理绑定失败: %s", e)

    def destroy_other_threads_resources(self):
        """清理除当前线程外其他所有线程的浏览器资源"""
        current_p = getattr(self._thread_local, "playwright", None)
        
        with self._resources_lock:
            # 找出其他线程的资源
            other_resources = [
                item for item in self._active_resources
                if item[0] != current_p
            ]
            # 保留当前线程的资源
            self._active_resources = [
                item for item in self._active_resources
                if item[0] == current_p
            ]
            
        for item in other_resources:
            p, browser, context, profile_dir = item[0], item[1], item[2], item[3]
            camoufox = item[4] if len(item) > 4 else None
            self._cleanup_resources(p, browser, context, profile_dir, camoufox=camoufox)

    def destroy_all_resources(self):
        """清理所有线程的浏览器资源"""
        with self._resources_lock:
            resources = list(self._active_resources)
            self._active_resources.clear()
            
        for item in resources:
            p, browser, context, profile_dir = item[0], item[1], item[2], item[3]
            camoufox = item[4] if len(item) > 4 else None
            self._cleanup_resources(p, browser, context, profile_dir, camoufox=camoufox)
            
        # 清除当前线程的线程本地属性
        for attr in ["playwright", "browser", "context", "profile_dir", "camoufox"]:
            if hasattr(self._thread_local, attr):
                delattr(self._thread_local, attr)
                
        # 清除所有代理绑定
        try:
            from utils.proxy_manager import get_proxy_manager
            from config import is_proxy_manager_enabled
            if is_proxy_manager_enabled():
                mgr = get_proxy_manager()
                if mgr:
                    with mgr._lock:
                        mgr._thread_proxy_map.clear()
        except Exception as e:
            logger.warning("清除所有代理绑定失败: %s", e)

# 创建全局实例
browser_factory = BrowserFactory()