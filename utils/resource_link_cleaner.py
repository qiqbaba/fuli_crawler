"""resource_link 文本行清洗工具

提供统一的资源链接行过滤逻辑，供爬虫与修复脚本共用：
1. 剔除首部的纯标签行（如 "115 ed2k:"、"磁力："、"磁力资源："、"ed2k:" 等）
2. 剔除尾部的说明/推广行（如 "ed2k请用115保存，迅雷等支持ed2k的客户端会失败"、"更多xx资源，尽在 https://..." 等）
3. 剔除与链接挤在同一行的内嵌说明文字（如 "…|/ ed2k请用115保存…"）
4. 整条记录仅剩标签/说明行时清空；普通内容（含纯文字文章）保持原样，不做截断
"""

import re

# 资源/关键信息行判定模式（命中即视为需要保留的行）
RESOURCE_PATTERNS = [
    r'^magnet:\?',
    r'^ed2k://',
    r'^thunder://',
    r'^https?://',
    r'提取码',
    r'解压密码',
    r'天翼',
]

# 纯标签行模式，如 "115 ed2k:"、"磁力："、"磁力资源："、"ed2k:" 等
LABEL_PATTERNS = [
    r'^115\s*ed2k\s*[:：]?\s*$',
    r'^(磁力|磁力链接|磁力资源|磁力链|ed2k|magnet|thunder|迅雷|天翼云盘|天翼网盘|百度网盘|百度云盘|115网盘|夸克网盘|阿里云盘|蓝奏云)\s*[:：]?\s*$',
]

# 说明/推广行模式，如 "ed2k请用115保存，迅雷等支持ed2k的客户端会失败"、"更多xx资源，尽在 https://..." 等
NOTE_PATTERNS = [
    r'^ed2k请用115保存',
    r'^更多.*?(资源|福利|热舞)',
]

# 内嵌说明文字（与链接挤在同一行时，如 "ed2k://...|/ ed2k请用115保存，迅雷等支持ed2k的客户端会失败"）
INLINE_NOTE_PATTERNS = [
    r'[ \t]*ed2k请用115保存[^\n]*',
]


def _strip_inline_notes(text):
    """剔除行内嵌的说明文字片段"""
    for pattern in INLINE_NOTE_PATTERNS:
        text = re.sub(pattern, '', text)
    return text.strip()


def is_resource_line(line):
    """判断一行是否为真正的资源/关键信息行"""
    lowered = (line or '').strip().lower()
    if not lowered:
        return False
    return any(re.search(pattern, lowered) for pattern in RESOURCE_PATTERNS)


def is_label_line(line):
    """判断一行是否为纯标签行（如 "115 ed2k:"、"磁力："）"""
    lowered = (line or '').strip().lower()
    if not lowered:
        return False
    return any(re.match(pattern, lowered) for pattern in LABEL_PATTERNS)


def is_note_line(line):
    """判断一行是否为说明/推广行（如 "ed2k请用115保存…"、"更多xx资源，尽在 https://…"）"""
    lowered = (line or '').strip().lower()
    if not lowered:
        return False
    return any(re.match(pattern, lowered) for pattern in NOTE_PATTERNS)


def clean_resource_lines(lines):
    """清洗资源链接文本行列表，返回清洗后的行列表

    规则：
    1. 去除空行与首尾空白
    2. 剔除行内嵌的说明文字片段（如 "…|/ ed2k请用115保存…"）
    3. 从首部循环去除纯标签行（如 "115 ed2k:"）与说明行
    4. 从尾部循环去除说明/推广行
    5. 若整条记录只剩标签/说明行（无任何真实内容），返回空列表
    6. 纯文字文章/普通内容保持原样，不做截断
    """
    cleaned = []
    for t in (lines or []):
        t = _strip_inline_notes(t.strip())
        if t:
            cleaned.append(t)
    if not cleaned:
        return []

    # 首部：循环去除纯标签行与说明行
    while len(cleaned) > 1 and (is_label_line(cleaned[0]) or is_note_line(cleaned[0])):
        cleaned.pop(0)
    # 尾部：循环去除说明/推广行
    while len(cleaned) > 1 and is_note_line(cleaned[-1]):
        cleaned.pop()

    # 若整条记录都是标签/说明行（无任何真实内容），清空
    if all(is_note_line(l) or is_label_line(l) for l in cleaned):
        return []

    return cleaned


def clean_resource_link(resource_link):
    """清洗多行 resource_link 字符串，返回清洗后的字符串（无变化时返回原值）"""
    if not resource_link:
        return resource_link
    cleaned = clean_resource_lines(resource_link.split('\n'))
    result = '\n'.join(cleaned)
    return result if result != resource_link else resource_link