"""
严格日本AV番号识别与过滤模块

遵循「宁可漏掉也不要误判，标题包含番号的一律跳过」原则。
主要用于在爬虫流程中（语言过滤之后）或数据库维护中，精准识别日本AV番号，
同时严格排除推特/OF社媒账号、国产原创传媒厂牌、视频规格参数、度量衡与日期年份等非番号内容。
"""

import re
from typing import Tuple, Optional, List


# ============================================================
# 1. 特殊与知名日本AV平台与厂商番号模式
# ============================================================

# 包含特定前缀/结构的知名平台番号（不区分大小写）
SPECIAL_AV_REGEX = re.compile(
    r'(?<![a-zA-Z0-9])('
    r'FC2[-_ ]?PPV[-_ ]?\d{5,8}|'
    r'FC2[-_ ]?\d{5,8}|'
    r'HEYZO[-_ ]?\d{3,5}|'
    r'HEYDOUGA[-_ ]?\d{3,5}[-_ ]?\d{2,5}|'
    r'(?:1PONDO|10MUSUME|CARIBBEANCOM|CARIBBEAN|CARIB|PACOPACOMAMA|PACO)[-_ ]?\d{6}[-_ ]?\d{1,4}|'
    r'TOKYO[-_ ]?HOT[-_ ]?(?:n\d{3,5}|cz\d{2,4}|k\d{3,5}|ge\d{2,4}|red\d{2,4}|\d{3,5})|'
    r'(?:259LUXU|200GANA|300MIUM|261ARA|277MAKI|LUXU|GANA|MIUM|ARA|MAKI)[-_ ]?\d{2,5}|'
    r'(?:C0930|H0930|H4610|S-CUTE|SCUTE)[-_ ]?[a-zA-Z0-9]+|'
    r'(?:T28|DRC|DANDY|RBD|SODVR|DSVR|VRKM|AVGL|S2M|S2MBD|MKBD-S|MBDD|MKMP|MOKP)[-_ ]?\d{2,6}'
    r')(?![a-zA-Z0-9])',
    re.IGNORECASE
)

# 标准日本AV厂商番号：2-6个英文字母 + 连字符(-) + 2-6位数字 (严格限定连字符，排除通配下划线以防用户名误判)
STANDARD_FANHAO_REGEX = re.compile(
    r'(?<![a-zA-Z0-9])([A-Za-z]{2,6}-\d{2,6})(?![a-zA-Z0-9])'
)

# 无码/流出格式：如 C2305-xxxx, 300mium-123
UNCENSORED_LEAK_REGEX = re.compile(
    r'(?<![a-zA-Z0-9])([A-Za-z]\d{4}-[A-Za-z0-9]+|\d{2,4}[A-Za-z]{3,6}-\d{2,6})(?![a-zA-Z0-9])'
)


# ============================================================
# 2. 国产原创与防误判白名单/黑名单
# ============================================================

