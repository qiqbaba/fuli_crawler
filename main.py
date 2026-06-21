import sys
import argparse
from config import get_db_path
from utils.db_manager import DBManager
from crawlers.seju_crawler import SejuCrawler
from crawlers.u3c3_crawler import U3c3Crawler

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
        required=True,
        choices=["seju", "u3c3"],
        help="指定运行哪一个网站的爬虫 (seju 或 u3c3)"
    )
    
    # 互斥参数：测试模式或正式爬取模式必须选择一个（默认是测试模式，为了防止手误直接开爬）
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--test", "-t",
        action="store_true",
        default=True,
        help="运行测试模式，提取前 5 条测试数据解析并输出，不入库 (默认激活)"
    )
    mode_group.add_argument(
        "--crawl",
        action="store_true",
        help="运行正式爬取模式，保存数据至数据库"
    )
    
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="起始页码 (默认: 1)"
    )
    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="结束页码 (seju默认: 56, u3c3默认: 12865)"
    )
    
    args = parser.parse_args()
    
    # 获取数据库路径并初始化
    db_path = get_db_path()
    print(f"[*] 正在初始化数据库管理，使用数据库文件: {db_path}")
    db_manager = DBManager(db_path)
    
    # 动态匹配爬虫
    crawler = None
    default_end = 1
    
    if args.crawler == "seju":
        crawler = SejuCrawler(db_manager)
        default_end = 56
    elif args.crawler == "u3c3":
        crawler = U3c3Crawler(db_manager)
        default_end = 12865
        
    if crawler is None:
        print(f"[-] 找不到指定的爬虫: {args.crawler}")
        db_manager.close()
        sys.exit(1)
        
    start_page = args.start
    end_page = args.end if args.end is not None else default_end
    
    # 判断运行模式
    # 如果用户显式传入了 --crawl，则 args.test 会因为互斥组自动为 False
    # 如果没有传 --crawl，根据互斥组默认值，args.test 将会是 True
    is_test = args.test
    if args.crawl:
        is_test = False
        
    print(f"[*] 模式: {'【测试模式 (不入库)】' if is_test else '【正式爬取模式 (入库)】'}")
    print(f"[*] 页码范围: {start_page} 到 {end_page}")
    
    try:
        crawler.run(is_test=is_test, start_page=start_page, end_page=end_page)
    finally:
        print("[*] 正在释放数据库资源...")
        db_manager.close()
        print("[+] 数据库已安全关闭！")

if __name__ == "__main__":
    main()
