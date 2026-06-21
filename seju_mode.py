import sys
from config import get_db_path
from utils.db_manager import DBManager
from crawlers.seju_crawler import SejuCrawler

# 强制控制台输出使用 utf-8 编码，防止中文乱码
if sys.platform.startswith('win'):
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

def main():
    db_path = get_db_path()
    db_manager = DBManager(db_path)
    crawler = SejuCrawler(db_manager)
    # 兼容原 seju_mode.py，默认直接以正式模式爬取 1 至 56 页
    try:
        crawler.run(is_test=False, start_page=1, end_page=56)
    finally:
        db_manager.close()

if __name__ == "__main__":
    main()
