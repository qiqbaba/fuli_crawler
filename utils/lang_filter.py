"""
日语标题检测工具

提供 is_japanese() 函数，用于判断文本是否为日语。
检测策略：
  1. 快速预过滤：检查是否包含假名（平假名/片假名）
  2. 假名占比阈值：假名占有效字符比例 >= 20% 才进入精确检测
  3. 精确检测（可选）：若安装了 lingua-language-detector，使用其精确判断
"""

import re
import sys

# 假名占有效字符比例阈值
JAPANESE_CHAR_RATIO_THRESHOLD = 0.2

try:
    from lingua import Language, LanguageDetectorBuilder

    # 只加载日语和中文模型，最小化资源占用
    _detector = LanguageDetectorBuilder.from_languages(
        Language.JAPANESE, Language.CHINESE
    ).with_minimum_relative_distance(0.5).build()

    _LINGUA_AVAILABLE = True
except ImportError:
    _LINGUA_AVAILABLE = False


def is_japanese(text):
    """
    判断文本是否为日语。
    先快速预过滤（检查是否有假名），再按假名占比过滤，最后调用 lingua 精确检测。
    返回: bool
    """
    if not text:
        return False

    # 统计假名数量（平假名 + 片假名）
    jp_chars = len(re.findall(r'[\u3040-\u309F\u30A0-\u30FF]', text))
    if jp_chars == 0:
        return False

    # 统计有效字符数（排除空格和常见标点）
    meaningful_chars = len(
        re.findall(r'[^\s\-_,.;:!?()\[\]{}【】「」『』《》<>・/\\~`@#$%^&*+=|"\']', text)
    )
    if meaningful_chars == 0:
        return False

    # 假名占比必须达到阈值
    if jp_chars / meaningful_chars < JAPANESE_CHAR_RATIO_THRESHOLD:
        return False

    # lingua 精确检测
    if not _LINGUA_AVAILABLE:
        # 没有 lingua 库时，仅靠假名占比判断
        return True

    try:
        lang = _detector.detect_language_of(text)
        return lang == Language.JAPANESE
    except Exception:
        return False