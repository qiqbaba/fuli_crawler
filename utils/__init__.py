# utils package
from utils.proxy_manager import ProxyManager, get_proxy_manager, init_proxy_manager, get_proxy_string, get_proxy_dict


def setup_console_utf8():
    """
    Windows 控制台强制使用 utf-8 编码输出，防止中文乱码。
    所有入口脚本（main.py、fixes/ 脚本）应调用此函数替代重复代码。
    """
    import sys
    if sys.platform.startswith('win'):
        if sys.stdout.encoding != 'utf-8':
            try:
                sys.stdout.reconfigure(encoding='utf-8')
                sys.stderr.reconfigure(encoding='utf-8')
            except AttributeError:
                pass


__all__ = [
    'ProxyManager', 'get_proxy_manager', 'init_proxy_manager',
    'get_proxy_string', 'get_proxy_dict', 'setup_console_utf8',
]
