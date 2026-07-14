"""
统一的日志系统模块。

替代原来散落在各文件中的 print() 调用，提供一致的日志格式和级别控制。

用法:
    from utils.logger import get_logger
    logger = get_logger(__name__)
    logger.info("消息")    # 替代 print("[+] ...") 或 print("[*] ...")
    logger.warning("消息") # 替代 print("[!] ...")
    logger.error("消息")   # 替代 print("[-] ...")
"""

import logging
import sys


# 根 logger 名称
_ROOT_LOGGER_NAME = "fuli_crawler"

# 日志格式：保留原始 print 风格的前缀符号，便于阅读
_FORMAT = "[%(levelname)s] %(name)s: %(message)s"

# 控制台 handler（输出到 stderr，避免干扰 stdout 管道输出）
_console_handler: logging.Handler = None


def _get_console_handler() -> logging.Handler:
    """获取或创建全局控制台 handler"""
    global _console_handler
    if _console_handler is not None:
        return _console_handler

    # 确保 Windows 控制台支持 utf-8 输出
    if sys.platform.startswith('win'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_FORMAT)
    handler.setFormatter(formatter)
    _console_handler = handler
    return handler


def setup_logging(level: str = "INFO") -> None:
    """
    初始化根日志配置。通常在程序入口处调用一次即可。

    Args:
        level: 日志级别，可选 DEBUG / INFO / WARNING / ERROR (默认: INFO)
    """
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 避免重复添加 handler
    if not logger.handlers:
        logger.addHandler(_get_console_handler())

    # 阻止日志传播到根 logger，避免重复输出
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的 logger 实例。

    用法:
        logger = get_logger(__name__)  # 输出: [INFO] crawlers.base_crawler: 消息

    Args:
        name: logger 名称，通常传入 __name__

    Returns:
        logging.Logger 实例
    """
    # 确保根 logger 已配置（如果还未调用 setup_logging）
    root_logger = logging.getLogger(_ROOT_LOGGER_NAME)
    if not root_logger.handlers:
        root_logger.addHandler(_get_console_handler())
        root_logger.setLevel(logging.INFO)
        root_logger.propagate = False

    # 如果 name 以 __main__ 开头，简化显示
    if name == "__main__":
        return logging.getLogger(_ROOT_LOGGER_NAME)

    # 返回子 logger: fuli_crawler.<name>
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")