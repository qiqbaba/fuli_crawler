import os
import time
import urllib.parse

from config import PDF_BASE_DIR
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 通用广告过滤规则：在保存 PDF 前注入执行，所有站点共享同一份
# ---------------------------------------------------------------------------

# 传统广告联盟 / 统计 / 弹窗脚本：在 goto 之前注册路由，从网络层阻止下载
COMMON_AD_URL_PATTERNS = [
    r'(?:doubleclick|googleads|googlesyndication|google-analytics)\.com',
    r'(?:adservice|pagead2|partnerads)\.googlesyndication',
    r'(?:cas\.pm|syndication|adsystem)\.com',
    r'(?:googleadservices|googletagmanager)\.com',
    r'\.css\?ver=.*&(?:ad|ads|banner)',
    r'(?:popup|pop-under|popunder)',
    r'(?:layer|float)_?(?:ad|adv|ads)',
    r'/ad(?:s|sense|unit|server|frame|script)\.',
    r'(?:s\d+\.cnzz|cnzz\.com|h5\.cnzz)',
    r'(?:hm\.baidu|posbaidu|cpro\.baidu)',
    r'(?:tanx|alimama|mmstat)\.com',
    r'(?:qzs\.qq|qq\.com)/ad',
]

# 新型博彩广告：页面顶部的全宽 <a><img> 横幅，旧的固定高度选择器无法匹配。
# 同一份规则同时用于网络层 URL 拦截和 DOM 层链接特征识别，避免两套规则不同步。
GAMBLING_AD_URL_PATTERNS = [
    r'\bok\.\d+\.cc\b',
    r'\b\d+\.cc:\d+/88\.html',
    r'\b(?:607|5538|3641|40|20|079|2080489|4080387|6077394|553880604)\.cc\b',
    r'\b(?:5538|3641)\.com\b',
    r'\b9929\.tv\b',
    r'hot\d+\.shop',
    r'rydsbj\.com/ry\d+',
    r'n8p9zk\.com',
    r'uidrvyc\.cn',
    r'ebjg6ty6\.cyou',
    r'gtjinshangshoppingcenter',
    r'(?:\?|&)channelCode=(?:med|mfd)',
]

AD_URL_PATTERNS = COMMON_AD_URL_PATTERNS + GAMBLING_AD_URL_PATTERNS


def _js_regex_array(patterns):
    """把 Python 正则列表转换成 JS 正则字面量数组源码（供注入脚本使用）"""
    return "[" + ", ".join("/" + p.replace("/", r"\/") + "/i" for p in patterns) + "]"


_GAMBLING_AD_BLOCK_JS_BODY = """
    // 新型博彩广告：<a><img> 全宽横幅，按链接 / 图片 URL 特征识别并移除
    const adHostPatterns = __AD_PATTERNS__;
    const isAdUrl = (url) => !!url && adHostPatterns.some(p => p.test(url));

    // 1. 指向博彩站点的链接；含图片横幅时连带移除只包了它的容器
    document.querySelectorAll('a[href]').forEach(a => {
        if (!isAdUrl(a.href)) return;
        const parent = a.parentElement;
        if (a.querySelector('img') && parent && parent.children.length === 1) {
            parent.remove();
        } else {
            a.remove();
        }
    });

    // 2. 广告图片本身（含懒加载占位属性）
    document.querySelectorAll('img').forEach(img => {
        const src = img.getAttribute('src') || '';
        const lazy = img.getAttribute('data-src') || img.getAttribute('data-original') || '';
        if (!isAdUrl(src) && !isAdUrl(lazy)) return;
        const link = img.closest('a');
        (link || img).remove();
    });

    // 3. 解除浮层广告常用的滚动锁定
    if (document.body) document.body.style.overflow = 'auto';
    if (document.documentElement) document.documentElement.style.overflow = 'auto';
""".replace("__AD_PATTERNS__", _js_regex_array(GAMBLING_AD_URL_PATTERNS))


