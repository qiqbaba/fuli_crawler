import os
import re

def parse_filename(filename):
    """
    解析 PDF 文件名，提取日期前缀和标题。
    支持格式:
      1. YYYY-MM-DD_Title.pdf
      2. Unknown_Date_Title.pdf
      3. Other_Title.pdf (无日期前缀)
    """
    # 移除 .pdf 后缀
    if filename.lower().endswith('.pdf'):
        name_part = filename[:-4]
    else:
        name_part = filename

    # 匹配 YYYY-MM-DD
    date_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.*)$")
    match = date_pattern.match(name_part)
    if match:
        return match.group(1), match.group(2)

    # 匹配 Unknown_Date
    if name_part.startswith("Unknown_Date_"):
        return "Unknown_Date", name_part[len("Unknown_Date_"):]

    return None, name_part

def clean_title_suffix(title_part):
    """
    剥离标题中可能含有的数字后缀，如 _1, _2 等
    """
    match = re.search(r"_(?P<num>\d+)$", title_part)
    if match:
        suffix = match.group(0)
        return title_part[:-len(suffix)]
    return title_part

def to_relative_path(pdf_path):
    """将绝对路径转换为相对路径格式：pdf/year/filename.pdf"""
    if not pdf_path:
        return ""
    p = pdf_path.replace('\\', '/')
    parts = [x for x in p.split('/') if x]
    if len(parts) >= 2:
        return f"pdf/{parts[-2]}/{parts[-1]}"
    return p

def generate_unique_path(target_dir, base_name):
    """
    在目标目录下生成一个唯一的文件路径（避免重名覆盖）
    """
    name, ext = os.path.splitext(base_name)
    target_path = os.path.join(target_dir, base_name)
    counter = 1
    while os.path.exists(target_path):
        new_name = f"{name}_{counter}{ext}"
        target_path = os.path.join(target_dir, new_name)
        counter += 1
    return target_path
