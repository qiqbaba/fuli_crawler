import os

# 尝试加载本地 .env 文件（本地开发时使用，CI 环境中无效但无副作用）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ========== 运行模式配置 ==========
_force_mode = None  # 可选值为 'local' 或 'cloud'

def set_run_mode(mode):
    """设置运行模式（通常由 main.py 命令行参数指定）"""
    global _force_mode
    if mode in ('local', 'cloud'):
        _force_mode = mode

def is_local_mode():
    """判断当前是否为本地模式"""
    if _force_mode == 'local':
        return True
    if _force_mode == 'cloud':
        return False
    # 默认 auto 模式：如果未检测到 GitHub Actions 环境，则判定为本地模式
    return os.environ.get("GITHUB_ACTIONS") != "true"

# ========== 数据库配置 ==========
# 优先读取环境变量（云端运行）；若无则使用本地 SQLite 路径（本地开发）
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# 本地 SQLite 回退路径（仅本地开发使用）
_LOCAL_DB_PATHS = [
    r"d:\programme\seju\all_data.db",
    r"d:\seju\all_data.db"
]
DB_PATHS = _LOCAL_DB_PATHS


def get_db_path():
    """获取有效的本地 SQLite 数据库路径（仅在未配置 Supabase 时使用）"""
    for path in _LOCAL_DB_PATHS:
        if os.path.exists(path):
            return path
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, "all_data.db")

def use_supabase():
    """判断是否使用 Supabase（通过环境变量是否配置来决定）"""
    if is_local_mode():
        return False
    return bool(SUPABASE_URL and SUPABASE_KEY)

# ========== Cloudflare R2 配置 ==========
R2_ACCOUNT_ID      = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID   = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME     = os.environ.get("R2_BUCKET_NAME", "")
R2_ENDPOINT_URL    = os.environ.get("R2_ENDPOINT_URL", "")

def use_r2():
    """判断是否使用 Cloudflare R2 存储 PDF"""
    if is_local_mode():
        return False
    return bool(R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME and R2_ENDPOINT_URL)

# PDF 本地存储目录
def _get_default_pdf_base_dir():
    pdf_dir = os.environ.get("PDF_BASE_DIR")
    if pdf_dir:
        return pdf_dir
    # 否则，如果是本地模式，检查默认的几个备选路径
    _LOCAL_PDF_PATHS = [
        r"d:\programme\seju\pdf",
        r"d:\seju\pdf"
    ]
    for path in _LOCAL_PDF_PATHS:
        if os.path.exists(path):
            return path
    # 默认 fallback 到当前目录下的 pdf 子目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, "pdf")

PDF_BASE_DIR = _get_default_pdf_base_dir()

# ========== 反爬 User-Agent 列表 ==========
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
]
