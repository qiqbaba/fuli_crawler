import sys
import argparse
from config import get_db_path, use_supabase, SUPABASE_URL, SUPABASE_KEY, is_local_mode
from utils.db_manager import DBManager, SupabaseDBManager
from crawlers.seju_crawler import SejuCrawler
from crawlers.u3c3_crawler import U3c3Crawler
from crawlers.datang_crawler import DatangCrawler

# Windows下控制台强制使用utf-8编码输出，防止中文乱码
if sys.platform.startswith('win'):
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

def main():
    parser = argparse.ArgumentParser(description="多网站通用数据爬虫统一入口")
    parser.add_argument(
        "--crawler", "-c",
        required=False,
        choices=["seju", "u3c3", "datang"],
        help="指定运行哪一个网站的爬虫 (seju, u3c3 或 datang)"
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
        help="并发线程数 (默认: seju为3, u3c3为30, datang为40)"
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
    
    args = parser.parse_args()
    
    # 如果没有指定 --crawler，进行交互式交互询问
    if not args.crawler:
        print("[*] 未通过命令行指定爬虫模块，请选择要运行的爬虫：")
        print("    1. seju (默认)")
        print("    2. u3c3")
        print("    3. datang")
        try:
            choice = input("请输入序号 [1/2/3] (直接回车默认 1): ").strip()
            if choice == "2":
                args.crawler = "u3c3"
            elif choice == "3":
                args.crawler = "datang"
            else:
                args.crawler = "seju"
        except (KeyboardInterrupt, EOFError):
            print("\n[-] 运行已取消")
            sys.exit(0)
    
    # 设定全局运行模式
    if args.mode != "auto":
        from config import set_run_mode
        set_run_mode(args.mode)
    
    # 根据环境变量和模式自动选择数据库后端
    if use_supabase():
        print(f"[*] 检测到 Supabase 配置，使用 Supabase PostgreSQL 数据库")
        db_manager = SupabaseDBManager(SUPABASE_URL, SUPABASE_KEY)
    else:
        db_path = get_db_path()
        if is_local_mode():
            print(f"[*] 本地模式已激活，使用本地 SQLite 数据库: {db_path}")
        else:
            print(f"[*] 未检测到 Supabase 配置，使用本地 SQLite 数据库: {db_path}")
        db_manager = DBManager(db_path)
    
    # 动态匹配爬虫
    crawler = None
    default_end = 1
    
    if args.crawler == "seju":
        crawler = SejuCrawler(db_manager)
        default_end = 4
        if args.workers is None:
            args.workers = 3
    elif args.crawler == "u3c3":
        crawler = U3c3Crawler(db_manager)
        default_end = 20
        if args.workers is None:
            args.workers = 30
    elif args.crawler == "datang":
        crawler = DatangCrawler(db_manager)
        default_end = 60
        if args.workers is None:
            args.workers = 40
        
    if crawler is None:
        print(f"[-] 找不到指定的爬虫: {args.crawler}")
        db_manager.close()
        sys.exit(1)
        
    start_page = args.start
    if start_page is None:
        if sys.stdin.isatty():
            try:
                val = input("[*] 请输入起始页码 (直接回车默认 1): ").strip()
                if val:
                    start_page = int(val)
                else:
                    start_page = 1
            except (KeyboardInterrupt, EOFError):
                print("\n[-] 运行已取消")
                sys.exit(0)
            except ValueError:
                print("[-] 输入无效，使用默认起始页码: 1")
                start_page = 1
        else:
            start_page = 1

    end_page = args.end
    if end_page is None:
        if sys.stdin.isatty():
            try:
                val = input(f"[*] 请输入结束页码 (直接回车默认 {default_end}): ").strip()
                if val:
                    end_page = int(val)
                else:
                    end_page = default_end
            except (KeyboardInterrupt, EOFError):
                print("\n[-] 运行已取消")
                sys.exit(0)
            except ValueError:
                print(f"[-] 输入无效，使用默认结束页码: {default_end}")
                end_page = default_end
        else:
            end_page = default_end

    # 做基本的验证
    if start_page < 1:
        print("[!] 起始页码不能小于 1，已自动设为 1")
        start_page = 1
    if end_page < start_page:
        print(f"[!] 结束页码不能小于起始页码 ({start_page})，已自动设为 {start_page}")
        end_page = start_page
    
    is_test = args.test
    if args.crawl:
        is_test = False
        
    print(f"[*] 模式: {'【测试模式 (不入库)】' if is_test else '【正式爬取模式 (入库)】'}")
    print(f"[*] 页码范围: {start_page} 到 {end_page}")
    
    try:
        crawler.run(
            is_test=is_test,
            start_page=start_page,
            end_page=end_page,
            max_workers=args.workers,
            no_early_stop=args.no_early_stop,
            quiet=args.quiet
        )
    finally:
        print("[*] 正在释放数据库资源...")
        db_manager.close()
        print("[+] 数据库已安全关闭！")

if __name__ == "__main__":
    main()
