import sys
from config import get_db_path
from utils.db_manager import DBManager
from crawlers.u3c3_crawler import U3c3Crawler

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
    crawler = U3c3Crawler(db_manager)
    
    is_test = True
    if len(sys.argv) > 1 and sys.argv[1] == '--crawl':
        is_test = False
        
    start = 1
    end = 12865
    if len(sys.argv) > 2:
        try:
            start = int(sys.argv[2])
        except ValueError:
            pass
    if len(sys.argv) > 3:
        try:
            end = int(sys.argv[3])
        except ValueError:
            pass
            
    try:
        crawler.run(is_test=is_test, start_page=start, end_page=end)
    finally:
        db_manager.close()

if __name__ == "__main__":
    main()
