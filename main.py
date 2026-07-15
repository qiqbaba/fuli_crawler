import sys
import argparse
from config import get_db_path, use_supabase, SUPABASE_URL, SUPABASE_KEY, is_local_mode
from utils.db_manager import DBManager, SupabaseDBManager
from utils import setup_console_utf8
from utils.logger import get_logger

logger = get_logger(__name__)
from crawlers.seju_crawler import SejuCrawler
from crawlers.u3c3_crawler import U3c3Crawler
from crawlers.datang_crawler import DatangCrawler
from crawlers.gcbt_crawler import GcbtCrawler
from crawlers.madou_crawler import MadouCrawler
from crawlers.jingpin_toupai_crawler import JingpinToupaiCrawler


# Windows下控制台强制使用utf-8编码输出，防止中文乱码
setup_console_utf8()

# 爬虫注册表：集中管理所有爬虫的类
# 默认结束页码和并发数由爬虫类自身声明（default_end_page / default_workers）
CRAWLER_REGISTRY = {
    "seju":         SejuCrawler,
    "u3c3":         U3c3Crawler,
    "datang":       DatangCrawler,
    "gcbt":         GcbtCrawler,
    "madou":        MadouCrawler,
    "jingpin_toupai": JingpinToupaiCrawler,
}

# 交互式爬虫选择菜单
CRAWLER_CHOICES = [
    ("seju", "seju (默认)"),
    ("u3c3", "u3c3"),
    ("datang", "datang"),
    ("gcbt", "gcbt"),
    ("madou", "madou"),
    ("jingpin_toupai", "jingpin_toupai"),
]


def _prompt_page_number(prompt: str, default: int, non_interactive: bool = False) -> int:
    """交互式询问页码，支持非 TTY 环境和非交互模式自动使用默认值"""
    if non_interactive or not sys.stdin.isatty():
        return default
    try:
        val = input(f"[*] {prompt} (直接回车默认 {default}): ").strip()
        return int(val) if val else default
    except (KeyboardInterrupt, EOFError):
        print("\n[-] 运行已取消")
        sys.exit(0)
    except ValueError:
        print(f"[-] 输入无效，使用默认值: {default}")
        return default


