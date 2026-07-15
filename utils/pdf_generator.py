import os
import re
import time
import random
import threading
import asyncio
import urllib.parse
import aiohttp

from config import PDF_BASE_DIR
from utils.logger import get_logger

logger = get_logger(__name__)

class PDFRenderConfig:
    """PDF 渲染配置类，控制每个站点在生成 PDF 时的特异化行为"""
    def __init__(self, 
                 ad_selectors=None, 
                 ad_block_js=None, 
                 ad_url_patterns=None,
                 emulate_media=None, 
                 scale=0.75, 
                 margin=None, 
                 need_lazy_scroll=False, 
                 need_img_proxy=False, 
                 referer=None, 
                 pre_access_url=None):
        self.ad_selectors = ad_selectors or []
        self.ad_block_js = ad_block_js
        # 广告 URL 正则模式列表——在 goto 前注册路由拦截，从网络层阻止广告资源下载
        self.ad_url_patterns = ad_url_patterns or []
        self.emulate_media = emulate_media
        self.scale = scale
        # 默认使用 15mm 边距
        self.margin = margin or {"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"}
        self.need_lazy_scroll = need_lazy_scroll
        self.need_img_proxy = need_img_proxy
        self.referer = referer
        self.pre_access_url = pre_access_url

class PDFGenerator:
    """通用的 PDF 生成与存储归档服务
    
    采用按需创建 aiohttp.ClientSession 的模式，避免在 __init__ 中长期持有 session，
    以及复杂的异步关闭逻辑。每次需要 HTTP 请求时，通过 _get_http_session() 获取
    或创建 session，并配合上下文管理器确保关闭。
    """
    def __init__(self, r2_uploader=None):
        self.r2_uploader = r2_uploader
        self._http_session = None
        self._http_session_lock = threading.Lock()
    
    def _get_http_session(self):
        """获取或延迟创建 aiohttp.ClientSession（线程安全）"""
        if self._http_session is None or self._http_session.closed:
            with self._http_session_lock:
                if self._http_session is None or self._http_session.closed:
                    self._http_session = aiohttp.ClientSession()
        return self._http_session
    
    def close(self):
        """关闭HTTP会话，释放资源"""
        if self._http_session and not self._http_session.closed:
            try:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    if loop.is_running():
                        loop.create_task(self._http_session.close())
                        return
                except RuntimeError:
                    pass
                try:
                    loop = asyncio.get_event_loop()
                    if not loop.is_running():
                        loop.run_until_complete(self._http_session.close())
                except RuntimeError:
                    pass
            except Exception as e:
                logger.warning("关闭HTTP会话失败: %s", e)
    
    def __del__(self):
        """析构函数，确保HTTP会话被关闭"""
        self.close()

    def _get_pdf_local_tmp_path(self, publish_date, title, source_name):
        """获取 PDF 本地临时/持久化保存路径"""
        if self.r2_uploader:
            base = f"/tmp/{source_name}_pdfs"
        else:
            base = PDF_BASE_DIR

        year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
        save_dir = os.path.join(base, year)
        os.makedirs(save_dir, exist_ok=True)

        from utils.metadata_parser import sanitize_filename
        safe_title = sanitize_filename(title)
        base_filename = f"{publish_date}_{safe_title}_{source_name}"
        pdf_path = os.path.join(save_dir, f"{base_filename}.pdf")

        counter = 1
        while os.path.exists(pdf_path):
            pdf_path = os.path.join(save_dir, f"{base_filename}_{counter}.pdf")
            counter += 1

        return pdf_path

    def _upload_or_return_pdf_path(self, local_path, publish_date, source_name):
        """统一的 PDF R2 上传与相对路径返回逻辑"""
        if not local_path or not os.path.exists(local_path):
            return None
        if self.r2_uploader:
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            remote_key = f"pdfs/{year}/{os.path.basename(local_path)}"
            result = self.r2_uploader.upload_pdf(local_path, remote_key)
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            return result
        else:
            year = publish_date.split('-')[0] if '-' in publish_date else "Unknown_Year"
            rel_path = f"pdf/{year}/{os.path.basename(local_path)}"
            return rel_path.replace('\\', '/')

    def _setup_image_proxy(self, page):
        """在网络层添加图片代理请求拦截器，在 Python 后台下载图片喂给浏览器以绕过防盗链和 GFW"""
        try:
            async def img_router(route):
                try:
                    req_url = route.request.url
                    if "plugin/img_layer/data/" in req_url and "?src=" in req_url:
                        try:
                            real_url = urllib.parse.unquote(req_url.split("?src=")[1])
                            
                            from config import get_effective_proxy
                            p_dict = get_effective_proxy(exclusive=True)
                            
                            headers = {
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                            }
                            
                            async with self._get_http_session().get(real_url, headers=headers, proxy=p_dict.get("http") if p_dict else None, timeout=aiohttp.ClientTimeout(total=15)) as r:
                                    if r.status == 200:
                                        content = await r.read()
                                        content_type = r.headers.get("Content-Type", "image/jpeg")
                                        await route.fulfill(
                                            status=200,
                                            content_type=content_type,
                                            body=content
                                        )
                                        return
                        except asyncio.CancelledError:
                            # 页面关闭时正在处理的路由任务会被取消，优雅放行
                            try:
                                await route.continue_()
                            except Exception:
                                pass
                            return
                        except Exception as route_err:
                            logger.warning("路由代理图片下载失败: %s", route_err)
                    await route.continue_()
                except asyncio.CancelledError:
                    # 顶层 CancelledError 保护，防止页面关闭时未捕获的异常
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            page.route("**/*", img_router)
        except Exception as route_setup_err:
            logger.warning("配置网络拦截路由异常: %s", route_setup_err)

    def _setup_ad_blocking_route(self, page, config: PDFRenderConfig):
        """
        在网络层拦截广告 URL，在 goto 之前注册路由，阻止广告资源下载。
        这比事后移除 DOM 更高效——广告资源根本不会下载到浏览器。
        """
        if not config.ad_url_patterns:
            return
        import re
        patterns = [re.compile(p, re.I) for p in config.ad_url_patterns]
        try:
            def ad_blocker(route):
                url = route.request.url
                for pat in patterns:
                    if pat.search(url):
                        route.abort()
                        return
                route.continue_()

            # 注册在已有路由之后——非广告请求会 continue_ 到后面的处理器
            page.route("**/*", ad_blocker)
        except Exception as e:
            logger.warning("注册广告拦截路由异常: %s", e)

    def _trigger_lazy_load(self, page):
        """滚动触发页面图片懒加载"""
        try:
            page.evaluate("""
                async () => {
                    const replaceLazyAttrs = () => {
                        const images = document.querySelectorAll('img');
                        const lazyAttrs = ['data-src', 'data-original', 'data-lazy-src', 'data-src-webp', 'data-cfsrc', 'lazy-src'];
                        images.forEach(img => {
                            for (const attr of lazyAttrs) {
                                const val = img.getAttribute(attr);
                                if (val) {
                                    img.src = val;
                                    break;
                                }
                            }
                        });
                    };
                    replaceLazyAttrs();

                    await new Promise((resolve) => {
                        let totalHeight = 0;
                        const distance = 600;
                        const timer = setInterval(() => {
                            const scrollHeight = document.body.scrollHeight;
                            window.scrollBy(0, distance);
                            totalHeight += distance;
                            replaceLazyAttrs();

                            if (totalHeight >= scrollHeight || window.scrollY + window.innerHeight >= scrollHeight) {
                                clearInterval(timer);
                                resolve();
                            }
                        }, 150);
                    });

                    window.scrollTo(0, 0);
                    await new Promise(r => setTimeout(r, 500));

                    // 4. 等待所有图片完全加载
                    const images = Array.from(document.querySelectorAll('img'));
                    const imagePromises = images.map(img => {
                        if (img.complete) return Promise.resolve();
                        return new Promise(resolve => {
                            img.addEventListener('load', resolve);
                            img.addEventListener('error', resolve); // 即使加载失败也 resolve，避免卡死
                        });
                    });
                    await Promise.all(imagePromises);
                }
            """)
        except Exception as e:
            logger.warning("执行懒加载滚动 JS 异常: %s", e)

    def _apply_ad_blocking(self, page, config: PDFRenderConfig):
        """屏蔽页面广告"""
        if config.ad_selectors or config.ad_block_js:
            try:
                # 拼接成一个统一的 JS 函数在 Page 中执行
                js_code = "() => {\n"
                if config.ad_selectors:
                    js_code += f"    const selectors = {config.ad_selectors};\n"
                    js_code += "    selectors.forEach(sel => {\n"
                    js_code += "        document.querySelectorAll(sel).forEach(el => el.remove());\n"
                    js_code += "    });\n"
                if config.ad_block_js:
                    js_code += f"    try {{ ({config.ad_block_js})(); }} catch(e) {{ console.error(e); }}\n"
                js_code += "}"
                page.evaluate(js_code)
            except Exception as ad_err:
                logger.warning("屏蔽广告脚本执行失败: %s", ad_err)

    def generate_pdf(self, page_or_context, target_url_or_page, publish_date, title, source_name, config: PDFRenderConfig):
        """
        核心生成 PDF 接口
        :param page_or_context: 可以是 sync_playwright 的 context（新建 Page），也可以是已有的 page（就地渲染）
        :param target_url_or_page: 可以是 URL 字符串，也可以是已有的 page（就地渲染）
        :param publish_date: 发布日期
        :param title: 标题
        :param source_name: 爬虫标识（如 gcbt）
        :param config: PDFRenderConfig 渲染配置
        """
        is_reuse_page = False
        page = None
        
        if hasattr(page_or_context, "pdf") and not isinstance(target_url_or_page, str):
            is_reuse_page = True
            page = page_or_context
        elif hasattr(target_url_or_page, "pdf"):
            is_reuse_page = True
            page = target_url_or_page

        if not publish_date or publish_date == "Unknown_Date":
            from datetime import datetime
            publish_date = datetime.now().strftime("%Y-%m-%d")

        local_path = self._get_pdf_local_tmp_path(publish_date, title, source_name)

        try:
            if is_reuse_page:
                # 挂载图片代理（复用页面也需启用）
                if config.need_img_proxy:
                    self._setup_image_proxy(page)
                # 注册广告网络层拦截
                self._setup_ad_blocking_route(page, config)
                # 1. 屏蔽广告
                self._apply_ad_blocking(page, config)
                # 2. 模拟排版
                if config.emulate_media:
                    try:
                        page.emulate_media(media=config.emulate_media)
                    except Exception:
                        pass
                # 3. 打印 PDF
                page.pdf(
                    path=local_path,
                    format="A4",
                    scale=config.scale,
                    print_background=True,
                    margin=config.margin
                )
            else:
                # 必须从 context 新建 page 并加载 URL
                context = page_or_context
                target_url = target_url_or_page
                page = context.new_page()

                # 挂载代理拦截
                if config.need_img_proxy:
                    self._setup_image_proxy(page)

                # 在 goto 前注册广告网络层拦截，阻止广告资源下载到浏览器
                self._setup_ad_blocking_route(page, config)

                # 前置访问
                if config.pre_access_url:
                    try:
                        page.goto(config.pre_access_url, timeout=20000, wait_until="networkidle")
                        time.sleep(1.5)
                    except Exception as e_home:
                        logger.warning("前置访问异常: %s", e_home)

                # 加载真正的详情页
                goto_args = {"timeout": 30000, "wait_until": "networkidle"}
                if config.referer:
                    goto_args["referer"] = config.referer
                page.goto(target_url, **goto_args)
                
                # 延迟或滚动
                if config.need_lazy_scroll:
                    self._trigger_lazy_load(page)
                else:
                    time.sleep(3.0)

                # 屏蔽广告
                self._apply_ad_blocking(page, config)

                # 模拟排版
                if config.emulate_media:
                    try:
                        page.emulate_media(media=config.emulate_media)
                    except Exception:
                        pass

                # 打印 PDF
                page.pdf(
                    path=local_path,
                    format="A4",
                    scale=config.scale,
                    print_background=True,
                    margin=config.margin
                )
                # 关闭页面前移除路由，避免异步路由任务被取消时抛出 CancelledError
                try:
                    page.unroute("**/*")
                except Exception:
                    pass
                page.close()
                page = None
        except Exception as e:
            logger.error("PDF 生成失败: %s", e)
            if page and not is_reuse_page:
                try:
                    page.unroute("**/*")
                except Exception:
                    pass
                try:
                    page.close()
                except Exception:
                    pass
            return ""

        return self._upload_or_return_pdf_path(local_path, publish_date, source_name)