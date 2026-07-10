"""
番号过滤脚本

从数据库中扫描所有标题，匹配番号模式后，提供两种操作：
  1. 导出匹配记录到新数据库（不含非番号记录）
  2. 删除匹配记录（同时删除对应 PDF 文件）

用法:
  python fixes/filter_fanhao.py                        # 交互式模式（逐步询问）
  python fixes/filter_fanhao.py --interactive           # 显式进入交互式模式
  python fixes/filter_fanhao.py --mode export           # 导出发番号记录到新库
  python fixes/filter_fanhao.py --mode delete           # 删除番号记录 (含 PDF)
  python fixes/filter_fanhao.py --mode export --dry-run # 预览导出
  python fixes/filter_fanhao.py --mode delete --dry-run # 预览删除
"""

import os
import re
import sys
import shutil
import sqlite3
import argparse
from datetime import datetime

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_db_path, PDF_BASE_DIR

# Windows 控制台 UTF-8 输出
if sys.platform.startswith('win'):
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass


# ============================================================
# 1. 番号正则模式与过滤
# ============================================================

# 以下 5 条模式用于在标题文本中搜索番号（带边界断言，防止误截）
FANHAO_PATTERNS = [
    # 模式1: 标准字母-数字  ABC-123, FC2-1234567, HEYZO-1234, CARIB-12345, n-1234 (允许单字母前缀以兼容 C-2426, n-1234 等)
    r'(?<![a-zA-Z0-9])([A-Za-z]{1,7}-\d{2,8})(?![a-zA-Z0-9])',
    # 模式2: 混合前缀(含数字)+分隔符-数字  1PONDO-12345, 200GANA-1234, T28-123
    r'(?<![a-zA-Z0-9])((?:[A-Za-z]+\d+[A-Za-z]*|\d+[A-Za-z]+)[A-Za-z0-9]*-\d{2,8})(?![a-zA-Z0-9])',
    # 模式3: 字母直接+数字(无分隔符)  ABC123, ssni123 (限制前缀为 2-5 个字母，防止 Richcat520 或单字母如 A123 误判)
    r'(?<![a-zA-Z0-9])([A-Za-z]{2,5}\d{2,8})(?![a-zA-Z0-9])',
    # 模式4: 下划线分隔 (使用前瞻断言限制下划线前缀为 2-12 位，且必须包含至少一个字母，杜绝纯数字如 062026_100)
    r'(?<![a-zA-Z0-9])((?=[a-zA-Z0-9]{2,12}_)[A-Za-z0-9]*[A-Za-z]+[A-Za-z0-9]*_\d{2,8})(?![a-zA-Z0-9])',
    # 模式5: 纯数字开头+字母+数字  10musume12345 (限制中间字母为 3-6 位，过滤 158CM80, 4K60 等)
    r'(?<![a-zA-Z0-9])(\d{1,3}[A-Za-z]{3,6}\d{2,6})(?![a-zA-Z0-9])',
]

# 编译好的正则
FANHAO_REGEX = re.compile('|'.join(FANHAO_PATTERNS), re.IGNORECASE)

