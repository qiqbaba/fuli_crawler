"""云端与本地缺失 PDF 批量重新生成与补全归档脚本 (fixes/backfill_missing_pdfs.py)

功能说明：
1. 从 Supabase `resources` 表（或本地 SQLite）中检索 `pdf_path` 为空或缺失的历史记录。
2. 使用统一的 Headless Chromium 引擎和各站点定制渲染/去广告规则生成 PDF。
3. 自动将生成的 PDF 上传至 Cloudflare R2（存储键规则为统一的 `pdf/{year}/{filename}.pdf`）。
4. 上传成功后将 R2 相对路径写回 Supabase（或本地 SQLite），实现全自动幂等补全。
5. 支持多线程并发、指定数据源、分批处理与 Dry-Run 预检。

用法示例：
  python fixes/backfill_missing_pdfs.py --batch-size 100 --workers 4
  python fixes/backfill_missing_pdfs.py --source u3c3 --batch-size 50
  python fixes/backfill_missing_pdfs.py --dry-run
"""

import os
import sys
import time
import argparse
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from utils import setup_console_utf8
from utils.logger import get_logger
from utils.browser_factory import browser_factory
from utils.pdf_generator import PDFGenerator, PDFRenderConfig
from utils.r2_uploader import get_r2_uploader
from utils.db_manager import SupabaseDBManager

logger = get_logger("backfill_missing_pdfs")

# 针对各站点的定制化 PDF 渲染配置
CONFIG_MAP = {
    "seju": PDFRenderConfig(
        margin={"top": "20mm", "bottom": "20mm", "left": "20mm", "right": "20mm"}
    ),
    "gcbt": PDFRenderConfig(
        need_img_proxy=False,
        wait_until="domcontentloaded",
        pre_access_url=None,
        referer="https://gcbt.net/",
        need_lazy_scroll=True,
        emulate_media="screen",
        ad_selectors=[
            '.layui-layer', '.layui-layer-shade',
            '.modal', '.modal-backdrop',
            '.swal-overlay', '.swal-modal', '.swal2-container',
            '[id*="layui-layer"]'
        ],
        ad_block_js="""() => {
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
        }"""
    ),
    "madou": PDFRenderConfig(
        ad_selectors=[
            'div[style*="height:60px"]',
            'div[style*="height:55px"]',
            'div[style*="height:70px"]',
            '#bottom_float'
        ]
    ),
    "datang": PDFRenderConfig(
        ad_block_js="""() => {
            const breadcrumbs = document.querySelector('.breadcrumbs');
            if (breadcrumbs) {
                let prev = breadcrumbs.previousElementSibling;
                while (prev) {
                    if (prev.classList.contains('gs-isgood') && 
                        !prev.textContent.includes('永久地址') && 
                        !prev.textContent.includes('永久')) {
                        prev.remove();
                    }
                    prev = prev.previousElementSibling;
                }
            }
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float');
            if (bottomFloat) {
                bottomFloat.remove();
            }
        }"""
    ),
    "dashen": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            '.hf-container', '#hf-container', '.hf-link', '.hf-img',
            '.dp-container', '#dp-container', '.dp-link', '.dp-img',
            '.partner-grid', '.partner-links', '.site-footer', 'footer.site-footer',
            'div[style*="height:60px"]', 'div[style*="height:140px"]', 'div[style*="height:150px"]',
            'div[style*="height:55px"]', 'div[style*="height:70px"]', 'div[style*="height:80px"]',
            'div[style*="height:95px"]', '#bottom_float', '.bottom_float',
            '.layui-layer', '.layui-layer-shade', '[id*="layui-layer"]',
            '.modal', '.modal-backdrop', '.swal-overlay', '.swal-modal', '.swal2-container'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
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
    ),
    "jingpin": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            'div[style*="height:60px"]', 'div[style*="height:55px"]', 'div[style*="height:70px"]',
            '#bottom_float', '.bottom_float',
            '.layui-layer', '.layui-layer-shade', '[id*="layui-layer"]',
            '.modal', '.modal-backdrop'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float');
            if (bottomFloat) bottomFloat.remove();
        }"""
    ),
    "tanhua": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            'div[style*="height:60px"]', 'div[style*="height:55px"]', 'div[style*="height:70px"]',
            '#bottom_float', '.bottom_float',
            '.layui-layer', '.layui-layer-shade', '[id*="layui-layer"]',
            '.modal', '.modal-backdrop'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float');
            if (bottomFloat) bottomFloat.remove();
        }"""
    ),
    "taose": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            'div[style*="height:60px"]', 'div[style*="height:55px"]', 'div[style*="height:70px"]',
            '#bottom_float', '.layui-layer', '.layui-layer-shade', '[id*="layui-layer"]',
            '.modal', '.modal-backdrop'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float');
            if (bottomFloat) bottomFloat.remove();
        }"""
    ),
    "mianfei_guochan": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            'div[style*="height:60px"]', 'div[style*="height:55px"]', 'div[style*="height:70px"]',
            '#bottom_float', '.bottom_float', '.layui-layer', '.layui-layer-shade',
            '[id*="layui-layer"]', '.modal', '.modal-backdrop'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
            const adDivs = document.querySelectorAll('div[style*="height:60px"], div[style*="height:55px"], div[style*="height:70px"]');
            adDivs.forEach(div => div.remove());
            const bottomFloat = document.getElementById('bottom_float') || document.querySelector('.bottom_float');
            if (bottomFloat) bottomFloat.remove();
        }"""
    ),
    "jingpin_toupai": PDFRenderConfig(
        emulate_media="screen",
        ad_selectors=[
            'div[style*="height:60px"]', 'div[style*="height:55px"]', 'div[style*="height:70px"]',
            '#bottom_float', '.layui-layer', '.layui-layer-shade', '[id*="layui-layer"]',
            '.modal', '.modal-backdrop'
        ],
        ad_block_js="""() => {
            document.querySelectorAll('iframe').forEach(iframe => iframe.remove());
            if (document.body) document.body.style.overflow = 'auto';
            if (document.documentElement) document.documentElement.style.overflow = 'auto';
        }"""
    )
}


