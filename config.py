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
    # Chrome (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    # Chrome (Linux)
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # Firefox (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    # Firefox (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:138.0) Gecko/20100101 Firefox/138.0",
    # Firefox (Linux)
    "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
    # Edge (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
    # Safari (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
]

# ========== 爬虫全局代理 ==========
# 可在 GitHub Secrets 或本地环境变量中配置，格式如 http://user:pass@host:port 或 http://host:port
CRAWLER_PROXY = os.environ.get("CRAWLER_PROXY", "")

# ========== 代理IP管理器配置 ==========
# 是否启用自动代理管理（从免费代理源获取并轮换代理）
ENABLE_PROXY_MANAGER = os.environ.get("ENABLE_PROXY_MANAGER", "false").lower() == "true"
# 代理缓存有效期（秒），默认12小时（43200秒）
PROXY_CACHE_TTL = int(os.environ.get("PROXY_CACHE_TTL", "43200"))
# 代理验证超时时间（秒）
PROXY_VERIFY_TIMEOUT = int(os.environ.get("PROXY_VERIFY_TIMEOUT", "10"))
# 代理验证并发线程数
PROXY_VERIFY_WORKERS = int(os.environ.get("PROXY_VERIFY_WORKERS", "100"))

# ========== 运行时代理覆盖（由 main.py 命令行参数设置） ==========
_runtime_proxy_override = None
_runtime_disable_proxy = False
_runtime_enable_proxy_manager = None


def set_runtime_proxy(proxy_url, disable_proxy=False, enable_proxy_manager=None):
    """设置运行时代理参数（通常由 main.py 命令行参数指定）"""
    global _runtime_proxy_override, _runtime_disable_proxy, _runtime_enable_proxy_manager
    # Bug 9 修复：只有当 proxy_url 不为 None 时才设置覆盖值，避免空字符串覆盖环境变量配置
    if proxy_url is not None:
        _runtime_proxy_override = proxy_url
    _runtime_disable_proxy = disable_proxy
    if enable_proxy_manager is not None:
        _runtime_enable_proxy_manager = enable_proxy_manager


def get_crawler_proxy():
    """获取当前生效的固定代理地址（支持运行时覆盖）"""
    if _runtime_disable_proxy:
        return ""
    if _runtime_proxy_override is not None:
        return _runtime_proxy_override
    return CRAWLER_PROXY


def is_proxy_manager_enabled():
    """判断代理管理器是否启用（支持运行时覆盖）"""
    if _runtime_disable_proxy:
        return False
    if _runtime_enable_proxy_manager is not None:
        return _runtime_enable_proxy_manager
    return ENABLE_PROXY_MANAGER
