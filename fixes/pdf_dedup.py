"""PDF 文件多维查重、智能去重与数据库引用自动纠偏工具 (fixes/pdf_dedup.py)

本模块提供针对本地海量 PDF 物理文件与 SQLite 数据库的超高效查重与安全去重机制：

1. 查重维度：
   - hash : 基于文件内容完全一致的 MD5 多阶段快速查重（17万+文件秒级完成）
   - name : 基于标题与文件名变体（如 _1.pdf, _2.pdf 及 Unknown_Date/正式日期）查重
   - db   : 基于 SQLite 数据库内 pdf_path 共享与物理文件关联一致性查重
   - all  : 全量多维综合查重并汇总

2. 智能保留策略：
   - primary (默认推荐): 优先保留标准规范文件名（无 _1 后缀、具体年份与具体日期前缀）、更大体积及最新生成者
   - larger             : 优先保留体积最大者（避免保留残缺文件）
   - newest             : 优先保留修改时间最新者
   - oldest             : 优先保留最早生成者

3. 安全执行与数据库纠偏：
   - 默认 Dry-Run 预览模式，清晰展示待保留与待清理文件、预估释放空间
   - 正式执行 (--run) 前自动创建 SQLite 数据库 .bak 备份
   - 物理清理多余副本时，自动将数据库中指向被删除副本的 pdf_path 纠偏重定向至保留的主文件路径，彻底杜绝断链
   - 支持将多余文件移入隔离回收区 (--trash) 或直接删除
   - 去重完成后自动执行 VACUUM 回收数据库磁盘空间，支持导出 CSV 审计表与 Markdown 报告

用法与命令示例:
  python fixes/pdf_dedup.py                                         # 交互式主菜单
  python fixes/pdf_dedup.py --mode hash                             # 预览基于 MD5 的内容重复文件
  python fixes/pdf_dedup.py --mode hash --run                       # 正式执行 MD5 去重并同步更新 DB
  python fixes/pdf_dedup.py --mode name                             # 预览 _1.pdf 等文件名变体重复
  python fixes/pdf_dedup.py --mode db                               # 检查数据库 pdf_path 共享与无效引用
  python fixes/pdf_dedup.py --mode all --export-csv                 # 全量综合查重并导出 CSV
  python fixes/pdf_dedup.py --mode all --run --keep larger          # 全量去重并优先保留体积最大版本
"""

import os
import re
import sys
import time
import shutil
import hashlib
import sqlite3
import argparse
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Tuple, Optional, Set, Any

# ========== 路径引导与环境初始化 ==========
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fixes.db_utils import (  # noqa: E402
    setup_fixes_module,
    get_connection,
    get_db_path,
    format_size,
    backup_db,
    vacuum_db,
    get_export_dir,
    get_timestamp,
    export_records_to_db,
    export_to_csv,
    print_banner,
    print_step,
    print_warning,
    confirm_action,
    pause_for_user,
)

setup_fixes_module()

from config import PDF_BASE_DIR  # noqa: E402
from utils.logger import get_logger  # noqa: E402
from utils.pdf_utils import parse_filename, to_relative_path  # noqa: E402

logger = get_logger(__name__)

CSV_OUTPUT_DIR = os.environ.get("CSV_OUTPUT_DIR") or (
    "D:\\" if os.path.exists("D:\\") else os.path.join(PROJECT_ROOT, "cache")
)
os.makedirs(CSV_OUTPUT_DIR, exist_ok=True)


class PDFFileInfo:
    """PDF 物理文件元数据载体"""
    __slots__ = (
        "path", "basename", "size", "mtime", "rel_path",
        "date_prefix", "title_part", "is_unknown_year",
        "is_unknown_date", "has_num_suffix", "md5", "quick_hash"
    )

    def __init__(self, full_path: str, base_dir: str):
        self.path = os.path.abspath(full_path)
        self.basename = os.path.basename(full_path)
        stat = os.stat(full_path)
        self.size = stat.st_size
        self.mtime = stat.st_mtime
        
        # 安全计算相对路径（兼容 Windows 跨盘符）
        try:
            raw_rel = os.path.relpath(full_path, PROJECT_ROOT)
        except (ValueError, Exception):
            try:
                raw_rel = os.path.relpath(full_path, base_dir)
            except (ValueError, Exception):
                raw_rel = os.path.basename(full_path)
        self.rel_path = to_relative_path(raw_rel)
        
        # 解析文件名
        fn_date, fn_title = parse_filename(self.basename)
        self.date_prefix = fn_date
        self.title_part = fn_title
        self.is_unknown_year = "Unknown_Year" in full_path or "Unknown_Year" in self.rel_path
        self.is_unknown_date = (fn_date == "Unknown_Date" or fn_date is None)
        self.has_num_suffix = bool(re.search(r"_\d+$", self.basename[:-4] if self.basename.lower().endswith(".pdf") else self.basename))
        
        self.md5: Optional[str] = None
        self.quick_hash: Optional[str] = None

    def __repr__(self):
        return f"<PDFFile {self.rel_path} size={self.size}>"


