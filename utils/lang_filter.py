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

    _LINGUA_AVAILABLE = True
    _detector = None  # 惰性加载，首次使用时才构建
except ImportError:
    _LINGUA_AVAILABLE = False


def _get_detector():
    """惰性初始化 LanguageDetector，仅在首次调用时加载模型"""
    global _detector
    if _detector is None and _LINGUA_AVAILABLE:
        # 只加载日语和中文模型，最小化资源占用
        _detector = LanguageDetectorBuilder.from_languages(
            Language.JAPANESE, Language.CHINESE
        ).with_minimum_relative_distance(0.5).build()
    return _detector


def _is_japanese_candidate(text: str) -> bool:
    """
    快速预过滤逻辑：检查文本是否有足够的假名占比
    """
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
    return jp_chars / meaningful_chars >= JAPANESE_CHAR_RATIO_THRESHOLD


def is_japanese(text):
    """
    判断文本是否为日语。
    先快速预过滤（检查是否有假名），再按假名占比过滤，最后调用 lingua 精确检测。
    返回: bool
    """
    if not text:
        return False

    if not _is_japanese_candidate(text):
        return False

    # lingua 精确检测
    if not _LINGUA_AVAILABLE:
        # 没有 lingua 库时，仅靠假名占比判断
        return True

    try:
        detector = _get_detector()
        if detector is None:
            return True
        lang = detector.detect_language_of(text)
        return lang == Language.JAPANESE
    except Exception:
        return False


def batch_is_japanese(titles):
    """
    批量判断多个标题是否为日语，减少 lingua 模型重复加载开销。
    
    策略：
    1. 先用快速正则预过滤所有标题，筛出"疑似日语"的候选
    2. 对候选列表一次性调用 lingua 批量检测（若可用）
    
    Args:
        titles: 标题字符串列表
    
    Returns:
        list[bool]: 与 titles 一一对应的日语判断结果
    """
    if not titles:
        return []

    # 第一步：快速正则预过滤（无模型开销）
    candidates = {}  # index -> title
    results = [False] * len(titles)

    for i, text in enumerate(titles):
        if text and _is_japanese_candidate(text):
            candidates[i] = text

    # 无候选，全部返回 False
    if not candidates:
        return results

    # 无 lingua 时，候选全部视为日语
    if not _LINGUA_AVAILABLE:
        for i in candidates:
            results[i] = True
        return results

    # 第二步：lingua 批量检测
    try:
        detector = _get_detector()
        if detector is None:
            for i in candidates:
                results[i] = True
            return results

        # 批量检测：lingua 支持 detect_languages_of 批量接口
        if hasattr(detector, 'detect_languages_of'):
            detected = detector.detect_languages_of(list(candidates.values()))
            for (i, _), lang in zip(candidates.items(), detected):
                results[i] = (lang == Language.JAPANESE)
        else:
            # 回退：逐个检测
            for i, text in candidates.items():
                lang = detector.detect_language_of(text)
                results[i] = (lang == Language.JAPANESE)
    except Exception:
        # 检测失败时，保守地视为日语
        for i in candidates:
            results[i] = True

    return results