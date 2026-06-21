import re
from datetime import datetime, timedelta

def parse_date(date_str):
    """从字符串中提取/推算年份和格式化日期 (YYYY-MM-DD)"""
    if not date_str:
        return "Unknown_Year", "Unknown_Date"
    
    now = datetime.now()
    
    # 格式1: YYYY-MM-DD 或类似
    match_full = re.search(r'(\d{4})[^\d]+(\d{1,2})[^\d]+(\d{1,2})', date_str)
    if match_full:
        year = match_full.group(1)
        month = match_full.group(2).zfill(2)
        day = match_full.group(3).zfill(2)
        return year, f"{year}-{month}-{day}"
        
    # 格式2: X个月前
    match_month_ago = re.search(r'(\d+)\s*个月前[（$](\d{1,2})[^\d]+(\d{1,2})[）$]?', date_str)
    if match_month_ago:
        months_ago = int(match_month_ago.group(1))
        month = match_month_ago.group(2).zfill(2)
        day = match_month_ago.group(3).zfill(2)
        total_months = now.year * 12 + now.month - 1
        target_year = str((total_months - months_ago) // 12)
        return target_year, f"{target_year}-{month}-{day}"
        
    # 格式3: X天前
    match_day_ago = re.search(r'(\d+)\s*天前', date_str)
    if match_day_ago:
        days_ago = int(match_day_ago.group(1))
        target_date = now - timedelta(days=days_ago)
        return str(target_date.year), target_date.strftime("%Y-%m-%d")
        
    # 格式4: 刚刚/小时前/分钟前
    if any(keyword in date_str for keyword in ["刚刚", "小时前", "分钟前"]):
        return str(now.year), now.strftime("%Y-%m-%d")
        
    # 格式5: MM-DD (默认今年)
    match_short = re.search(r'(?<!\d)(\d{1,2})[-/月](\d{1,2})(?:日)?(?!\d)', date_str)
    if match_short:
        year = str(now.year)
        month = match_short.group(1).zfill(2)
        day = match_short.group(2).zfill(2)
        return year, f"{year}-{month}-{day}"
        
    return "Unknown_Year", "Unknown_Date"