def _merge_ad_block_js(site_js=None):
    """把通用广告过滤 JS 与站点自定义 JS 合并成一个箭头函数"""
    body = _GAMBLING_AD_BLOCK_JS_BODY
    if site_js:
        s = site_js.strip()
        if "=>" in s and "{" in s and s.rstrip().endswith("}"):
            s = s[s.index("{") + 1:s.rindex("}")]
        body += "\n" + s
    return "() => {\n" + body + "\n}"


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
                 pre_access_url=None,
                 wait_until="load",
                 use_common_ad_block=True):
        self.ad_selectors = ad_selectors or []
        # 广告 JS：通用新型博彩广告规则自动合并站点自定义规则，在保存 PDF 前执行
        self.ad_block_js = _merge_ad_block_js(ad_block_js) if use_common_ad_block else ad_block_js
        # 广告 URL 正则模式列表——在 goto 前注册路由拦截，从网络层阻止广告资源下载
        if use_common_ad_block:
            self.ad_url_patterns = list(dict.fromkeys(
                list(ad_url_patterns or []) + COMMON_AD_URL_PATTERNS + GAMBLING_AD_URL_PATTERNS
            ))
        else:
            self.ad_url_patterns = ad_url_patterns or []
        self.emulate_media = emulate_media
        self.scale = scale
        # 默认使用 15mm 边距
        self.margin = margin or {"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"}
        self.need_lazy_scroll = need_lazy_scroll
        self.need_img_proxy = need_img_proxy
        self.referer = referer
        self.pre_access_url = pre_access_url
        self.wait_until = wait_until