# 中文国产原创厂牌/自媒体/平台前缀（一律保留，绝不当作日本番号）
CHINESE_DOMESTIC_PREFIXES = {
    # 麻豆系列
    'MD', 'MDX', 'MDSR', 'MDWP', 'MDCM', 'MDHG', 'MDHT', 'MDL', 'MGL', 'MSD', 'MMZ', 'MM', 'MDSJ', 'MXB', 'MKY', 'MDVD', 'MDC', 'MAN', 'DHT',
    # 果冻/91系列
    'GDCM', 'GD', 'GDX', '91KCM', '91YCM', '91CM', '91BCM', '91TCM', '91', 'KCM', 'YCM', 'BCM', 'TCM',
    # 天美系列
    'TMW', 'TM', 'TML', 'TMG', 'TMK',
    # 星空系列
    'XKG', 'XK', 'XKQP', 'XKTV', 'XKTC', 'XKKY', 'XKV', 'XKVP',
    # 精东系列
    'JD', 'JDMY', 'JDSY', 'JDBC', 'JDYL', 'JDTY', 'JDYP', 'JDYA',
    # 兔子先生/杏吧/蜜桃/香蕉/大象/肉肉/冠希/绝对领域/爱豆/辣椒/猫爪/SWAG等
    'TZ', 'DAD', 'XB', 'PMC', 'XJX', 'DA', 'DT', 'RR', 'GX', 'LY', 'ID', 'IDG', 'IA', 'HPP', 'JV', 'SWAG',
    'BLX', 'MPG', 'DMX', 'MFK', 'SZL', 'NHAV', 'XSJ', 'MAD', 'LS', 'FSOG', 'RS', 'DYDX', 'DYBC',
    'RAS', 'MCY', 'AYW', 'EDEA', 'PH', 'ST', 'AY', 'SQ', 'HJ', 'TG', 'WM', 'TC',
    'ND', 'PM', 'QD', 'QDOG', 'KB', 'LADY', 'TW', 'HK', 'CN', 'CC', 'QQ', 'WX', 'WB',
    'DY', 'KS', 'TB', 'XY', 'PDD', 'VIP', 'SVIP', 'HZ', 'SWYP', 'JSBY', 'LJ',
    'BAK', 'YR', 'QX', 'QXF', 'NI', 'CP', 'HC', 'LLS', 'DB', 'DW', 'BA', 'NNS', 'MJPD',
    'ZA', 'IU', 'JK', 'OF', 'SW', 'JY', 'CZ', 'QT', 'VNS', 'DDL', 'KM', 'LMG', 'MT', 'SSN', 'KFC',
    'WS', 'TX', 'XSJKY', 'XG', 'AISI', 'SVDL', 'DS', 'JB', 'ODE', 'WMOG', 'LTV', 'BC'
}

# 国产/中文特色关键词：若标题包含此类关键词，且非明确的 Special AV，则优先判定为国产资源（宁可漏掉也不要误判）
CHINESE_DOMESTIC_KEYWORDS = [
    '传媒', '影业', '制片厂', '映画', '原创', '自购', '福利姬', '网红', '主播', '探花', '大神', '国产',
    '麻豆', '果冻', '天美', '星空', '精东', '杏吧', '蜜桃', '香蕉', '大象', '肉肉', '冠希', '绝对领域',
    '爱豆', '辣椒', '猫爪', '抖阴', '皇家华人', '千禧', '麦尼', '渡边', '帝王', '巨鹿', '萝莉社',
    '性视界', '性世界', '糖心', '乌鸦', '91制片厂', '微密圈', '秀场', '字母圈', '门票', '开票',
    '推特', 'twitter', 'fansone', 'onlyfans', 'swag', 'faphouse', 'stripchat', 'myfans'
]

