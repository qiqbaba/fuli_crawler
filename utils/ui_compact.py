import re
import streamlit as st

# 覆盖常见 Emoji / 箭头 / 数学符号 / 变体选择符 / ZWJ 连接序列
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # 扩展 Emoji 主区
    "\u2190-\u27BF"           # 箭头 / 数学符号 / 杂项符号
    "\u2B00-\u2BFF"           # 杂项符号和箭头
    "\uFE0F"                   # 变体选择符 (VS16)
    "\u200D"                   # 零宽连接符 (ZWJ)
    "]+"
)


def T(text):
    """精简图标模式：剥离字符串中的装饰性 Emoji 图标。

    - 未开启精简模式时，原样返回。
    - 开启后，去除所有匹配到的 Emoji/箭头，并清理首尾空白。
    - 仅作为显示层辅助（不用作业务取值），避免破坏内部逻辑判断。
    """
    if not isinstance(text, str):
        return text
    if not st.session_state.get("compact_icons", True):
        return text
    cleaned = re.sub(r"[ \t]+", " ", _EMOJI_RE.sub("", text)).strip()
    # 若剥离后为空（如纯图标按钮），回退保留原文本，避免控件内容消失
    return cleaned if cleaned else text