class PDFBackfiller:
    def __init__(self, db_manager: SupabaseDBManager, r2_uploader, max_workers: int = 4, delay: float = 0.5):
        self.db = db_manager
        self.r2_uploader = r2_uploader
        self.max_workers = max_workers
        self.delay = delay
        self.pdf_generator = PDFGenerator(r2_uploader=self.r2_uploader)

        self.lock = threading.Lock()
        self.total = 0
        self.processed = 0
        self.success = 0
        self.failed = 0
        self.start_time = 0

    def query_missing_records(self, batch_size: int = 1000, source: str = "all", start_date: str = "") -> list:
        """从 Supabase 查询 pdf_path 为空的记录"""
        logger.info("[*] 正在从 Supabase 检索缺失 PDF 的记录 (source=%s, start_date=%s, batch_size=%s)...",
                    source, start_date or "未限制", batch_size if batch_size > 0 else "全部")

        all_records = []
        page_size = 1000
        offset = 0

        while True:
            # 限制单次拉取量不超过 batch_size
            current_limit = page_size
            if batch_size > 0:
                remaining_needed = batch_size - len(all_records)
                if remaining_needed <= 0:
                    break
                current_limit = min(page_size, remaining_needed)

            query = self.db.client.table('resources').select('id,title,url,publish_time,source,created_at')\
                .eq('pdf_path', '')\
                .order('created_at', desc=True)

            if source and source.lower() != "all":
                query = query.eq('source', source.lower())

            if start_date:
                query = query.gte('created_at', f"{start_date}T00:00:00")

            # 带重试的查询
            data = None
            for attempt in range(3):
                try:
                    resp = query.range(offset, offset + current_limit - 1).execute()
                    data = resp.data or []
                    break
                except Exception as query_err:
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
                    else:
                        logger.error("[-] 查询 Supabase 失败: %s", query_err)
                        raise query_err

            all_records.extend(data)

            if len(data) < current_limit:
                break
            offset += current_limit

        return all_records

    def process_record(self, record: dict) -> bool:
        """处理单条记录：生成 PDF，上传 R2，更新 Supabase"""
        rec_id = record.get('id')
        title = record.get('title') or 'untitled'
        url = record.get('url') or ''
        # 提取规范的 YYYY-MM-DD 发布日期
        import re
        raw_time = record.get('publish_time') or ''
        if not raw_time or 'unknown' in str(raw_time).lower():
            raw_time = record.get('created_at') or ''
        
        m_date = re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', str(raw_time))
        if m_date:
            publish_date = m_date.group(1)
        else:
            publish_date = datetime.now().strftime('%Y-%m-%d')

        source = record.get('source') or 'unknown'

        if not url:
            with self.lock:
                self.processed += 1
                self.failed += 1
            return False

        config_item = CONFIG_MAP.get(source, PDFRenderConfig())
        success = False
        r2_path = None

        try:
            _, _, context = browser_factory.create_browser_context(headless=True, source=source)
            r2_path = self.pdf_generator.generate_pdf(
                page_or_context=context,
                target_url_or_page=url,
                publish_date=publish_date,
                title=title,
                source_name=source,
                config=config_item
            )

            if r2_path:
                # 更新 Supabase 记录中的 pdf_path (带重试)
                for attempt in range(3):
                    try:
                        update_resp = self.db.client.table('resources').update({'pdf_path': r2_path}).eq('id', rec_id).execute()
                        if update_resp.data or update_resp.count is None or update_resp.count > 0:
                            success = True
                        break
                    except Exception as upd_err:
                        if attempt < 2:
                            time.sleep(1.5)
                        else:
                            logger.warning("[-] 更新 Supabase 失败 ID=%s: %s", rec_id, upd_err)

        except Exception as e:
            logger.warning("[-] 处理记录异常 ID=%s source=%s url=%s: %s", rec_id, source, url, e)

        with self.lock:
            self.processed += 1
            if success:
                self.success += 1
            else:
                self.failed += 1

            now = time.time()
            elapsed = max(0.1, now - self.start_time)
            speed = self.processed / elapsed
            remaining = self.total - self.processed
            eta = remaining / speed if speed > 0 else 0
            pct = (self.processed / self.total * 100) if self.total > 0 else 0

            status_char = "✓" if success else "✗"
            logger.info(
                "[%s] [%d/%d] (%5.1f%%) 成功:%d 失败:%d | 速度: %.1f/s 剩余: %.0fs | ID=%s %s -> %s",
                status_char, self.processed, self.total, pct, self.success, self.failed,
                speed, eta, rec_id, source, r2_path or "FAILED"
            )

        if self.delay > 0:
            time.sleep(self.delay)

        return success

    def run(self, records: list):
        """多线程批量补全"""
        self.total = len(records)
        self.processed = 0
        self.success = 0
        self.failed = 0
        self.start_time = time.time()

        logger.info("[*] 开始执行多线程 PDF 补全: 总记录数 %d, 并发线程 %d", self.total, self.max_workers)

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(self.process_record, r) for r in records]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error("[-] 线程池任务未捕获异常: %s", e)

        # 清理所有线程的浏览器实例
        try:
            browser_factory.destroy_all_resources()
        except Exception as e:
            logger.warning("[-] 销毁浏览器资源异常: %s", e)

        total_elapsed = time.time() - self.start_time
        logger.info("=" * 60)
        logger.info("[+] PDF 补全任务完成!")
        logger.info("    总记录数:   %d", self.total)
        logger.info("    成功生成:   %d", self.success)
        logger.info("    失败跳过:   %d", self.failed)
        logger.info("    总耗时:     %.1f 秒 (约 %.1f 分钟)", total_elapsed, total_elapsed / 60.0)
        logger.info("=" * 60)