# 黑名单正则模式 —— 过滤非番号的各种音视频、网站、集数、漫展或版本标志
BLACKList_PATTERNS = [
    r'^X26[45]$',
    r'^H26[45]$',
    r'^HEVC$',
    r'^HDR$',
    r'^10BIT$',
    r'^R-?18$',
    r'^VR180$',
    r'^\d+K\d*$',       # 4K, 8K, 4K60
    r'^C\d{2,3}$',       # C100, C107 (Comiket)
    r'^C-\d{2,3}$',      # C-107
    r'^EP-?\d+$',        # EP01, EP-01
    r'^E-?\d+$',         # E01, E-01
    r'^S\d{1,2}$',       # S01 (Season)
    r'^VOL-?\d+$',       # VOL01, VOL-01
    r'^CH-?\d+$',        # CH01, CH-01
    r'^V-?\d+$',         # V1, V-1
    r'^P-?\d+$',         # P1, P-1
    r'^PART-?\d+$',      # PART1, PART-1
    r'^IMG[-_]?\d+$',    # IMG_123, IMG123
    r'^[A-Za-z]\d+$',   # 纯单字母加数字 (无分隔符)，如 A244, B001
    r'^MP[34]$',
    r'^AVI$',
    r'^MKV$',
    r'^WMV$',
    r'^FLV$',
    r'.*\.VIP$',
    r'.*\.CC$',
    r'.*\.COM$',
    r'.*\.NET$',
    r'.*\.ORG$',
    # 域名后接数字的误判
    r'^COM[-_]\d+$',
    r'^NET[-_]\d+$',
    r'^ORG[-_]\d+$',
    r'^VIP[-_]?\d+$',    # VIP1196, VIP_1196
    r'^VER[-_]?\d+.*$',  # VER25, Ver25.01.22
    r'^KU-?\d+$',        # KU100
    r'^FIT-?\d+$',       # FIT18
    r'^TRANS-?\d+$',     # TRANS500
]

BLACKLIST_REGEX = re.compile('|'.join(BLACKList_PATTERNS), re.IGNORECASE)


# ============================================================
# 2. 番号检测核心函数
# ============================================================

def extract_fanhao(title):
    """
    从标题中提取番号。
    返回: (found: bool, matched: str or None)
    """
    if not title:
        return False, None

    # --- 排除型过滤: 先看是不是元数据标题 ---

    # 2a. 如果标题中只有方括号元数据（如 [16V/537P]），无番号特征
    #     取出方括号内容检查
    bracket_contents = re.findall(r'\[([^\]]+)\]', title)
    if bracket_contents:
        all_meta = True
        for bc in bracket_contents:
            parts = bc.split('/')
            for part in parts:
                part = part.strip()
                # 是纯数字+单位(V/P/size) → 元数据
                if re.match(r'^\d+(?:\.\d+)?\s*(?:[GgMmTt][Bb]?|[VvPp])$', part):
                    continue
                # 纯数字
                if re.match(r'^\d+$', part):
                    continue
                all_meta = False
                break
            if not all_meta:
                break
        if all_meta and bracket_contents:
            # 所有方括号都是元数据 → 跳过
            return False, None

    # 2b. 分离方括号内容，在剩余文本中搜索番号
    title_without_brackets = re.sub(r'\[[^\]]*\]', ' ', title)

    # --- 正向匹配: 执行番号正则 ---
    match = FANHAO_REGEX.search(title_without_brackets)
    if match:
        # 找到匹配的番号
        fanhao = match.group(0)
        fh_upper = fanhao.upper()
        
        # 1. 黑名单正则匹配过滤
        if BLACKLIST_REGEX.match(fh_upper):
            return False, None
            
        # 2. 排除域名后缀被误切（例如 6a88.vip 匹配出 6A88）
        # 检查原标题中匹配到的部分后面是否紧接着 .vip, .cc 等域名后缀
        start_idx = title_without_brackets.lower().find(fanhao.lower())
        if start_idx != -1:
            after_text = title_without_brackets[start_idx + len(fanhao):].strip()
            if after_text.lower().startswith(('.vip', '.cc', '.com', '.net', '.org', '.xyz')):
                return False, None
                
        # 3. 过滤一些特定的单词
        if fh_upper in ('COM', 'VIP', 'AV', 'HD', 'FHD', 'DVD', 'BD', 'VR', 'AI', '3D', '4K', '8K'):
            return False, None

        # 4. 过滤尾部数字 (年份无条件过滤，分辨率视连字符条件过滤)
        bad_resolutions = {360, 480, 576, 720, 960, 1080, 1280, 1920, 2160, 3840}
        bad_years = {
            2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019,
            2020, 2021, 2022, 2023, 2024, 2025, 2026, 2027, 2028, 2029, 2030
        }
        num_match = re.search(r'\d+$', fanhao)
        if num_match:
            num_val = int(num_match.group(0))
            # 年份无条件过滤 (如 KIKI-2025)
            if num_val in bad_years:
                return False, None
            # 分辨率只在非 '-' (下划线或直接拼接，如 Acosta_720, chick1920) 时过滤
            if num_val in bad_resolutions and '-' not in fanhao:
                return False, None

        return True, fanhao

    return False, None