def scan_all_physical_pdfs(base_dir: Optional[str] = None) -> List[PDFFileInfo]:
    """快速扫描指定目录下的全部物理 PDF 文件"""
    scan_dir = base_dir or PDF_BASE_DIR
    if not os.path.exists(scan_dir):
        logger.warning("PDF 目录不存在: %s", scan_dir)
        return []

    print(f"[*] 正在全量扫描 PDF 目录: {scan_dir} ...")
    t0 = time.time()
    pdf_files: List[PDFFileInfo] = []

    for root, _, files in os.walk(scan_dir):
        for f in files:
            if f.lower().endswith(".pdf"):
                full = os.path.join(root, f)
                try:
                    pdf_files.append(PDFFileInfo(full, scan_dir))
                except Exception as e:
                    logger.debug("读取文件元数据失败 %s: %s", full, e)

    t1 = time.time()
    print(f"[+] 物理文件扫描完成，共耗时 {t1 - t0:.2f}s，发现 {len(pdf_files)} 个 PDF 文件。")
    return pdf_files


# ===================================================================
# 1. 哈希查重 (Hash-based Content Deduplication)
# ===================================================================

def _compute_quick_hash(file_info: PDFFileInfo) -> Tuple[PDFFileInfo, Optional[str]]:
    """快速计算首尾 4KB 局部特征哈希"""
    try:
        sz = file_info.size
        with open(file_info.path, "rb") as f:
            head = f.read(4096)
            if sz > 8192:
                f.seek(-4096, os.SEEK_END)
                tail = f.read(4096)
            else:
                tail = b""
            qh = hashlib.md5(head + tail).hexdigest()
            file_info.quick_hash = qh
            return file_info, qh
    except Exception as e:
        logger.debug("计算局部哈希失败 %s: %s", file_info.path, e)
        return file_info, None