def main():
    setup_console_utf8()
    parser = argparse.ArgumentParser(description="Supabase 缺失 PDF 批量生成与 R2 归档补全工具")
    parser.add_argument("--batch-size", type=int, default=1000, help="单次处理的最大记录数 (默认 1000, 0 表示全部)")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数 (默认 4)")
    parser.add_argument("--source", type=str, default="all", help="指定补全来源，如 u3c3 / dashen / all")
    parser.add_argument("--start-date", type=str, default="2026-09-12", help="检索起始日期 (YYYY-MM-DD)")
    parser.add_argument("--delay", type=float, default=0.2, help="单任务间隔延时 (秒)")
    parser.add_argument("--dry-run", action="store_true", help="仅检索并显示待补全记录，不实际生成与上传")
    parser.add_argument("--proxy-start-threshold", type=int, default=250, help="代理池提前启动阈值 (默认 250)")
    parser.add_argument("--refresh-proxy", action="store_true", help="是否强制重新获取并验证代理")

    args = parser.parse_args()

    # 优先设置为 cloud 模式
    if config.SUPABASE_URL and config.SUPABASE_KEY:
        config.set_run_mode('cloud')

    if config.get_crawler_proxy():
        logger.info("[*] 网络代理: 使用固定代理 %s", config.get_crawler_proxy())
    elif config.is_proxy_manager_enabled():
        logger.info("[*] 网络代理: 已启用自动代理池管理器")
        from utils.proxy_manager import get_proxy_manager
        manager = get_proxy_manager()
        if manager:
            force_refresh = os.environ.get('REFRESH_PROXY', 'false').lower() == 'true' or args.refresh_proxy
            start_threshold = int(os.environ.get('PROXY_START_THRESHOLD', str(args.proxy_start_threshold)))
            target_count = int(os.environ.get('PROXY_TARGET_COUNT', '1000'))

            if force_refresh or len(manager._working_proxies) < start_threshold:
                logger.info(
                    "[*] 正在准备代理池 (强制刷新=%s, 启动阈值=%d, 目标数量=%d)...",
                    force_refresh, start_threshold, target_count
                )
                if force_refresh or len(manager._proxies) < 500:
                    manager.fetch_proxies(force=force_refresh)
                
                manager.verify_proxies(
                    force=force_refresh,
                    target_count=target_count,
                    start_threshold=start_threshold,
                    post_start_workers=config.get_proxy_verify_post_start_workers(),
                    max_workers=config.get_proxy_verify_workers(),
                    source=args.source if args.source != 'all' else None
                )
            stats = manager.get_stats()
            logger.info(
                "[*] 代理池就绪: 可用 %d 个 (总计 %d 个)，爬虫任务开始，后台验证继续运行...",
                stats['working'], stats['total']
            )
    else:
        logger.info("[*] 网络代理: 未启用代理 (直连模式)")

    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        logger.error("[-] 错误: 未配置 SUPABASE_URL 或 SUPABASE_KEY 环境变量")
        sys.exit(1)

    db = SupabaseDBManager(config.SUPABASE_URL, config.SUPABASE_KEY)
    r2_uploader = get_r2_uploader()

    if not r2_uploader and not args.dry_run:
        logger.warning("[!] 警告: 未配置 R2 环境变量，PDF 将生成在本地临时目录，无法归档至 R2")

    backfiller = PDFBackfiller(
        db_manager=db,
        r2_uploader=r2_uploader,
        max_workers=args.workers,
        delay=args.delay
    )

    records = backfiller.query_missing_records(
        batch_size=args.batch_size,
        source=args.source,
        start_date=args.start_date
    )

    logger.info("[*] 找到待补全记录: %d 条", len(records))

    if not records:
        logger.info("[+] 没有需要补全的记录，退出。")
        return

    if args.dry_run:
        logger.info("[*] Dry-Run 预览模式: 显示前 10 条待处理记录:")
        for idx, r in enumerate(records[:10], 1):
            logger.info("  %2d. ID=%s | 来源=%s | 时间=%s | 标题=%s | URL=%s",
                        idx, r.get('id'), r.get('source'), r.get('created_at'),
                        r.get('title')[:30] if r.get('title') else '', r.get('url'))
        logger.info("[*] Dry-run 完成，未产生任何实际修改。")
        return

    backfiller.run(records)

    # 爬虫补全结束后，触发一次代理缓存保存，落盘后台新验证的代理
    if config.is_proxy_manager_enabled():
        try:
            from utils.proxy_manager import get_proxy_manager
            mgr = get_proxy_manager()
            if mgr:
                mgr._save_cache()
        except Exception:
            pass


if __name__ == "__main__":
    main()
