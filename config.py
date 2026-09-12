import os
import sys
import threading
from functools import lru_cache
from typing import Dict, Optional

# ========== 项目根目录（用于解析相对路径，跨平台兼容） ==========
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 尝试加载本地 .env 文件（本地开发时使用，CI 环境中无效但无副作用）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from utils.logger import get_logger
logger = get_logger(__name__)


def _sanitize_dead_proxies():
    """
    自愈机制：检测系统代理与环境变量中的代理是否可用。
    在 Windows 下，当本地代理软件（如 Clash、v2rayN 等）异常关闭但系统代理（注册表 ProxyEnable=1）未关闭时，
    urllib.request.getproxies() 会返回不可用的 127.0.0.1:7890，导致 boto3、httpx、requests 等全部报
    ProxyConnectionError: Failed to connect to proxy URL: "http://127.0.0.1:7890" 或 WinError 10061。
    本函数在启动时快速探测该代理端口，若无法连通则自动屏蔽该失效代理，保证程序可直接直连云端服务（R2、Supabase、DynamoDB）。
    """
    import socket
    import urllib.request
    from urllib.parse import urlparse

    try:
        proxies = urllib.request.getproxies()
        if not proxies:
            return

        dead_proxies = {}
        for proto, proxy_url in list(proxies.items()):
            if not proxy_url:
                continue
            test_url = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
            try:
                parsed = urlparse(test_url)
                host = parsed.hostname
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if not host:
                    continue

                if host in ("127.0.0.1", "localhost", "::1"):
                    with socket.create_connection((host, port), timeout=0.3):
                        pass
            except Exception:
                dead_proxies[proto] = proxy_url

        if dead_proxies:
            logger.warning(
                "检测到系统/环境代理已开启但目标端口未连通 %s，自动屏蔽失效代理以启用直连",
                dead_proxies,
            )
            os.environ["NO_PROXY"] = "*"
            os.environ["no_proxy"] = "*"

            for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                if var in os.environ:
                    os.environ.pop(var, None)

            orig_getproxies = urllib.request.getproxies

            def _clean_getproxies():
                curr = orig_getproxies()
                return {k: v for k, v in curr.items() if k not in dead_proxies}

            urllib.request.getproxies = _clean_getproxies
    except Exception as e:
        logger.debug("检测代理健康状态失败: %s", e)


_sanitize_dead_proxies()
sanitize_dead_proxies = _sanitize_dead_proxies

# ========== 运行模式配置 ==========
_force_mode = None  # 可选值为 'local' 或 'cloud'
_force_mode_lock = threading.Lock()

def set_run_mode(mode):
    """设置运行模式（通常由 main.py 命令行参数指定）"""
    global _force_mode
    with _force_mode_lock:
        if mode in ('local', 'cloud'):
            _force_mode = mode

def is_local_mode():
    """判断当前是否为本地模式"""
    with _force_mode_lock:
        if _force_mode == 'local':
            return True
        if _force_mode == 'cloud':
            return False
    # 默认 auto 模式：如果未检测到 GitHub Actions 环境，则判定为本地模式
    return os.environ.get("GITHUB_ACTIONS") != "true"

# ========== 浏览器引擎配置 ==========
# 可选值: "camoufox"（默认，反检测 Firefox 内核，试运行中）或 "chromium"（原有方案，保留作回退）
BROWSER_ENGINE = os.environ.get("BROWSER_ENGINE", "camoufox").strip().lower()

def get_browser_engine():
    """获取当前浏览器引擎，返回 'camoufox' 或 'chromium'"""
    return "camoufox" if BROWSER_ENGINE == "camoufox" else "chromium"

# ========== 数据库配置 ==========
# 优先读取环境变量（云端运行）；若无则使用本地 SQLite 路径（本地开发）
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# 本地 SQLite 回退路径（优先检查项目根目录，跨平台兼容）
_LOCAL_DB_PATHS = [
    os.path.join(PROJECT_ROOT, "..", "seju", "all_data.db"),
    os.path.join(PROJECT_ROOT, "..", "..", "seju", "all_data.db")
]
DB_PATHS = _LOCAL_DB_PATHS


def get_db_path():
    """获取有效的本地 SQLite 数据库路径（仅在未配置 Supabase 时使用）"""
    for path in _LOCAL_DB_PATHS:
        if os.path.exists(path):
            return path
    return os.path.join(PROJECT_ROOT, "all_data.db")

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
        # 验证目录是否存在，若不存在则尝试创建
        if os.path.exists(pdf_dir):
            return pdf_dir
        try:
            os.makedirs(pdf_dir, exist_ok=True)
            logger.info("已创建 PDF 输出目录: %s", pdf_dir)
            return pdf_dir
        except Exception as e:
            logger.warning("PDF_BASE_DIR 环境变量指向的目录不可用 (%s)，回退到默认路径: %s", e, pdf_dir)
    # 否则，如果是本地模式，检查默认的几个备选路径（相对于项目根目录）
    _LOCAL_PDF_PATHS = [
        os.path.join(PROJECT_ROOT, "..", "seju", "pdf"),
        os.path.join(PROJECT_ROOT, "..", "..", "seju", "pdf")
    ]
    for path in _LOCAL_PDF_PATHS:
        if os.path.exists(path):
            return path
    # 默认 fallback 到当前目录下的 pdf 子目录，确保目录存在
    default_path = os.path.join(PROJECT_ROOT, "pdf")
    os.makedirs(default_path, exist_ok=True)
    return default_path

