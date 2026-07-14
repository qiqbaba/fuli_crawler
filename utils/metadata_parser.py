import re
from urllib.parse import urlparse, parse_qs, unquote


def _extract_video_photo_count(text, filter_small_p_without_v=False):
    """
    从文本中提取视频数 (V) 和图片数 (P)，返回格式化字符串如 "30V" 或 "137V/537P"。

    Args:
        text: 要搜索的文本
        filter_small_p_without_v: 若为 True，过滤掉 <=5 且无 V 伴随的 P 值
                                  （用于标题解析，避免将 "3P" 玩法误识别为图片数）

    Returns:
        格式化字符串如 "30V/537P"，无匹配时返回 None
    """
    if not text:
        return None

    v_matches = re.findall(r'(?<![a-zA-Z0-9])(\d+)[Vv](?![a-zA-Z0-9])', text)
    p_matches = re.findall(r'(?<![a-zA-Z0-9])(\d+)[Pp](?![a-zA-Z0-9])', text)

    valid_v = [f"{v}V" for v in v_matches]

    valid_p = []
    for p in p_matches:
        p_int = int(p)
        # 排除常见的视频分辨率 1080P, 720P
        if p_int in [1080, 720]:
            continue
        # 单独的小数字 P (如 3P, 4P) 通常代表玩法而不是图片张数
        if filter_small_p_without_v and p_int <= 5 and not v_matches:
            continue
        valid_p.append(f"{p}P")

    if not valid_v and not valid_p:
        return None

    return "/".join(valid_v + valid_p)

def parse_title(title):
    """从标题中提取文件大小 size 和资源形式 resource_format"""
    if not title:
        return None, None
        
    # 1. 优先尝试匹配方括号中的内容，这是绝大多数最规范的格式
    bracket_matches = re.findall(r'\[([^\]]+)\]', title)
    bracket_content = bracket_matches[-1] if bracket_matches else None
    
    size_val = None
    formats = []
    
    if bracket_content:
        parts = bracket_content.split('/')
        for part in parts:
            part = part.strip()
            # 视频 (e.g. 16V) 或者 图片 (e.g. 1077P)
            if re.match(r'^\d+[Vv]$', part):
                formats.append(part.upper())
            elif re.match(r'^\d+[Pp]$', part):
                formats.append(part.upper())
            elif re.match(r'^\d+(?:\.\d+)?\s*(?:[a-zA-Z]+)?$', part):
                size_val = part.upper()
            else:
                if not size_val and not any(c in part.upper() for c in ['V', 'P']):
                    size_val = part
    else:
        # 2. 如果没有方括号，通过正则匹配标题中的特定元数据
        vp_format = _extract_video_photo_count(title, filter_small_p_without_v=True)
        if vp_format:
            formats = vp_format.split('/')

        # 匹配大小，例如 10.9G, 5.83G, 10.9GB, 500MB 等
        size_match = re.search(r'(?<![a-zA-Z0-9])(\d+(?:\.\d+)?\s*[GgMmTt][Bb]?)(?![a-zA-Z0-9])', title)
        if size_match:
            size_val = size_match.group(1).upper()
            
    # 格式化资源形式，例如 "30V" 或 "137V/537P"
    if formats:
        v_parts = [f for f in formats if 'V' in f]
        p_parts = [f for f in formats if 'P' in f]
        resource_format = "/".join(v_parts + p_parts)
    else:
        resource_format = None
        
    return size_val, resource_format

def parse_link_metadata(resource_link):
    """
    尝试从磁力链接或电驴链接中提取 size 和 resource_format
    """
    if not resource_link:
        return None, None
        
    lines = resource_link.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        
        # 1. 尝试解析电驴链接
        if line.lower().startswith("ed2k://"):
            try:
                parts = line.split('|')
                if len(parts) >= 5:
                    raw_name = parts[2]
                    raw_size = parts[3]
                    
                    # 1.1 解析大小
                    size_str = None
                    if raw_size.isdigit():
                        bytes_val = int(raw_size)
                        if bytes_val >= 1024**3:
                            size_str = f"{bytes_val / (1024**3):.2f}GB"
                        else:
                            size_str = f"{bytes_val / (1024**2):.2f}MB"
                    
                    # 1.2 解析资源形式
                    decoded_name = unquote(raw_name)
                    vp_format = _extract_video_photo_count(decoded_name)
                    if vp_format:
                        resource_format = vp_format
                    else:
                        ext_match = re.search(r'\.([a-zA-Z0-9]+)$', decoded_name)
                        resource_format = ext_match.group(1).upper() if ext_match else None

                    return size_str, resource_format
            except Exception:
                pass

        # 2. 尝试解析磁力链接
        elif line.lower().startswith("magnet:?"):
            try:
                parsed = urlparse(line)
                query_params = parse_qs(parsed.query)
                
                size_str = None
                resource_format = None
                
                # 2.1 解析大小
                xl_list = query_params.get('xl')
                if xl_list and xl_list[0].isdigit():
                    bytes_val = int(xl_list[0])
                    if bytes_val >= 1024**3:
                        size_str = f"{bytes_val / (1024**3):.2f}GB"
                    else:
                        size_str = f"{bytes_val / (1024**2):.2f}MB"
                        
                # 2.2 解析资源形式
                dn_list = query_params.get('dn')
                if dn_list:
                    decoded_dn = unquote(dn_list[0])
                    vp_format = _extract_video_photo_count(decoded_dn)
                    if vp_format:
                        resource_format = vp_format
                    else:
                        ext_match = re.search(r'\.([a-zA-Z0-9]+)$', decoded_dn)
                        resource_format = ext_match.group(1).upper() if ext_match else None

                return size_str, resource_format
            except Exception:
                pass
                
    return None, None

def parse_pikpak_link(resource_link):
    """
    尝试从 resource_link 中提取 PikPak 链接
    """
    if not resource_link:
        return None
    match = re.search(r'(https?://[a-zA-Z0-9][-a-zA-Z0-9]{0,62}(?:\.[a-zA-Z0-9][-a-zA-Z0-9]{0,62})*pikpak\.[a-zA-Z]{2,}(?:/[^\s]*)?)', resource_link)
    if match:
        return match.group(1).strip()
    return None


def sanitize_filename(filename):
    """清理文件名中的非法字符，移除表情符号及特殊变体字符防止编码问题"""
    # 替换 Windows 文件名非法字符
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
    # 移除非 BMP 字符（如 Emoji 等 Unicode 码点大于 0xFFFF 的字符）
    filename = re.sub(r'[^\u0000-\uFFFF]', '', filename)
    # 移除特殊的不可见控制字符和变体选择器
    filename = re.sub(r'[\u200b-\u200d\ufe00-\ufe0f\ufeff]', '', filename)
    return filename.strip()

