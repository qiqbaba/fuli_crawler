"""
代理IP管理模块（兼容门面）
向后兼容原 ProxyManager 的各种全局方法和常量
"""
from typing import Optional, Dict
from utils.logger import get_logger

logger = get_logger(__name__)

# 导入拆分后的组件与常量以实现向后兼容
from utils.proxy_fetcher import PROXY_SOURCES
from utils.proxy_verifier import PROXY_TEST_URLS
from utils.proxy_pool import ProxyPool

# ========== 全局代理管理器实例 ==========
_proxy_manager: Optional[ProxyPool] = None


def get_proxy_manager() -> Optional[ProxyPool]:
    """获取全局代理管理器实例"""
    global _proxy_manager
    from config import is_local_mode, is_proxy_manager_enabled, PROXY_CACHE_TTL
    local_on = is_local_mode()
    mgr_on = is_proxy_manager_enabled()
    if _proxy_manager is None:
        if local_on or mgr_on:
            _proxy_manager = ProxyPool(cache_ttl=PROXY_CACHE_TTL)
        else:
            return None
    return _proxy_manager


def init_proxy_manager(force_fetch=False, force_verify=False) -> Optional[ProxyPool]:
    """
    初始化并预加载代理管理器
    
    Args:
        force_fetch: 是否强制获取新代理
        force_verify: 是否强制验证代理
        
    Returns:
        ProxyPool 实例或 None
    """
    manager = get_proxy_manager()
    if manager is None:
        return None
    
    if force_fetch or not manager._proxies:
        manager.fetch_proxies(force=force_fetch)
    
    if force_verify or not manager._working_proxies:
        manager.verify_proxies(force=force_verify)
    
    return manager


def get_proxy_string(exclusive: bool = True, source: Optional[str] = None) -> str:
    """
    获取当前代理字符串，遵循 config 的运行时参数覆盖及禁用设置
    
    Args:
        exclusive: 是否使用线程独占模式。默认为 True（给 Playwright 等长效客户端使用）
        source: 针对的爬虫源名称
    """
    from config import get_crawler_proxy, is_proxy_manager_enabled
    
    # 1. 优先使用命令行或环境变量中配置的固定代理
    fixed_proxy = get_crawler_proxy()
    if fixed_proxy:
        return fixed_proxy
        
    # 2. 如果没有指定固定代理且启用了代理IP管理器，使用管理器拿代理
    if is_proxy_manager_enabled():
        manager = get_proxy_manager()
        if manager:
            if exclusive:
                return manager.get_thread_exclusive_proxy(source=source) or ""
            else:
                return manager.get_random_pool_proxy(source=source) or ""
            
    return ""


def get_proxy_dict(exclusive: bool = False, source: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    获取代理字典（用于 requests/curl_cffi 库）
    默认使用非线程独占模式，每次请求使用高频随机轮换的代理IP
    
    Args:
        exclusive: 是否使用线程独占模式。默认为 False（requests 默认不独占）
        source: 针对的爬虫源名称
    Returns:
        {"http": "...", "https": "..."} 或 None
    """
    proxy_url = get_proxy_string(exclusive=exclusive, source=source)
    if proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    return None


if __name__ == "__main__":
    # 测试代理管理器
    logger.info("=" * 60)
    logger.info("代理IP管理器（兼容测试层）测试")
    logger.info("=" * 60)
    
    manager = ProxyPool()
    
    # 获取代理
    count = manager.fetch_proxies(force=True)
    logger.info("获取到 %s 个代理", count)
    
    # 验证代理
    if count > 0:
        working = manager.verify_proxies(force=True, max_workers=100, target_count=5)
        logger.info("可用代理: %s 个", working)
        
        # 显示统计
        stats = manager.get_stats()
        logger.info("统计信息: %s", stats)
        
        # 获取随机代理
        proxy = manager.get_random_proxy()
        logger.info("随机代理: %s", proxy)
    else:
        logger.warning("未获取到代理，请检查网络连接")