# 非番号黑名单单词（分辨率、音视频格式、压制组、英文人名、常用英文单词等）
BLACKLIST_WORDS = {
    # 视频格式与参数
    '4K', '8K', '1080P', '720P', '2160P', '480P', '540P', '360P', '240P',
    'H264', 'H265', 'X264', 'X265', 'HEVC', 'AVC', 'HDR', '10BIT', '8BIT', '60FPS', '120FPS', '30FPS',
    'VR180', 'VR360', '3D', 'HD', 'FHD', 'UHD', 'SD', 'BD', 'DVD',
    # 音频与容器
    'MP3', 'MP4', 'MKV', 'AVI', 'WMV', 'FLV', 'RMVB', 'TS', 'M2TS', 'ISO', 'VOB', 'MOV', 'AAC', 'DTS', 'FLAC',
    # 压制组与发布标签
    'WEB', 'DL', 'WEBDL', 'WEBRIP', 'BLURAY', 'BDRIP', 'DVDRIP', 'HDTV', 'CAM', 'TC',
    'P2P', 'XXX', 'WRB', 'KTR', 'RARBG', 'YIFY', 'TGX', 'EZTV', 'ETRG', 'XVX', 'XC', 'NBQ', 'HQ',
    # 集数/章节/版本/编号标志
    'EP', 'E', 'S', 'VOL', 'CH', 'V', 'P', 'PART', 'NO', 'VER', 'SEASON', 'ACT', 'STAGE', 'SCENE', 'ITEM',
    # 漫展/同人
    'C', 'COMIC', 'COMITIA', 'FF', 'FESTIVAL',
    # 域名后缀
    'COM', 'NET', 'ORG', 'VIP', 'CC', 'XYZ', 'TOP', 'ME', 'SITE', 'CLUB', 'ONLINE', 'LIVE', 'APP', 'IO', 'CO',
    # 欧美厂商与常见缩写
    'POV', 'BBC', 'MILF', 'DILF', 'ASMR', 'VR', 'AI', 'AV', 'SZ', 'RQ', 'RAW',
    'VIXEN', 'DEEPER', 'BLACKED', 'TUSHY', 'BRAZZERS', 'SEXART', 'LEGALPORNO', 'NAUGHTY',
    # 度量衡单位
    'CM', 'KG', 'CUP', 'GB', 'MB', 'KB', 'TB', 'FPS',
    # 相机/图片前缀
    'IMG', 'DSC', 'PHOTO', 'PIC',
    # 常见英文单词
    'LOVE', 'GIRL', 'BOY', 'BABY', 'SWEET', 'HOT', 'SEXY', 'SUPER', 'MEGA', 'ULTRA',
    'BEST', 'TOP', 'NEW', 'OLD', 'BIG', 'MINI', 'MAX', 'PRO', 'PLUS', 'LITE',
    'DATE', 'DAY', 'YEAR', 'TIME', 'SHOW', 'LIVE', 'STAR', 'MOON', 'SUN', 'SKY',
    'BLUE', 'RED', 'BLACK', 'WHITE', 'PINK', 'GOLD', 'DARK', 'LIGHT',
    'STUDIO', 'MEDIA', 'CLUB', 'TEAM', 'GROUP', 'WORKS', 'PROJECT',
    'VIDEO', 'MOVIE', 'CLIP', 'FILM', 'PHOTO', 'ALBUM',
    'SUB', 'CHS', 'CHT', 'BIG5', 'GBK', 'UTF8', 'ENG', 'JPN', 'KOR',
    'UNCENSORED', 'CENSORED', 'LEAKED', 'REDUCED', 'MOSAIC', 'DECODER',
    'REMASTERED', 'ENHANCED', 'RESTORED', 'AI',
    # 常见英文/拼音名字
    'KIKI', 'YAYA', 'BELLA', 'SUNNY', 'DAIDAI', 'MOMO', 'NANA', 'COCO', 'LILI', 'MIMI',
    'ANNA', 'LUCY', 'MARY', 'JENNY', 'EMMA', 'ALICE', 'EVA', 'ELENA', 'LINA', 'TINA',
    'JIAJIA', 'XIAO', 'XINXIN', 'WANWAN', 'BUBU', 'MENGMENG', 'TIANTIAN', 'YUMMY', 'YUMI',
    'AURORA', 'OLIVIA', 'SOPHIA', 'LUNA', 'MIA', 'ZOE', 'CHLOE', 'LILY', 'RUBY',
    'LUPIN', 'ALESSANDRO', 'ANDREJ', 'THOMAS', 'REED', 'NEVENA', 'NAOMI', 'NICOLE'
}

# 排除年份与常见分辨率、吉祥数字
BAD_RESOLUTIONS = {240, 360, 480, 540, 576, 720, 960, 1080, 1280, 1920, 2160, 3840}
BAD_YEARS = set(range(1990, 2035))
BAD_LUCKY_NUMBERS = {520, 521, 666, 777, 888, 999, 1314, 5200, 521125, 8888, 88888, 6666, 66666}


# ============================================================
# 3. 核心校验与提取函数
# ============================================================