def _compute_full_md5(file_info: PDFFileInfo) -> Tuple[PDFFileInfo, Optional[str]]:
    """流式计算完整文件 MD5 哈希"""
    try:
        h = hashlib.md5()
        with open(file_info.path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        digest = h.hexdigest()
        file_info.md5 = digest
        return file_info, digest
    except Exception as e:
        logger.debug("计算完整 MD5 失败 %s: %s", file_info.path, e)
        return file_info, None


def find_hash_duplicates(pdf_files: List[PDFFileInfo], max_workers: int = 16) -> Dict[str, List[PDFFileInfo]]:
    """三阶段渐进式哈希查重：大小初筛 -> 首尾抽样哈希 -> 全量 MD5 校验
    
    Returns:
        Dict[md5, List[PDFFileInfo]]: key 为 MD5，value 为包含 2 个及以上文件的重复组列表
    """
    print("\n" + "=" * 60)
    print("           [模式 1: 文件内容哈希查重 (MD5 Pipeline)]")
    print("=" * 60)
    t0 = time.time()

    # 阶段一：按字节大小秒级分组
    size_groups = defaultdict(list)
    for fi in pdf_files:
        size_groups[fi.size].append(fi)

    stage1_candidates: List[PDFFileInfo] = []
    for sz, flist in size_groups.items():
        if len(flist) > 1:
            stage1_candidates.extend(flist)

    t1 = time.time()
    print(f"[*] 阶段 1 (文件大小初筛): 从 {len(pdf_files)} 个文件中筛选出 {len(stage1_candidates)} 个潜在重复候选 (耗时 {t1 - t0:.2f}s)")

    if not stage1_candidates:
        return {}

    # 阶段二：首尾局部特征哈希抽样
    quick_groups = defaultdict(list)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for fi, qh in executor.map(_compute_quick_hash, stage1_candidates):
            if qh:
                quick_groups[(fi.size, qh)].append(fi)

    stage2_candidates: List[PDFFileInfo] = []
    for k, flist in quick_groups.items():
        if len(flist) > 1:
            stage2_candidates.extend(flist)

    t2 = time.time()
    print(f"[*] 阶段 2 (首尾特征哈希): 收敛至 {len(stage2_candidates)} 个高度疑似候选 (耗时 {t2 - t1:.2f}s)")

    if not stage2_candidates:
        return {}

    # 阶段三：全量 MD5 精确校验
    full_groups = defaultdict(list)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for fi, digest in executor.map(_compute_full_md5, stage2_candidates):
            if digest:
                full_groups[digest].append(fi)

    # 过滤出重复组
    exact_duplicates = {digest: flist for digest, flist in full_groups.items() if len(flist) > 1}
    t3 = time.time()

    total_dup_files = sum(len(flist) for flist in exact_duplicates.values())
    wasted_bytes = sum(sum(fi.size for fi in flist[1:]) for flist in exact_duplicates.values())

    print(f"[+] 阶段 3 (全量 MD5 校验完成): 共发现 {len(exact_duplicates)} 组完全重复文件，涉及 {total_dup_files} 个物理文件，冗余空间: {format_size(wasted_bytes)} (耗时 {t3 - t2:.2f}s)")
    print(f"[+] 哈希查重总耗时: {t3 - t0:.2f}s")
    return exact_duplicates


# ===================================================================
# 2. 标题/文件名变体查重 (Name / Title Variant Deduplication)
# ===================================================================

def normalize_title_variant_key(basename: str) -> str:
    """提取文件名的归一化基础名称（剥离 .pdf 和 _1, _2 等自增后缀）"""
    name = basename[:-4] if basename.lower().endswith(".pdf") else basename
    # 剥离末尾的 _1, _2 等数字后缀
    name = re.sub(r"_\d+$", "", name)
    return name.strip().lower()


def find_name_variant_duplicates(pdf_files: List[PDFFileInfo]) -> Dict[str, List[PDFFileInfo]]:
    """基于文件名/标题变体查重（识别爬虫重试生成的 _1.pdf, _2.pdf 等同名多版本）
    
    Returns:
        Dict[norm_key, List[PDFFileInfo]]: key 为归一化基础名，value 为包含 2 个及以上变体文件的列表
    """
    print("\n" + "=" * 60)
    print("         [模式 2: 文件名/标题变体查重 (Suffix & Variant)]")
    print("=" * 60)
    t0 = time.time()

    variant_groups = defaultdict(list)
    for fi in pdf_files:
        norm_key = normalize_title_variant_key(fi.basename)
        if norm_key:
            variant_groups[norm_key].append(fi)

    duplicate_variants = {k: v for k, v in variant_groups.items() if len(v) > 1}
    t1 = time.time()

    total_variant_files = sum(len(v) for v in duplicate_variants.values())
    print(f"[+] 文件名变体扫描完成，发现 {len(duplicate_variants)} 组同名/标题变体，涉及 {total_variant_files} 个物理文件 (耗时 {t1 - t0:.2f}s)")
    return duplicate_variants


# ===================================================================
# 3. 数据库引用查重与一致性检测 (Database PDF Reference Deduplication)
# ===================================================================

def find_db_pdf_duplicates(conn: sqlite3.Connection, pdf_files: Optional[List[PDFFileInfo]] = None) -> Dict[str, Any]:
    """检测数据库中关于 pdf_path 的多重共享引用、无效死链及一致性状态
    
    Returns:
        Dict 包含 shared_paths (多记录共享同一 pdf_path), missing_phys_paths (有库无物理文件), etc.
    """
    print("\n" + "=" * 60)
    print("        [模式 3: 数据库 PDF 引用与一致性检测 (Database DB)]")
    print("=" * 60)
    cursor = conn.cursor()

    # 1. 统计数据库中 pdf_path 总体情况
    cursor.execute("SELECT COUNT(*) FROM resources")
    total_records = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(pdf_path), COUNT(DISTINCT pdf_path) FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
    row = cursor.fetchone()
    total_with_pdf, distinct_pdfs = row[0], row[1]

    print(f"[*] 数据库总记录数: {total_records}")
    print(f"[*] 包含 pdf_path 记录数: {total_with_pdf}")
    print(f"[*] 独立不重复 pdf_path 数: {distinct_pdfs}")
    print(f"[*] 存在共享引用的超额记录数: {total_with_pdf - distinct_pdfs}")

    # 2. 查询共享 pdf_path 的分组
    cursor.execute("""
        SELECT pdf_path, COUNT(*) as cnt, GROUP_CONCAT(id) as id_list
        FROM resources
        WHERE pdf_path IS NOT NULL AND pdf_path != ''
        GROUP BY pdf_path
        HAVING cnt > 1
    """)
    shared_rows = cursor.fetchall()
    shared_pdf_map = {}
    for path, cnt, ids in shared_rows:
        shared_pdf_map[path] = {
            "count": cnt,
            "ids": [int(x) for x in ids.split(",")] if ids else []
        }

    print(f"[+] 发现 {len(shared_pdf_map)} 个被多条数据库记录共享引用的 pdf_path。")

    # 3. 物理存在性核验 (若传入了 pdf_files)
    missing_phys_records = []
    if pdf_files:
        existing_rel_paths = {fi.rel_path.lower(): fi for fi in pdf_files}
        cursor.execute("SELECT id, title, pdf_path FROM resources WHERE pdf_path IS NOT NULL AND pdf_path != ''")
        for r_id, r_title, r_pdf_path in cursor.fetchall():
            norm_rel = to_relative_path(r_pdf_path).lower()
            if norm_rel not in existing_rel_paths:
                missing_phys_records.append((r_id, r_title, r_pdf_path))

        print(f"[*] 物理文件丢失的数据库死链记录数: {len(missing_phys_records)}")

    return {
        "total_records": total_records,
        "total_with_pdf": total_with_pdf,
        "distinct_pdfs": distinct_pdfs,
        "shared_pdf_map": shared_pdf_map,
        "missing_phys_records": missing_phys_records,
    }


# ===================================================================
# 4. 智能主文件判定与保留策略 (Keep Policy)
# ===================================================================

def select_primary_file(files: List[PDFFileInfo], policy: str = "primary") -> Tuple[PDFFileInfo, List[PDFFileInfo]]:
    """在重复文件组中根据指定策略选出唯一的【主保留文件 (Primary File)】，其余作为待清理副本
    
    策略说明:
      - primary (默认): 综合评分法
          +100 分: 不在 Unknown_Year 目录
          +50  分: 文件名含有效日期 (YYYY-MM-DD) 而非 Unknown_Date
          +30  分: 文件名无 _1, _2 等后缀
          主排序: 规范性得分降序 > 文件大小降序 > 修改时间降序
      - larger: 文件大小降序 > 规范性得分降序 > 修改时间降序
      - newest: 修改时间降序 > 规范性得分降序 > 文件大小降序
      - oldest: 修改时间升序 > 规范性得分降序 > 文件大小降序
    """
    if not files:
        raise ValueError("文件列表不可为空")
    if len(files) == 1:
        return files[0], []

    def compute_canonical_score(fi: PDFFileInfo) -> int:
        score = 0
        if not fi.is_unknown_year:
            score += 100
        if not fi.is_unknown_date:
            score += 50
        if not fi.has_num_suffix:
            score += 30
        return score

    if policy == "larger":
        # 优先体积大，体积相同时优先规范名，然后最新
        sorted_files = sorted(files, key=lambda fi: (fi.size, compute_canonical_score(fi), fi.mtime), reverse=True)
    elif policy == "newest":
        # 优先最新，然后规范名，然后体积大
        sorted_files = sorted(files, key=lambda fi: (fi.mtime, compute_canonical_score(fi), fi.size), reverse=True)
    elif policy == "oldest":
        # 优先最早，然后规范名，然后体积大
        sorted_files = sorted(files, key=lambda fi: (-fi.mtime, compute_canonical_score(fi), fi.size), reverse=True)
    else:  # primary
        # 优先规范名，然后体积大，然后最新
        sorted_files = sorted(files, key=lambda fi: (compute_canonical_score(fi), fi.size, fi.mtime), reverse=True)

    primary = sorted_files[0]
    duplicates = sorted_files[1:]
    return primary, duplicates


# ===================================================================
# 5. 去重计划生成与数据库引用纠偏执行 (Deduplication Execution)
# ===================================================================

class DedupActionPlan:
    """去重动作执行计划"""
    def __init__(self, group_id: str, group_type: str, primary: PDFFileInfo, duplicates: List[PDFFileInfo]):
        self.group_id = group_id
        self.group_type = group_type
        self.primary = primary
        self.duplicates = duplicates
        self.reclaim_bytes = sum(fi.size for fi in duplicates)
        self.redirect_map: Dict[str, str] = {}  # deleted_rel_path -> primary_rel_path
        for fi in duplicates:
            if fi.rel_path != primary.rel_path:
                self.redirect_map[fi.rel_path] = primary.rel_path


def build_dedup_plan(
    duplicate_groups: Dict[str, List[PDFFileInfo]],
    group_type: str = "hash",
    policy: str = "primary"
) -> List[DedupActionPlan]:
    """构建去重动作计划清单"""
    plans: List[DedupActionPlan] = []
    for gid, flist in duplicate_groups.items():
        if len(flist) < 2:
            continue
        primary, duplicates = select_primary_file(flist, policy=policy)
        plan = DedupActionPlan(gid, group_type, primary, duplicates)
        plans.append(plan)
    return plans


def execute_dedup_plans(
    plans: List[DedupActionPlan],
    db_path: str,
    dry_run: bool = True,
    trash_dir: Optional[str] = None,
    sync_db: bool = True,
) -> Dict[str, Any]:
    """执行物理文件清理并自动重定向纠偏 SQLite 数据库中的 pdf_path 引用
    
    Args:
        plans: 去重计划列表
        db_path: 数据库文件路径
        dry_run: 是否为预览模式 (True 时不执行实际文件删除与数据库写入)
        trash_dir: 可选的回收站目录路径（若提供则将文件移动到该目录，否则直接删除）
        sync_db: 是否同步纠偏数据库引用
        
    Returns:
        执行统计字典
    """
    total_groups = len(plans)
    total_files_to_remove = sum(len(p.duplicates) for p in plans)
    total_bytes_to_reclaim = sum(p.reclaim_bytes for p in plans)

    # 汇总重定向映射: old_rel_path -> new_rel_path
    global_redirect_map: Dict[str, str] = {}
    for p in plans:
        for old_p, new_p in p.redirect_map.items():
            global_redirect_map[old_p] = new_p

    print("\n" + "=" * 60)
    print(f"[*] 执行模式: {'【正式执行 (RUN)】' if not dry_run else '【预览模式 (DRY RUN)】'}")
    print(f"[*] 待处理去重组数: {total_groups}")
    print(f"[*] 待清理冗余文件数: {total_files_to_remove}")
    print(f"[*] 预估可释放磁盘空间: {format_size(total_bytes_to_reclaim)}")
    print(f"[*] 涉及数据库引用重定向路径数: {len(global_redirect_map)}")
    print("=" * 60)

    stats = {
        "total_groups": total_groups,
        "files_removed": 0,
        "bytes_reclaimed": 0,
        "db_records_redirected": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    if dry_run:
        print("[*] 当前为 Dry-Run 预览模式，未对物理文件和数据库进行任何修改。")
        print("[*] 若确认执行去重，请在命令后加上 --run 参数。")
        return stats

    # 1. 正式写入前备份数据库
    if sync_db and os.path.exists(db_path):
        backup_db(db_path, prefix_tag="pdf_dedup")

    # 2. 物理文件删除或隔离
    if trash_dir:
        os.makedirs(trash_dir, exist_ok=True)
        print(f"[*] 启用了隔离回收站，多余文件将被移动至: {trash_dir}")

    deleted_count = 0
    reclaimed_bytes = 0
    err_count = 0

    print("[*] 正在执行物理文件清理...")
    for p in plans:
        for dup in p.duplicates:
            try:
                if not os.path.exists(dup.path):
                    continue
                if trash_dir:
                    # 保留原相对路径结构移入 trash_dir
                    target_trash = os.path.join(trash_dir, os.path.basename(dup.path))
                    if os.path.exists(target_trash):
                        base_n, ext_n = os.path.splitext(os.path.basename(dup.path))
                        target_trash = os.path.join(trash_dir, f"{base_n}_{int(time.time()*1000)}{ext_n}")
                    shutil.move(dup.path, target_trash)
                else:
                    os.remove(dup.path)
                deleted_count += 1
                reclaimed_bytes += dup.size
            except Exception as e:
                logger.error("清理文件失败 %s: %s", dup.path, e)
                err_count += 1

    stats["files_removed"] = deleted_count
    stats["bytes_reclaimed"] = reclaimed_bytes
    stats["errors"] = err_count
    print(f"[+] 物理文件清理完成: 成功处理 {deleted_count} 个文件，释放空间 {format_size(reclaimed_bytes)} (失败 {err_count} 个)。")

    # 3. 数据库引用自动重定向纠偏
    if sync_db and os.path.exists(db_path) and global_redirect_map:
        print("[*] 正在同步纠偏数据库中的 pdf_path 引用...")
        conn = get_connection(db_path)
        cursor = conn.cursor()

        updated_records = 0
        # 批量更新数据库引用
        for old_rel, new_rel in global_redirect_map.items():
            # 考虑可能保存的反斜杠/斜杠格式
            old_rel_slash = old_rel.replace("\\", "/")
            old_rel_bslash = old_rel.replace("/", "\\")
            
            cursor.execute(
                "UPDATE resources SET pdf_path = ? WHERE pdf_path = ? OR pdf_path = ?",
                (new_rel, old_rel_slash, old_rel_bslash)
            )
            updated_records += cursor.rowcount

        conn.commit()
        stats["db_records_redirected"] = updated_records
        print(f"[+] 数据库引用纠偏完成: 成功重定向了 {updated_records} 条记录的 pdf_path！")

        # 压缩数据库
        vacuum_db(conn)
        conn.close()

    print("\n" + "=" * 60)
    print("                     去重执行结果汇总")
    print("=" * 60)
    print(f" 处理去重组数:                      {total_groups}")
    print(f" 清理多余物理文件数:                {deleted_count}")
    print(f" 释放磁盘物理空间:                  {format_size(reclaimed_bytes)}")
    print(f" 数据库重定向纠偏记录数:            {stats['db_records_redirected']}")
    print(f" 异常错误数:                        {err_count}")
    print("=" * 60)
    return stats


# ===================================================================
# 6. DB 导出、CSV 导出与 Markdown 审计报告生成 (统一优先 D 盘 / temp_profiles)
# ===================================================================

def export_dedup_db(plans: List[DedupActionPlan], output_dir: Optional[str] = None) -> str:
    """将查重审计明细导出为独立 SQLite .db 数据库文件 (默认导出格式)"""
    if not plans:
        return ""
    records: List[Dict[str, Any]] = []
    for plan in plans:
        p = plan.primary
        records.append({
            "group_id": plan.group_id,
            "group_type": plan.group_type,
            "status": "KEEP",
            "rel_path": p.rel_path,
            "basename": p.basename,
            "size": p.size,
            "size_str": format_size(p.size),
            "mtime": datetime.fromtimestamp(p.mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "md5": p.md5 or "",
            "path": p.path,
        })
        for dup in plan.duplicates:
            records.append({
                "group_id": plan.group_id,
                "group_type": plan.group_type,
                "status": "DUPLICATE",
                "rel_path": dup.rel_path,
                "basename": dup.basename,
                "size": dup.size,
                "size_str": format_size(dup.size),
                "mtime": datetime.fromtimestamp(dup.mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "md5": dup.md5 or "",
                "path": dup.path,
            })

    ts = get_timestamp()
    filename = f"pdf_dedup_audit_{ts}.db"
    return export_records_to_db(records, filename, table_name="dedup_audit", output_dir=output_dir, unique_col=None)


def export_dedup_csv(plans: List[DedupActionPlan], output_dir: Optional[str] = None) -> str:
    """导出全部查重明细至带 UTF-8 BOM 编码的 CSV 文件"""
    if not plans:
        return ""
    records: List[Dict[str, Any]] = []
    for plan in plans:
        p = plan.primary
        records.append({
            "group_id": plan.group_id,
            "group_type": plan.group_type,
            "status": "KEEP",
            "rel_path": p.rel_path,
            "basename": p.basename,
            "size": p.size,
            "size_str": format_size(p.size),
            "mtime": datetime.fromtimestamp(p.mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "md5": p.md5 or "",
            "path": p.path,
        })
        for dup in plan.duplicates:
            records.append({
                "group_id": plan.group_id,
                "group_type": plan.group_type,
                "status": "DUPLICATE",
                "rel_path": dup.rel_path,
                "basename": dup.basename,
                "size": dup.size,
                "size_str": format_size(dup.size),
                "mtime": datetime.fromtimestamp(dup.mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "md5": dup.md5 or "",
                "path": dup.path,
            })

    header_map = {
        "group_id": "分组标识 (GroupID)",
        "group_type": "查重类型 (Type)",
        "status": "状态 (Status)",
        "rel_path": "文件相对路径 (RelativePath)",
        "basename": "文件名 (Filename)",
        "size": "大小 (Bytes)",
        "size_str": "易读大小 (SizeStr)",
        "mtime": "修改时间 (MTime)",
        "md5": "MD5哈希 (MD5)",
        "path": "物理绝对路径 (AbsolutePath)",
    }
    ts = get_timestamp()
    filename = f"pdf_dedup_audit_{ts}.csv"
    return export_to_csv(records, filename, header_map=header_map, output_dir=output_dir)


def generate_dedup_markdown_report(plans: List[DedupActionPlan], stats: Dict[str, Any], output_path: Optional[str] = None) -> str:
    """生成 Markdown 格式的 PDF 查重与去重审计报告 (统一保存至导出目录)"""
    ts = get_timestamp()
    out_dir = get_export_dir()
    rep_path = output_path or os.path.join(out_dir, f"pdf_dedup_report_{ts}.md")

    total_groups = len(plans)
    total_dups = sum(len(p.duplicates) for p in plans)
    total_reclaim = sum(p.reclaim_bytes for p in plans)

    lines = [
        "# PDF 查重与去重审计分析报告\n",
        f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **运行模式**: {'正式执行 (RUN)' if not stats.get('dry_run', True) else '预览模式 (DRY RUN)'}",
        f"- **重复文件组数**: {total_groups}",
        f"- **多余副本文件数**: {total_dups}",
        f"- **冗余释放空间**: {format_size(total_reclaim)}",
        f"- **数据库纠偏重定向记录数**: {stats.get('db_records_redirected', 0)}",
        "\n" + "=" * 50 + "\n",
        "## 重复文件组明细样例 (Top 50)\n",
        "| 序号 | 分组类型 | 保留的主文件 (KEEP) | 大小 | 多余副本列表 (DUPLICATES) | 冗余大小 |",
        "| --- | --- | --- | --- | --- | --- |"
    ]

    for i, plan in enumerate(plans[:50], 1):
        dup_names = "<br>".join(f"`{d.basename}` ({format_size(d.size)})" for d in plan.duplicates)
        lines.append(
            f"| {i} | {plan.group_type} | `{plan.primary.rel_path}` | {format_size(plan.primary.size)} | {dup_names} | {format_size(plan.reclaim_bytes)} |"
        )

    if len(plans) > 50:
        lines.append("\n*(仅展示前 50 组样例，完整明细请查看导出的 DB / CSV 文件)*\n")

    with open(rep_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[+] Markdown 审计报告已生成至: {rep_path}")
    return rep_path


# ===================================================================
# 7. 统一运行入口函数 (Runner API)
# ===================================================================

def run_pdf_dedup(
    mode: str = "all",
    keep: str = "primary",
    run: bool = False,
    export_db: bool = True,
    export_csv: bool = False,
    trash: bool = False,
    db_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    max_workers: int = 16,
) -> Dict[str, Any]:
    """PDF 查重与去重核心调度接口

    Args:
        mode: 查重模式 ('hash', 'name', 'db', 'all')
        keep: 保留策略 ('primary', 'larger', 'newest', 'oldest')
        run: 是否正式执行去重 (False=预览模式, True=正式执行)
        export_db: 是否导出独立 SQLite .db 审计库 (默认 True)
        export_csv: 是否导出 CSV 明细表
        trash: 是否将多余文件移动到 trash 隔离区而非直接删除
        db_path: SQLite 数据库路径
        base_dir: PDF 物理文件根目录
        output_dir: 导出文件输出目录
        max_workers: 并发工作线程数
    """
    effective_db = get_db_path(db_path)
    effective_pdf_dir = base_dir or PDF_BASE_DIR
    trash_dir = os.path.join(PROJECT_ROOT, "cache", "pdf_trash") if trash else None

    print_banner("PDF 查重与去重管理系统")
    print(f"[*] 运行模式: {'【正式执行 (RUN)】' if run else '【预览模式 (Dry Run)】'}")
    print(f"[*] 查重维度: {mode}")
    print(f"[*] 保留策略: {keep}")
    print(f"[*] 数据库路径: {effective_db}")
    print(f"[*] PDF 根目录: {effective_pdf_dir}")
    print(f"[*] 导出目标路径: {get_export_dir(output_dir)}")
    print("=" * 60)

    # 1. 扫描物理文件
    pdf_files = scan_all_physical_pdfs(effective_pdf_dir)
    if not pdf_files:
        print_warning("未找到任何物理 PDF 文件。")
        return {}

    all_plans: List[DedupActionPlan] = []

    # 2. 按模式执行检测
    if mode in ("hash", "all"):
        hash_dups = find_hash_duplicates(pdf_files, max_workers=max_workers)
        plans = build_dedup_plan(hash_dups, group_type="hash", policy=keep)
        all_plans.extend(plans)

    if mode in ("name", "title", "all"):
        name_dups = find_name_variant_duplicates(pdf_files)
        plans = build_dedup_plan(name_dups, group_type="name_variant", policy=keep)
        all_plans.extend(plans)

    if mode in ("db", "all"):
        if os.path.exists(effective_db):
            conn = get_connection(effective_db)
            find_db_pdf_duplicates(conn, pdf_files=pdf_files)
            conn.close()
        else:
            print_warning(f"数据库文件不存在: {effective_db}")

    # 合并与去重 Plan（防止跨模式重复处理同一文件）
    unique_plans: List[DedupActionPlan] = []
    seen_duplicate_paths: Set[str] = set()

    for p in all_plans:
        filtered_dups = [d for d in p.duplicates if d.path not in seen_duplicate_paths]
        if filtered_dups:
            for d in filtered_dups:
                seen_duplicate_paths.add(d.path)
            p.duplicates = filtered_dups
            p.reclaim_bytes = sum(d.size for d in filtered_dups)
            unique_plans.append(p)

    # 3. 默认导出独立 .db 数据库审计文件
    if export_db and unique_plans:
        export_dedup_db(unique_plans, output_dir=output_dir)

    # 4. 可选导出 CSV 审计表
    if export_csv and unique_plans:
        export_dedup_csv(unique_plans, output_dir=output_dir)

    # 5. 执行去重与重定向纠偏
    stats = execute_dedup_plans(
        unique_plans,
        db_path=effective_db,
        dry_run=not run,
        trash_dir=trash_dir,
        sync_db=True,
    )

    # 6. 生成 Markdown 报告
    if unique_plans:
        generate_dedup_markdown_report(unique_plans, stats)

    return stats


# ===================================================================
# 8. 命令行 CLI 与交互式菜单主入口 (常驻循环)
# ===================================================================

def interactive_menu():
    """PDF 查重与去重管理系统主菜单 (常驻循环)"""
    while True:
        print_banner("PDF 查重与去重管理系统 (交互模式)")
        print("  请选择要执行的操作：")
        print()
        print("    1. [预览] 全量综合查重 (MD5哈希 + 文件名变体 + 数据库引用，默认导出 .db)")
        print("    2. [预览] 仅基于内容 MD5 哈希查重")
        print("    3. [预览] 仅基于文件名/标题变体 (_1.pdf) 查重")
        print("    4. [预览] 仅检查数据库 pdf_path 共享与一致性")
        print("    5. [执行] 全量安全去重 (备份DB + 删除多余副本 + 自动重定向DB引用)")
        print("    6. [执行] 全量隔离去重 (移入 cache/pdf_trash 隔离区 + 重定向DB)")
        print()
        print("    0. 退出程序")
        print("=" * 60)

        try:
            choice = input("  请输入序号 [0-6] (直接回车默认 1): ").strip()
            if not choice:
                choice = "1"
        except (KeyboardInterrupt, EOFError):
            print("\n[-] 运行已取消")
            break

        if choice in ("0", "q", "quit", "exit"):
            print_step("已退出程序。")
            break

        if choice == "1":
            run_pdf_dedup(mode="all", run=False, export_db=True, export_csv=True)
        elif choice == "2":
            run_pdf_dedup(mode="hash", run=False, export_db=True, export_csv=True)
        elif choice == "3":
            run_pdf_dedup(mode="name", run=False, export_db=True, export_csv=True)
        elif choice == "4":
            run_pdf_dedup(mode="db", run=False, export_db=False, export_csv=False)
        elif choice == "5":
            if confirm_action("\n[!] 警告: 将正式删除多余的重复 PDF 文件并同步纠偏数据库！确认继续?", default=False):
                run_pdf_dedup(mode="all", run=True, export_db=True, export_csv=True)
            else:
                print_step("操作已取消。")
        elif choice == "6":
            run_pdf_dedup(mode="all", run=True, trash=True, export_db=True, export_csv=True)
        else:
            print_warning("无效的序号。")

        pause_for_user()


def main():
    setup_fixes_module()
    parser = argparse.ArgumentParser(
        description="PDF 文件多维查重、去重与数据库引用纠偏工具"
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["all", "hash", "name", "title", "db"],
        default="all",
        help="查重维度: hash (内容MD5), name/title (文件名与标题变体), db (数据库关联), all (全量综合，默认)"
    )
    parser.add_argument(
        "--keep", "-k",
        choices=["primary", "larger", "newest", "oldest"],
        default="primary",
        help="保留策略: primary (规范文件名优先，默认), larger (最大体积), newest (最新生成), oldest (最早生成)"
    )
    parser.add_argument(
        "--run", action="store_true", default=False,
        help="正式执行物理文件清理与数据库重定向，不加此参数时仅进行安全预览 (Dry Run)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="显式声明预览模式"
    )
    parser.add_argument(
        "--export-db", action="store_true", default=True,
        help="导出查重明细至独立 SQLite .db 数据库 (默认开启)"
    )
    parser.add_argument(
        "--export-csv", action="store_true", default=False,
        help="导出查重明细至 CSV 审计表"
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="指定导出文件目录 (默认优先 D:\\ 否则 temp_profiles/)"
    )
    parser.add_argument(
        "--trash", action="store_true", default=False,
        help="将多余重复文件移动至 cache/pdf_trash 隔离区而非直接物理删除"
    )
    parser.add_argument(
        "--db", default=None,
        help="指定自定义 SQLite 数据库路径"
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=16,
        help="多线程哈希计算线程数 (默认 16)"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", default=False,
        help="跳过确认提示直接执行"
    )

    args = parser.parse_args()

    if len(sys.argv) == 1:
        interactive_menu()
    else:
        is_run = args.run or args.yes
        run_pdf_dedup(
            mode=args.mode,
            keep=args.keep,
            run=is_run,
            export_db=args.export_db,
            export_csv=args.export_csv,
            trash=args.trash,
            db_path=args.db,
            output_dir=args.output_dir,
            max_workers=args.workers,
        )


if __name__ == "__main__":
    main()