def has_fanhao(title):
    """快速判断标题是否包含番号"""
    found, _ = extract_fanhao(title)
    return found


# ============================================================
# 3. 数据库操作
# ============================================================

def get_columns(cursor):
    """获取 resources 表所有列名"""
    cursor.execute("PRAGMA table_info(resources)")
    cols = [row[1] for row in cursor.fetchall()]
    return cols


def scan_fanhao_records(conn):
    """
    扫描数据库，返回匹配番号的记录列表。
    每项为 (matched_fanhao, row_data_dict)
    """
    cursor = conn.cursor()
    columns = get_columns(cursor)
    cursor.execute(f"SELECT {', '.join(columns)} FROM resources ORDER BY id")
    rows = cursor.fetchall()

    matches = []
    for row in rows:
        row_dict = dict(zip(columns, row))
        title = row_dict.get('title', '')
        found, fanhao = extract_fanhao(title)
        if found:
            matches.append((fanhao, row_dict))

    return matches, columns


def export_to_new_db(matches, columns, output_path):
    """将匹配的记录导出到新的 SQLite 数据库"""
    print(f"\n[*] 正在导出 {len(matches)} 条记录到: {output_path}")
    
    if os.path.exists(output_path):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{output_path}.bak_{timestamp}"
        shutil.copy2(output_path, backup)
        print(f"[*] 已备份原有数据库: {backup}")
    
    conn = sqlite3.connect(output_path)
    cursor = conn.cursor()
    
    col_list = ', '.join(columns)
    placeholders = ', '.join(['?' for _ in columns])
    
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS resources (
            {', '.join(f'{col} TEXT' if col != 'id' else 'id INTEGER PRIMARY KEY' for col in columns)}
        )
    ''')
    
    # 确保有 fanhao 列
    try:
        cursor.execute("ALTER TABLE resources ADD COLUMN fanhao TEXT")
    except sqlite3.OperationalError:
        pass
    
    # 插入数据
    insert_cols = ', '.join(columns + ['fanhao'])
    insert_placeholders = ', '.join(['?' for _ in columns] + ['?'])
    
    cursor.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_url ON resources(url)")
    
    for fanhao, row_dict in matches:
        values = [row_dict.get(col) for col in columns]
        values.append(fanhao)
        cursor.execute(
            f"INSERT OR IGNORE INTO resources ({insert_cols}) VALUES ({insert_placeholders})",
            values
        )
    
    conn.commit()
    print(f"[+] 导出完成！新数据库共 {len(matches)} 条记录")
    conn.close()


def delete_records(conn, matches, dry_run=False):
    """删除匹配的记录，并可选删除对应的 PDF 文件"""
    ids_to_delete = [row_dict['id'] for _, row_dict in matches]
    pdf_files_to_delete = []
    
    for _, row_dict in matches:
        pdf_path = row_dict.get('pdf_path', '')
        if pdf_path:
            # 支持相对路径和绝对路径
            abs_path = pdf_path
            if not os.path.isabs(pdf_path):
                # 尝试从项目根目录解析
                project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                abs_path = os.path.join(project_dir, pdf_path)
            if os.path.exists(abs_path):
                pdf_files_to_delete.append(abs_path)
    
    print(f"\n{'='*60}")
    print(f"[操作] 准备删除 {len(ids_to_delete)} 条数据库记录")
    if pdf_files_to_delete:
        print(f"[操作] 同时删除 {len(pdf_files_to_delete)} 个 PDF 文件")
    
    if dry_run:
        print("\n[预览模式] 以下记录将被删除:")
        for i, (fanhao, row_dict) in enumerate(matches, 1):
            title = row_dict.get('title', '')[:60]
            pdf = row_dict.get('pdf_path', '')
            print(f"  {i:4d}. [{fanhao}] {title}")
            if pdf:
                print(f"       PDF: {pdf}")
        print(f"\n[预览] 共 {len(ids_to_delete)} 条记录, {len(pdf_files_to_delete)} 个 PDF 文件将被删除")
        return
    
    # 确认
    confirm = input(f"\n[?] 确定要删除这 {len(ids_to_delete)} 条记录 {'和 '+str(len(pdf_files_to_delete))+' 个 PDF 文件' if pdf_files_to_delete else ''}？(yes/NO): ").strip().lower()
    if confirm != 'yes':
        print("[-] 已取消操作")
        return
    
    cursor = conn.cursor()
    
    # 1. 删除 PDF 文件
    deleted_pdfs = 0
    failed_pdfs = 0
    for pdf_path in pdf_files_to_delete:
        try:
            os.remove(pdf_path)
            deleted_pdfs += 1
            print(f"  [删除PDF] {pdf_path}")
        except Exception as e:
            failed_pdfs += 1
            print(f"  [-删除PDF失败] {pdf_path}: {e}")
    
    # 2. 删除数据库记录
    deleted_count = 0
    batch_size = 500
    for i in range(0, len(ids_to_delete), batch_size):
        batch = ids_to_delete[i:i+batch_size]
        placeholders = ','.join(['?' for _ in batch])
        cursor.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", batch)
        deleted_count += cursor.rowcount
    
    conn.commit()
    
    print(f"\n[+] 操作完成！")
    print(f"  删除数据库记录: {deleted_count} 条")
    print(f"  删除 PDF 文件: {deleted_pdfs} 个{' (失败: {})'.format(failed_pdfs) if failed_pdfs else ''}")
    print(f"  数据库中剩余记录: {get_total_count(conn)} 条")


def get_total_count(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM resources")
    return cursor.fetchone()[0]


# ============================================================
# 4. 交互式控制台询问
# ============================================================

def ask_yes_no(prompt, default=False):
    """交互式 yes/no 询问"""
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} ({hint}): ").strip().lower()
        if not answer:
            return default
        if answer in ('y', 'yes'):
            return True
        if answer in ('n', 'no'):
            return False
        print("  请输入 y 或 n。")


def ask_choice(prompt, choices, default=None):
    """交互式选项选择"""
    print(f"\n{prompt}")
    for i, (key, desc) in enumerate(choices, 1):
        marker = " [默认]" if key == default else ""
        print(f"  {i}. {key} - {desc}{marker}")
    while True:
        answer = input(f"  请输入选项 ({'/'.join(k for k, _ in choices)}): ").strip().lower()
        if not answer and default:
            return default
        if answer in (k for k, _ in choices):
            return answer
        # 也支持数字
        if answer.isdigit():
            idx = int(answer) - 1
            if 0 <= idx < len(choices):
                return choices[idx][0]
        print(f"  无效选项，请重新输入。")


def ask_path(prompt, default=None, must_exist=False):
    """交互式路径输入"""
    while True:
        answer = input(f"{prompt}{' (直接回车使用默认值)' if default else ''}: ").strip()
        if not answer and default:
            return default
        if not answer:
            print("  路径不能为空。")
            continue
        if must_exist and not os.path.exists(answer):
            print(f"  路径不存在: {answer}")
            continue
        return answer


def show_results_preview(matches):
    """显示匹配结果的预览列表"""
    print(f"\n{'='*60}")
    print(f"[*] 匹配到 {len(matches)} 条含番号的记录:")
    print(f"{'='*60}")
    # 按番号前缀分组统计
    prefix_stats = {}
    for fanhao, _ in matches:
        prefix = re.sub(r'[\d\-_].*$', '', fanhao).upper()
        prefix_stats[prefix] = prefix_stats.get(prefix, 0) + 1
    
    print(f"\n[*] 番号前缀分布 (Top 20):")
    for prefix, count in sorted(prefix_stats.items(), key=lambda x: -x[1])[:20]:
        print(f"    {prefix:<10s} : {count:5d} 条")
    
    show_detail = ask_yes_no("\n[*] 是否查看详细匹配列表?", False)
    if show_detail:
        print(f"\n[*] 匹配详情:")
        for i, (fanhao, row_dict) in enumerate(matches, 1):
            title = row_dict.get('title', '')[:70]
            url = row_dict.get('url', '')
            print(f"  {i:4d}. [{fanhao}] {title}")
            if url:
                print(f"       URL: {url}")
                if i >= 100:
                    print(f"  ... 及另外 {len(matches) - 100} 条")
                    break


def interactive_mode():
    """交互式控制台模式：逐步询问用户"""
    print(f"\n{'='*60}")
    print("  番号过滤工具 - 交互式模式")
    print(f"{'='*60}\n")

    # 1. 询问数据库路径
    default_db = get_db_path()
    db_path = ask_path("请输入数据库路径", default=default_db, must_exist=True)
    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)

    # 连接数据库并获取基本信息
    conn = sqlite3.connect(db_path)
    total = get_total_count(conn)
    print(f"\n[*] 数据库总记录数: {total}")

    # 扫描番号
    print("[*] 正在扫描所有标题中的番号...")
    matches, columns = scan_fanhao_records(conn)
    print(f"[*] 找到含番号的记录: {len(matches)} 条 ({len(matches)/total*100:.1f}%)")

    if not matches:
        print("[*] 没有找到任何含番号的记录，退出。")
        conn.close()
        return

    conn.close()

    # 显示预览
    show_results_preview(matches)

    # 2. 询问操作模式
    print(f"\n{'='*60}")
    mode = ask_choice(
        "请选择操作模式:",
        [("export", "导出匹配记录到新数据库"), ("delete", "删除匹配记录 (含PDF)")],
        default="export"
    )

    # 3. 询问是否预览
    dry_run = ask_yes_no("是否仅预览 (不执行实际操作)?", default=False)

    # 4. 如果导出模式，询问输出路径
    output_path = None
    if mode == "export":
        default_output = None
        base_dir = os.path.dirname(db_path)
        base_name = os.path.splitext(os.path.basename(db_path))[0]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_output = os.path.join(base_dir, f"{base_name}_fanhao_only_{timestamp}.db")
        output_path = ask_path("请输入导出数据库路径", default=default_output)

    # 5. 询问是否显示详细日志
    verbose = ask_yes_no("是否显示每条记录的匹配详情?", False)

    # 6. 确认执行
    print(f"\n{'='*60}")
    print(f"[*] 操作确认:")
    print(f"  数据库:     {db_path}")
    print(f"  操作模式:   {'导出' if mode == 'export' else '删除'}")
    print(f"  预览模式:   {'是 (不实际执行)' if dry_run else '否'}")
    print(f"  匹配记录:   {len(matches)} 条")
    if mode == "export" and output_path:
        print(f"  输出路径:   {output_path}")
    if verbose:
        print(f"  详细日志:   是")
    print(f"{'='*60}")

    if not ask_yes_no("\n确认执行以上操作?", default=False):
        print("[-] 已取消操作")
        return

    # 7. 执行
    if mode == "export":
        if dry_run:
            print("\n[预览模式] 导出预览:")
            for i, (fanhao, row_dict) in enumerate(matches, 1):
                title = row_dict.get('title', '')[:60]
                print(f"  {i:4d}. [{fanhao}] {title}")
            print(f"\n[预览] 共 {len(matches)} 条记录【将会】导出到: {output_path}")
        else:
            export_to_new_db(matches, columns, output_path)
    elif mode == "delete":
        conn = sqlite3.connect(db_path)
        delete_records(conn, matches, dry_run=dry_run)
        conn.close()


# ============================================================
# 5. 主入口
# ============================================================

def main():
    # 检查是否提供了命令行参数
    if len(sys.argv) == 1:
        # 无参数时进入交互式模式
        interactive_mode()
        return

    parser = argparse.ArgumentParser(
        description="番号过滤工具：扫描数据库中所有标题，识别含番号的记录，支持导出或删除。"
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        default=False,
        help="进入交互式控制台模式（逐步询问）"
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["export", "delete"],
        help="操作模式: export=导出匹配记录到新数据库, delete=删除匹配记录(含PDF)"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        default=False,
        help="仅预览，不执行实际操作"
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="导出模式下的输出数据库路径（默认: 自动生成）"
    )
    parser.add_argument(
        "--db-path", "-d",
        default=None,
        help="指定要处理的数据库路径（默认使用 config.get_db_path()）"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="显示每条记录的匹配详情"
    )
    args = parser.parse_args()

    # 交互式模式（显式指定 --interactive）
    if args.interactive:
        interactive_mode()
        return

    # 非交互式模式要求 --mode 参数
    if not args.mode:
        parser.error("非交互式模式需要指定 --mode 参数（使用 --interactive 可进入交互式模式）")

    # 确定数据库路径
    db_path = args.db_path or get_db_path()
    if not os.path.exists(db_path):
        print(f"[-] 错误: 数据库文件不存在: {db_path}")
        sys.exit(1)
    
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] 操作模式: {'导出' if args.mode == 'export' else '删除'}"
          f"{' [预览模式]' if args.dry_run else ''}")
    print()

    # 连接数据库
    conn = sqlite3.connect(db_path)
    total = get_total_count(conn)
    print(f"[*] 数据库中总记录数: {total}")

    # 扫描匹配番号的记录
    print("[*] 正在扫描所有标题中的番号...")
    matches, columns = scan_fanhao_records(conn)
    print(f"[*] 找到含番号的记录: {len(matches)} 条 ({len(matches)/total*100:.1f}%)")

    if not matches:
        print("[*] 没有找到任何含番号的记录，退出。")
        conn.close()
        return

    # 统计最热门的番号前缀
    prefix_stats = {}
    for fanhao, _ in matches:
        prefix = re.sub(r'[\d\-_].*$', '', fanhao).upper()
        prefix_stats[prefix] = prefix_stats.get(prefix, 0) + 1
    
    top_prefixes = sorted(prefix_stats.items(), key=lambda x: -x[1])[:20]
    print(f"\n[*] Top 20 番号前缀分布:")
    for prefix, count in top_prefixes:
        print(f"    {prefix:<10s} : {count:5d} 条")

    if args.verbose:
        print(f"\n[*] 匹配详情 (前 30 条):")
        for i, (fanhao, row_dict) in enumerate(matches[:30], 1):
            title = row_dict.get('title', '')
            url = row_dict.get('url', '')
            print(f"  {i:4d}. [{fanhao}] {title[:70]}")
            if url:
                print(f"       URL: {url}")

    print()
    print("=" * 60)

    # --- 执行操作 ---
    if args.mode == 'export':
        if args.dry_run:
            print("[预览模式] 导出预览:")
            for i, (fanhao, row_dict) in enumerate(matches, 1):
                title = row_dict.get('title', '')[:60]
                print(f"  {i:4d}. [{fanhao}] {title}")
            print(f"\n[预览] 共 {len(matches)} 条记录将被导出")
        else:
            output_path = args.output
            if not output_path:
                base_dir = os.path.dirname(db_path)
                base_name = os.path.splitext(os.path.basename(db_path))[0]
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_path = os.path.join(base_dir, f"{base_name}_fanhao_only_{timestamp}.db")
                print(f"[*] 未指定输出路径，自动生成: {output_path}")
            export_to_new_db(matches, columns, output_path)

    elif args.mode == 'delete':
        delete_records(conn, matches, dry_run=args.dry_run)

    conn.close()


if __name__ == '__main__':
    main()