def is_valid_fanhao(fanhao_candidate: str, title: str, is_special_av: bool = False) -> bool:
    """
    严格判定提取出的候选字符串是否为日本AV番号。
    遵循「宁可漏掉也不要误判」原则。
    
    Args:
        fanhao_candidate: 提取出的番号候选串 (如 "SSIS-963", "FC2-PPV-123456")
        title: 资源原始完整标题
        is_special_av: 是否由 SPECIAL_AV_REGEX 匹配出的知名平台格式
        
    Returns:
        bool: True 表示确属日本AV番号，False 表示非番号
    """
    if not fanhao_candidate:
        return False
    
    fh_upper = fanhao_candidate.upper().strip()
    lower_title = title.lower()
    
    # 1. 明确的 Special AV (如 FC2-PPV-123456, HEYZO-123, 10musume-xxxx, 1pondo-xxxx)
    if is_special_av or SPECIAL_AV_REGEX.fullmatch(fh_upper):
        # 检查上下文，避免网址后缀误判
        lower_candidate = fanhao_candidate.lower()
        start_pos = lower_title.find(lower_candidate)
        if start_pos != -1:
            after_text = lower_title[start_pos + len(lower_candidate):].strip()
            if after_text.startswith(('.com', '.net', '.org', '.vip', '.cc', '.xyz', '.top', '.me', '.site', '.club', '.tv', '.io')):
                return False
        return True
    
    # 2. 如果包含国产特色关键词，优先视为国产内容（不按通用番号过滤，避免误杀国产传媒/短剧）
    if any(kw in lower_title for kw in CHINESE_DOMESTIC_KEYWORDS):
        return False

    # 3. 分解前缀与数字
    if '-' in fh_upper:
        prefix, num_str = fh_upper.split('-', 1)
    else:
        return False
            
    if not prefix or not num_str:
        return False
    
    # 4. 前缀校验
    if prefix in CHINESE_DOMESTIC_PREFIXES:
        return False
    if prefix in BLACKLIST_WORDS:
        return False
    if len(prefix) < 2 or len(prefix) > 6:
        return False
    if not prefix.isalpha():
        # 如果前缀含数字，必须符合特定番号格式如 259LUXU, 200GANA 等
        if not re.match(r'^\d{1,4}[A-Z]{3,5}$', prefix):
            return False

    # 5. 数字部分校验
    clean_num_match = re.match(r'^\d+', num_str)
    if not clean_num_match:
        return False
    num_val = int(clean_num_match.group(0))
    num_len = len(clean_num_match.group(0))
    
    if num_val in BAD_YEARS:
        return False
    if num_val in BAD_RESOLUTIONS:
        return False
    if num_val in BAD_LUCKY_NUMBERS:
        return False
    if num_len > 6 or num_len < 2:
        return False

    # 6. 上下文校验：避免匹配网址/邮箱/社交账号
    lower_candidate = fanhao_candidate.lower()
    start_pos = lower_title.find(lower_candidate)
    if start_pos != -1:
        after_text = lower_title[start_pos + len(lower_candidate):].strip()
        if after_text.startswith(('.com', '.net', '.org', '.vip', '.cc', '.xyz', '.top', '.me', '.site', '.club', '.tv', '.io')):
            return False
        if start_pos > 0 and lower_title[start_pos - 1] in ('@', '＠'):
            return False

    return True


def extract_fanhao(title: str) -> Tuple[bool, Optional[str]]:
    """
    从标题中严格提取日本AV番号。
    
    Args:
        title: 资源标题
        
    Returns:
        (found: bool, matched_fanhao: str or None)
    """
    if not title:
        return False, None
    
    # 1. 优先检测特殊知名AV厂商模式 (FC2, HEYZO, 1PONDO, 10MUSUME, CARIB, TOKYO-HOT 等)
    m_special = SPECIAL_AV_REGEX.search(title)
    if m_special:
        fh = m_special.group(1)
        if is_valid_fanhao(fh, title, is_special_av=True):
            return True, fh

    # 2. 检测无码/流出格式 (如 C2305-xxxx, 300mium-123)
    m_uncensored = UNCENSORED_LEAK_REGEX.search(title)
    if m_uncensored:
        fh = m_uncensored.group(1)
        if is_valid_fanhao(fh, title, is_special_av=False):
            return True, fh

    # 3. 检测标准字母-数字番号 (如 SSIS-123, IPX-504, JUR-090, MIDV-075)
    for m in STANDARD_FANHAO_REGEX.finditer(title):
        candidate = m.group(1)
        if is_valid_fanhao(candidate, title, is_special_av=False):
            return True, candidate

    return False, None


def has_fanhao(title: str) -> bool:
    """
    快速判断标题是否包含番号。
    
    Args:
        title: 资源标题
        
    Returns:
        bool: True 表示包含番号，False 表示不包含
    """
    found, _ = extract_fanhao(title)
    return found


def batch_has_fanhao(titles: List[str]) -> List[bool]:
    """
    批量判断多个标题是否包含番号，返回与 titles 一一对应的布尔值列表。
    
    Args:
        titles: 标题字符串列表
        
    Returns:
        list[bool]: 每个标题是否包含番号
    """
    if not titles:
        return []
    return [has_fanhao(t) for t in titles]