def main():
    parser = argparse.ArgumentParser(description="多网站通用数据爬虫统一入口")
    parser.add_argument(
        "--crawler", "-c",
        required=False,
        choices=["seju", "u3c3", "datang", "gcbt", "madou", "jingpin_toupai"],
        help="指定运行哪一个网站的爬虫 (seju, u3c3, datang, gcbt, madou, jingpin_toupai)"
    )
    
    # 互斥参数：测试模式或正式爬取模式
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--test", "-t",
        action="store_true",
        default=False,
        help="运行测试模式，提取前 5 条测试数据解析并输出，不入库"
    )
    mode_group.add_argument(
        "--crawl",
        action="store_true",
        help="运行正式爬取模式，保存数据至数据库 (默认激活)"
    )
    
    parser.add_argument(
        "--start",
        type=int,
        default=None,
        help="起始页码 (默认: 1)"
    )
    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="结束页码 (seju默认: 4, u3c3默认: 20, datang默认: 60)"
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["auto", "local", "cloud"],
        default="auto",
        help="运行模式: auto (Action环境为cloud, 否则为local), local (强制本地模式), cloud (强制云端模式) (默认: auto)"
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=None,
        help="并发线程数 (默认: Playwright爬虫seju/datang等为8, u3c3为50)"
    )
    parser.add_argument(
        "--no-early-stop",
        action="store_true",
        default=False,
        help="禁用早停机制 (连续已存在/重复页早停)"
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        default=False,
        help="静音模式，减少控制台日志输出 (不打印每个重复跳过的网址)"
    )
    parser.add_argument(
        "--resume", "-r",
        action="store_true",
        default=False,
        help="断点续爬模式，自动跳过已完成的板块/页面，从上一次中断处继续 (默认: 从头开始)"
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        default=False,
        help="禁用 PDF 生成和相关图片下载，仅爬取结构化元数据以实现极致提速"
    )
    
    # 代理相关参数
    parser.add_argument(
        "--proxy",
        type=str,
        default=None,
        help="指定固定代理地址，格式如 http://host:port 或 http://user:pass@host:port (覆盖环境变量 CRAWLER_PROXY)"
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        default=False,
        help="禁用所有代理 (忽略环境变量 CRAWLER_PROXY 和 ENABLE_PROXY_MANAGER)"
    )
    parser.add_argument(
        "--proxy-manager",
        action="store_true",
        default=False,
        help="启用自动代理管理器 (从免费代理源获取并轮换代理，覆盖环境变量 ENABLE_PROXY_MANAGER)"
    )
    
    # 反检测 / Stealth 相关参数
    parser.add_argument(
        "--no-stealth",
        action="store_true",
        default=False,
        help="禁用 stealth 反检测注入 (不推荐)"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="强制使用 headless 无头模式 (默认本地自动 headful，便于调试且降低检测风险)"
    )
    parser.add_argument(
        "--non-interactive", "-n",
        action="store_true",
        default=False,
        help="非交互模式：跳过所有交互式提示，使用默认值 (CI/CD 环境推荐)"
    )
    
    args = parser.parse_args()
    
    # 自动检测非 TTY 环境（管道输入、重定向、CI/CD 等），强制非交互模式
    if not sys.stdin.isatty():
        args.non_interactive = True
        logger.info("[*] 检测到非 TTY 环境，自动启用非交互模式")
    
    # 如果没有指定 --crawler，进行交互式交互询问
    if not args.crawler:
        if args.non_interactive:
            args.crawler = "seju"
            logger.info("[*] 非交互模式，使用默认爬虫: seju")
        else:
            print("[*] 未通过命令行指定爬虫模块，请选择要运行的爬虫：")
            for i, (key, label) in enumerate(CRAWLER_CHOICES, 1):
                print(f"    {i}. {label}")
            try:
                choice = input(f"请输入序号 [1-{len(CRAWLER_CHOICES)}] (直接回车默认 1): ").strip()
                choice_idx = 1  # 默认
                if choice:
                    choice_idx = int(choice)
                if 1 <= choice_idx <= len(CRAWLER_CHOICES):
                    args.crawler = CRAWLER_CHOICES[choice_idx - 1][0]
                else:
                    args.crawler = "seju"
            except (KeyboardInterrupt, EOFError):
                print("\n[-] 运行已取消")
                sys.exit(0)
            except ValueError:
                args.crawler = "seju"
    
    # 设定全局运行模式
    if args.mode != "auto":
        from config import set_run_mode
        set_run_mode(args.mode)
    
    # 设定代理参数
    from config import set_runtime_proxy, get_crawler_proxy, is_proxy_manager_enabled
    set_runtime_proxy(
        proxy_url=args.proxy,
        disable_proxy=args.no_proxy,
        enable_proxy_manager=args.proxy_manager if args.proxy_manager else None
    )
    
    # 设定反检测参数
    from config import set_runtime_stealth
    set_runtime_stealth(
        disable_stealth=args.no_stealth,
        force_headless=args.headless
    )
    
    # 打印代理配置信息
    effective_proxy = get_crawler_proxy()
    proxy_manager_on = is_proxy_manager_enabled()
    if args.no_proxy:
        logger.info("[*] 代理已禁用 (--no-proxy)")
    elif effective_proxy:
        logger.info("[*] 使用固定代理: %s", effective_proxy)
    elif proxy_manager_on:
        logger.info("[*] 代理管理器已启用 (--proxy-manager)")
        logger.info("[*] 代理将在爬虫启动时自动获取和验证")
    else:
        logger.info("[*] 未配置代理，将直接连接目标网站")
    
    # 根据环境变量和模式自动选择数据库后端
    if use_supabase():
        logger.info("[*] 检测到 Supabase 配置，使用 Supabase PostgreSQL 数据库")
        db_manager = SupabaseDBManager(SUPABASE_URL, SUPABASE_KEY)
    else:
        db_path = get_db_path()
        if is_local_mode():
            logger.info("[*] 本地模式已激活，使用本地 SQLite 数据库: %s", db_path)
        else:
            logger.info("[*] 未检测到 Supabase 配置，使用本地 SQLite 数据库: %s", db_path)
        db_manager = DBManager(db_path)
    
    # 动态匹配爬虫
    crawler = None
    default_end = 1
    default_workers = 8

    if args.crawler in CRAWLER_REGISTRY:
        crawler_cls = CRAWLER_REGISTRY[args.crawler]
        try:
            crawler = crawler_cls(db_manager)
        except Exception as e:
            logger.error("[-] 爬虫 %s 实例化失败: %s", args.crawler, e)
            db_manager.close()
            sys.exit(1)
        default_end = getattr(crawler_cls, "default_end_page", 1)
        default_workers = getattr(crawler_cls, "default_workers", 8)
        if args.workers is None:
            args.workers = default_workers

    if crawler is None:
        logger.error("[-] 找不到指定的爬虫: %s", args.crawler)
        db_manager.close()
        sys.exit(1)
        
    start_page = args.start
    if start_page is None:
        start_page = _prompt_page_number("请输入起始页码", 1, args.non_interactive)

    end_page = args.end
    if end_page is None:
        end_page = _prompt_page_number("请输入结束页码", default_end, args.non_interactive)

    # 做基本的验证
    if start_page < 1:
        logger.warning("[!] 起始页码不能小于 1，已自动设为 1")
        start_page = 1
    if end_page < start_page:
        logger.warning("[!] 结束页码不能小于起始页码 (%s)，已自动设为 %s", start_page, start_page)
        end_page = start_page
    
    # 交互式询问是否启用断点续爬（仅当未通过命令行指定时）
    if not args.resume and not args.non_interactive and sys.stdin.isatty():
        try:
            val = input("[*] 是否启用断点续爬模式？(y/N, 直接回车默认 N): ").strip().lower()
            if val in ('y', 'yes'):
                args.resume = True
        except (KeyboardInterrupt, EOFError):
            print("\n[-] 运行已取消")
            sys.exit(0)
    
    is_test = args.test
    if args.crawl:
        is_test = False
        
    logger.info("[*] 模式: %s", '【测试模式 (不入库)】' if is_test else '【正式爬取模式 (入库)】')
    logger.info("[*] 页码范围: %s 到 %s", start_page, end_page)
    if args.resume:
        logger.info("[*] 断点续爬模式已启用，将自动跳过已完成板块/页面")
    
    interrupted = False
    try:
        crawler.run(
            is_test=is_test,
            start_page=start_page,
            end_page=end_page,
            max_workers=args.workers,
            no_early_stop=args.no_early_stop,
            quiet=args.quiet,
            resume=args.resume,
            no_pdf=args.no_pdf
        )
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("\n[-] 用户中断，跳过统计，正在释放资源...")
        raise
    finally:
        if not interrupted:
            # 输出三个平台的数据统计信息
            logger.info("\n" + "=" * 50)
            logger.info("📊 爬虫完成，正在输出平台数据统计...")
            logger.info("=" * 50)
            try:
                try:
                    from sync.stats import query_supabase, query_r2, query_dynamodb
                    query_supabase()
                    query_r2()
                    query_dynamodb()
                except ImportError as ie:
                    logger.error("[-] 统计模块导入失败（不影响爬虫结果）: %s", ie)
                except Exception as e:
                    logger.error("[-] 统计查询失败: %s", e)
            finally:
                logger.info("[*] 正在释放数据库资源...")
                db_manager.close()
                logger.info("[+] 数据库已安全关闭！")
        else:
            # 中断时仅释放资源，跳过统计
            logger.info("[*] 正在释放数据库资源...")
            db_manager.close()
            logger.info("[+] 数据库已安全关闭！")

if __name__ == "__main__":
    main()