PDF_BASE_DIR = _get_default_pdf_base_dir()

# ========== AWS 配置（DynamoDB 等） ==========
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")

# ========== 反爬 User-Agent 列表 ==========
USER_AGENTS = [
    # Chrome 120 (与爬虫 impersonate="chrome120" 版本对齐，减少指纹不一致风险)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
# 代理验证时是否校验 SSL 证书（默认 True，关闭存在 MITM 风险）
PROXY_VERIFY_SSL = os.environ.get("PROXY_VERIFY_SSL", "true").lower() == "true"
# 代理验证并发线程数（惰性求值，首次访问时根据环境变量或硬件计算）
@lru_cache(maxsize=None)
def get_proxy_verify_workers():
    """
    获取代理验证并发数（环境变量 PROXY_VERIFY_WORKERS 优先，否则惰性计算硬件适配值）
    """
    env_val = os.environ.get("PROXY_VERIFY_WORKERS")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return _compute_auto_workers()


def _compute_auto_workers(base_multiplier=30, max_limit=300, min_limit=50):
    try:
        # 1. 获取 CPU 核心数
        cpu_count = os.cpu_count() or 1
        workers = cpu_count * base_multiplier
        
        # 2. 根据内存调整（尝试使用 psutil，若未安装则自动跳过）
        try:
            import psutil
            mem = psutil.virtual_memory()
            total_gb = mem.total / (1024 ** 3)
            
            # 针对低配机器（例如 1G 内存的轻量云服务器）强行限制并发，防止 OOM
            if total_gb < 1.5:
                workers = min(workers, 50)
            elif total_gb < 3.5:
                workers = min(workers, 120)
        except ImportError:
            # 未安装 psutil 时，如果 CPU 核心数极少（例如 1 核），给一个温和的并发值
            if cpu_count == 1:
                workers = min(workers, 60)
                
        # 3. 针对操作系统的限制做保护
        # Windows 的 IOCP/Select 在高并发下较容易达到网络句柄瓶颈，在此限制最高并发
        if sys.platform.startswith("win"):
            max_limit = min(max_limit, 200)
            
    except Exception:
        # 任何异常情况下回退到安全的默认并发数
        workers = 80
        
    # 限制在安全区间内 [min_limit, max_limit]
    return max(min_limit, min(workers, max_limit))


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


def get_effective_proxy(exclusive: bool = False, source: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    统一代理获取入口：固定代理 > 代理池 > 无代理
    
    Args:
        exclusive: 是否为 Playwright 等长连接客户端获取独占代理
        source: 针对的爬虫源名称
    Returns:
        {"http": "...", "https": "..."} 或 None
    """
    fixed = get_crawler_proxy()
    if fixed:
        return {"http": fixed, "https": fixed}
    if is_proxy_manager_enabled():
        from utils.proxy_manager import get_proxy_dict
        return get_proxy_dict(exclusive=exclusive, source=source)
    return None


def get_effective_proxy_string(exclusive: bool = True, source: Optional[str] = None) -> str:
    """
    统一代理字符串获取入口：固定代理 > 代理池 > 直连
    
    Args:
        exclusive: 是否为 Playwright 等长连接客户端获取独占代理
        source: 针对的爬虫源名称
    Returns:
        代理 URL 字符串，或空字符串（直连）
    """
    fixed = get_crawler_proxy()
    if fixed:
        return fixed
    if is_proxy_manager_enabled():
        from utils.proxy_manager import get_proxy_string
        return get_proxy_string(exclusive=exclusive, source=source)
    return ""


# ========== 反检测 / Stealth 配置 ==========
# 是否启用高级 stealth 注入（推荐 True）
ENABLE_STEALTH = os.environ.get("ENABLE_STEALTH", "true").lower() == "true"

# 浏览器类型固定为 chromium，Workflow 只安装了 Chromium
# 本地开发时是否使用 headful（有头）模式
# headful 模式不会被大多数反爬系统标记为自动化浏览器
HEADFUL_LOCAL = os.environ.get("HEADFUL_LOCAL", "true").lower() == "true"

# 云端运行时是否也使用 headful 模式（默认 false，因为无图形界面）
HEADFUL_CLOUD = os.environ.get("HEADFUL_CLOUD", "false").lower() == "true"

# ========== 运行时 Stealth 覆盖（由 main.py 命令行参数指定） ==========
_runtime_disable_stealth = False
_runtime_force_headless = False


def set_runtime_stealth(disable_stealth=False, force_headless=False):
    """设置运行时反检测参数（通常由 main.py 命令行参数指定）"""
    global _runtime_disable_stealth, _runtime_force_headless
    _runtime_disable_stealth = disable_stealth
    _runtime_force_headless = force_headless


def is_stealth_enabled():
    """判断 stealth 是否启用（支持运行时覆盖）"""
    if _runtime_disable_stealth:
        return False
    return ENABLE_STEALTH


def is_headless_forced():
    """判断是否强制 headless 模式（支持运行时覆盖）"""
    return _runtime_force_headless
