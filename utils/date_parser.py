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
    match_month_ago = re.search(r'(\d+)\s*个?月前(?:[（(]?(\d{1,2})[^\d]+(\d{1,2})[）)])?', date_str)
    if match_month_ago:
        months_ago = int(match_month_ago.group(1))
        # 计算目标年份和月份，避免跨年错误
        total_months = now.year * 12 + now.month - 1 - months_ago
        target_year = total_months // 12
        target_month = total_months % 12 + 1
        if match_month_ago.group(2) and match_month_ago.group(3):
            month = match_month_ago.group(2).zfill(2)
            day = match_month_ago.group(3).zfill(2)
            return str(target_year), f"{target_year}-{month}-{day}"
        return str(target_year), f"{target_year}-{str(target_month).zfill(2)}-{now.strftime('%d')}"        
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
    # Bug 11 修复：增加负向前瞻 (?!\d) 和负向后顾，防止误匹配四位数字中的片段
    # 例如 "10-11-2024" 应已被格式1捕获；此处只匹配纯粹的 MM-DD 格式
    match_short = re.search(r'(?<!\d)(\d{1,2})[-/月](\d{1,2})(?:日)?(?!\d)', date_str)
    if match_short:
        month_val = int(match_short.group(1))
        day_val = int(match_short.group(2))
        # 月份范围验证：防止误匹配（如 "13-32" 这类无效日期）
        if 1 <= month_val <= 12 and 1 <= day_val <= 31:
            year = str(now.year)
            month = str(month_val).zfill(2)
            day = str(day_val).zfill(2)
            return year, f"{year}-{month}-{day}"
        
    return "Unknown_Year", "Unknown_Date"