class PDFGenerator:
    """通用的 PDF 生成与存储归档服务
    
    在网络拦截器中按需、局部地创建 aiohttp.ClientSession，其生命周期与 Page 实例绑定，
    从而在支持高并发图片代理的同时，彻底避免多线程环境下事件循环冲突及析构泄漏的问题。
    """
    def __init__(self, r2_uploader=None):
        self.r2_uploader = r2_uploader
    
    def close(self):
        """空方法，用于向后兼容"""
        pass

    def _normalize_date_and_year(self, publish_date):
        """标准化发布日期与年份，防止出现 Unknown_Year 或不规范路径"""
        import re
        from datetime import datetime
        date_str = str(publish_date or "").strip()
        m = re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', date_str)
        if m:
            clean_date = m.group(1)
            return clean_date, clean_date.split('-')[0]
        m_year = re.search(r'\b(20\d{2})\b', date_str)
        if m_year:
            year = m_year.group(1)
            today = datetime.now().strftime("%m-%d")
            return f"{year}-{today}", year
        now = datetime.now().strftime("%Y-%m-%d")
        return now, now.split('-')[0]

    def _get_pdf_local_tmp_path(self, publish_date, title, source_name):
        """获取 PDF 本地临时/持久化保存路径"""
        publish_date, year = self._normalize_date_and_year(publish_date)
        if self.r2_uploader:
            import tempfile
            base = os.path.join(tempfile.gettempdir(), f"{source_name}_pdf")
        else:
            base = PDF_BASE_DIR

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
        publish_date, year = self._normalize_date_and_year(publish_date)
        if self.r2_uploader:
            remote_key = f"pdf/{year}/{os.path.basename(local_path)}"
            result = self.r2_uploader.upload_pdf(local_path, remote_key)
            if result:
                if os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                    except Exception:
                        pass
                return result
            # 上传失败时保留本地文件，返回相对路径避免数据丢失
            return f"pdf/{year}/{os.path.basename(local_path)}"
        else:
            rel_path = f"pdf/{year}/{os.path.basename(local_path)}"
            return rel_path.replace('\\', '/')

    def _setup_image_proxy(self, page):
        """在网络层添加图片代理请求拦截器，在 Python 后台下载图片喂给浏览器以绕过防盗链和 GFW"""
        try:
            from config import get_effective_proxy
            from curl_cffi import requests as cffi_requests

            def img_router(route):
                try:
                    req_url = route.request.url
                    if "plugin/img_layer/data/" in req_url and "?src=" in req_url:
                        try:
                            real_url = urllib.parse.unquote(req_url.split("?src=")[1])
                            p_dict = get_effective_proxy(exclusive=True)
                            proxy_url = p_dict.get("http") if p_dict else None
                            headers = {
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                            }
                            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
                            resp = cffi_requests.get(real_url, headers=headers, proxies=proxies, timeout=15, verify=False)
                            if resp.status_code == 200:
                                content_type = resp.headers.get("Content-Type", "image/jpeg")
                                route.fulfill(
                                    status=200,
                                    content_type=content_type,
                                    body=resp.content
                                )
                                return
                        except Exception as route_err:
                            logger.warning("路由代理图片下载失败: %s", route_err)
                    route.continue_()
                except Exception as ex:
                    logger.warning("路由处理异常: %s", ex)
                    try:
                        route.continue_()
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

                    // 4. 等待所有图片完全加载（带 2.5s 单图超时兜底，防止挂起）
                    const images = Array.from(document.querySelectorAll('img'));
                    const imagePromises = images.map(img => {
                        if (img.complete) return Promise.resolve();
                        return new Promise(resolve => {
                            const timer = setTimeout(resolve, 2500);
                            img.addEventListener('load', () => { clearTimeout(timer); resolve(); });
                            img.addEventListener('error', () => { clearTimeout(timer); resolve(); });
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
                # 先清空可能存在的旧路由，防止路由链式累加和泄漏
                try:
                    page.unroute("**/*")
                except Exception:
                    pass

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
                        # 对于前置访问，使用 domcontentloaded 或 load 即可，没必要用 networkidle 导致超时
                        pre_wait = getattr(config, "wait_until", "load")
                        if pre_wait == "networkidle":
                            pre_wait = "load"
                        page.goto(config.pre_access_url, timeout=20000, wait_until=pre_wait)
                        time.sleep(1.5)
                    except Exception as e_home:
                        logger.warning("前置访问异常: %s", e_home)

                # 加载真正的详情页
                wait_until_val = getattr(config, "wait_until", "load")
                goto_args = {"timeout": 30000, "wait_until": wait_until_val}
                if config.referer:
                    goto_args["referer"] = config.referer
                try:
                    page.goto(target_url, **goto_args)
                except Exception as goto_err:
                    if ("timeout" in str(goto_err).lower() or "Timeout" in type(goto_err).__name__) and goto_args.get("wait_until") == "load":
                        logger.warning("页面 goto 超时，降级使用 domcontentloaded 重试: %s", target_url)
                        goto_args["wait_until"] = "domcontentloaded"
                        goto_args["timeout"] = 25000
                        page.goto(target_url, **goto_args)
                    else:
                        raise goto_err
                
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


# ---------------------------------------------------------------------------
# 各站点 PDF 渲染配置
# 爬虫、fixes/backfill_missing_pdfs.py、fixes/pdf_maintenance.py 共用这一份，
# 避免同一站点的广告过滤规则在多处重复维护导致不一致。
# ---------------------------------------------------------------------------

# 共用同一套站点模板：广告容器、弹层与底飘结构一致
TEMPLATE_SITE_SOURCES = (
    "taose", "tanhua", "jingpin", "jingpin_toupai", "mianfei_guochan", "madou",
)

TEMPLATE_SITE_AD_SELECTORS = [
    'div[style*="height:60px"]',
    'div[style*="height:55px"]',
    'div[style*="height:70px"]',
    '#bottom_float',
    '.bottom_float',
    '.layui-layer',
    '.layui-layer-shade',
    '[id*="layui-layer"]',
    '.modal',
    '.modal-backdrop',
    '.swal-overlay',
    '.swal-modal',
    '.swal2-container',
]

_TEMPLATE_SITE_AD_JS = r"""() => {
    // 旧版 iframe / 固定高度广告 / 底飘
    document.querySelectorAll('iframe').forEach(el => el.remove());
    const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
    adDivs.forEach(div => div.remove());
    const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float');
    if (bottomFloat) bottomFloat.remove();
}"""

_DATANG_AD_JS = r"""() => {
    // 旧逻辑只清理 .breadcrumbs 之前的 .gs-isgood，新版布局把广告移到了下载区/页脚
    document.querySelectorAll('.gs-isgood').forEach(el => {
        if (!el.textContent.includes('永久')) el.remove();
    });
    const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"]');
    adDivs.forEach(div => div.remove());
    const bottomFloat = document.getElementById('bottom_float');
    if (bottomFloat) bottomFloat.remove();
}"""

_DASHEN_AD_SELECTORS = [
    '.hf-container', '#hf-container', '.hf-link', '.hf-img',
    '.dp-container', '#dp-container', '.dp-link', '.dp-img',
    '.partner-grid', '.partner-links', '.site-footer', 'footer.site-footer',
    'div[style*="height:60px"]', 'div[style*="height:140px"]', 'div[style*="height:150px"]',
    'div[style*="height:55px"]', 'div[style*="height:70px"]', 'div[style*="height:80px"]',
    'div[style*="height:95px"]', '#bottom_float', '.bottom_float',
    '.layui-layer', '.layui-layer-shade', '[id*="layui-layer"]',
    '.modal', '.modal-backdrop', '.swal-overlay', '.swal-modal', '.swal2-container',
]

_DASHEN_AD_JS = r"""() => {
    document.querySelectorAll('iframe').forEach(el => el.remove());
    document.querySelectorAll('.hf-container, #hf-container, .hf-link, .hf-img, .dp-container, #dp-container, .dp-link, .dp-img').forEach(el => el.remove());
    document.querySelectorAll('.partner-grid, .partner-links').forEach(el => {
        const card = el.closest('.info-card') || el;
        card.remove();
    });
    document.querySelectorAll('.site-footer, footer.site-footer').forEach(el => el.remove());
    const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:140px"], div[style*="height:150px"], div[style*="height:55px"], div[style*="height:70px"], div[style*="height:80px"], div[style*="height:95px"]');
    adDivs.forEach(div => div.remove());
    const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float');
    if (bottomFloat) bottomFloat.remove();
    document.querySelectorAll('.layui-layer, .layui-layer-shade, [id*="layui-layer"], .modal, .modal-backdrop, .swal-overlay, .swal-modal, .swal2-container').forEach(el => el.remove());
}"""

_GCBT_AD_SELECTORS = [
    '.layui-layer', '.layui-layer-shade',
    '.modal', '.modal-backdrop',
    '.swal-overlay', '.swal-modal', '.swal2-container',
    '[id*="layui-layer"]',
]

_SEJU_AD_SELECTORS = [
    'aside.sidebar',
    'div.d_textbanner',
    'form#commentform',
    '.comt-author',
    '.article-footer',
    'footer.footer',
    '.wp-block-search',
]

_SEJU_AD_JS = r"""() => {
    // 侧边栏推广、评论区与页脚
    document.querySelectorAll('aside.sidebar, div.d_textbanner, form#commentform, .comt-author, .article-footer, footer.footer, .wp-block-search').forEach(el => el.remove());
    // 相关推荐区中指向外部广告站点的链接
    const externalAdPatterns = [/ent\.unxty\.shop/i, /afengy\.app/i];
    document.querySelectorAll('a[href]').forEach(a => {
        if (externalAdPatterns.some(p => p.test(a.href))) a.remove();
    });
}"""


def build_pdf_render_config(source):
    """按站点标识构建 PDF 渲染配置，爬虫与维护脚本共用同一份规则"""
    if source == "gcbt":
        return PDFRenderConfig(
            need_img_proxy=False,
            wait_until="domcontentloaded",
            pre_access_url=None,
            referer="https://gcbt.net/",
            need_lazy_scroll=True,
            emulate_media="screen",
            ad_selectors=list(_GCBT_AD_SELECTORS),
        )
    if source == "seju":
        return PDFRenderConfig(
            margin={"top": "20mm", "bottom": "20mm", "left": "20mm", "right": "20mm"},
            need_lazy_scroll=True,
            ad_selectors=list(_SEJU_AD_SELECTORS),
            ad_block_js=_SEJU_AD_JS,
        )
    if source == "datang":
        return PDFRenderConfig(
            ad_selectors=list(TEMPLATE_SITE_AD_SELECTORS),
            ad_block_js=_DATANG_AD_JS,
        )
    if source == "dashen":
        return PDFRenderConfig(
            emulate_media="screen",
            ad_selectors=list(_DASHEN_AD_SELECTORS),
            ad_block_js=_DASHEN_AD_JS,
        )
    if source in TEMPLATE_SITE_SOURCES:
        return PDFRenderConfig(
            emulate_media="screen",
            ad_selectors=list(TEMPLATE_SITE_AD_SELECTORS),
            ad_block_js=_TEMPLATE_SITE_AD_JS,
        )
    return PDFRenderConfig()


def build_pdf_config_map():
    """生成一份独立的站点配置映射，每个调用方持有自己的实例"""
    sources = (
        "seju", "gcbt", "madou", "datang", "dashen",
        "jingpin", "tanhua", "taose", "mianfei_guochan", "jingpin_toupai",
    )
    return {s: build_pdf_render_config(s) for s in sources}
