"""
统一的日志系统模块。

替代原来散落在各文件中的 print() 调用，提供一致的日志格式和级别控制。

用法:
    from utils.logger import get_logger
    logger = get_logger(__name__)
    logger.info("消息")    # 替代 print("[+] ...") 或 print("[*] ...")
    logger.warning("消息") # 替代 print("[!] ...")
    logger.error("消息")   # 替代 print("[-] ...")

    带 source_name 的爬虫实例日志:
    from utils.logger import get_source_logger
    log = get_source_logger(__name__, "madou")
    log.info("消息")       # 输出: [INFO] [madou] 消息
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


class SourceLoggerAdapter(logging.LoggerAdapter):
    """
    为日志消息添加 [source_name] 前缀的适配器，用于区分不同爬虫实例的日志输出。

    用法:
        from utils.logger import SourceLoggerAdapter, get_logger
        logger = get_logger(__name__)
        log = SourceLoggerAdapter(logger, {"source_name": "madou"})
        log.info("消息")  # 输出: [INFO] crawlers.base_crawler [madou]: 消息
    """

    def process(self, msg, kwargs):
        source_name = self.extra.get("source_name", "")
        return f"[{source_name}] {msg}", kwargs


def get_source_logger(name: str, source_name: str) -> SourceLoggerAdapter:
    """
    获取带 source_name 标识的日志适配器，用于爬虫实例区分日志来源。

    用法:
        log = get_source_logger(__name__, "madou")
        log.info("消息")  # 输出: [INFO] crawlers.base_crawler [madou]: 消息

    Args:
        name: logger 名称，通常传入 __name__
        source_name: 爬虫标识名称，如 "madou"、"datang" 等

    Returns:
        SourceLoggerAdapter 实例
    """
    logger = get_logger(name)
    return SourceLoggerAdapter(logger, {"source_name": source_name})