import os

# 数据库路径列表（按优先级尝试，若都不存在则在当前目录创建）
DB_PATHS = [
    r"d:\programme\seju\all_data.db",
    r"d:\seju\all_data.db"
]

def get_db_path():
    """获取有效数据库路径，若都不存在则默认在当前目录下创建/使用"""
    for path in DB_PATHS:
        if os.path.exists(path):
            return path
    # 如果都不存在，在当前项目根目录创建
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, "all_data.db")

# PDF 保存基准路径
PDF_BASE_DIR = r"d:\seju\pdf"

# 反爬 User-Agent 列表
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
]
