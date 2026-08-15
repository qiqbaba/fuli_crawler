# utils package


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


def ensure_project_root():
    """
    将项目根目录加入 sys.path，使 `from config import ...` 等导入在任何子目录脚本中均可工作。
    返回项目根目录的绝对路径。
    
    用法:
        import sys; sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    可替换为:
        from utils import ensure_project_root; ensure_project_root()
    """
    import os
    import sys
    caller_file = sys._getframe(1).f_globals.get('__file__', __file__)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(caller_file)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    return project_root


def __getattr__(name):
    """惰性导入代理池相关依赖，避免轻量工具脚本因缺少可选网络依赖而报错"""
    if name == 'ProxyPool':
        from utils.proxy_pool import ProxyPool
        return ProxyPool
    elif name in (
        'get_proxy_manager', 'init_proxy_manager', 'get_proxy_string',
        'get_proxy_dict', 'report_failure', 'report_success'
    ):
        import utils.proxy_manager as pm
        return getattr(pm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    'ProxyPool', 'get_proxy_manager', 'init_proxy_manager',
    'get_proxy_string', 'get_proxy_dict', 'report_failure', 'report_success',
    'setup_console_utf8', 'ensure_project_root',
]