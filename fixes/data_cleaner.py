"""数据清洗与元数据修复工具合集 (fixes/data_cleaner.py)

本脚本集成了数据库记录清洗、URL 域名修正、数据库结构升级、磁力元数据补全及空链接重抓回填等全流程数据维护功能。

包含以下 5 大核心功能与子命令：

1. clean-noise: 清理 resource_link 广告与标签噪声
   - 功能用途: 扫描 resources 表中所有 resource_link 字段，剔除广告推广行、下载渠道废弃说明、多余标签以及无用空行。
   - 安全机制: 默认 Dry-Run 预览模式，正式执行 (--run) 前自动创建带时间戳的数据库备份。
   - 多云同步: 支持通过参数 --sync-supabase 与 --sync-dynamodb 将清洗后的链接同步更新至云端 Supabase 与 AWS DynamoDB。
   - 审计导出: 支持通过 --export-csv 导出清洗前后的明细对照。

2. replace-domain: 批量替换 URL 中的域名或镜像子串
   - 功能用途: 当采集源网站域名变更或发布页镜像更新时，批量替换 resources.url 中的旧域名（如 dyh.393659.xyz 替换为 dtn.628563.xyz）。
   - 安全机制: 默认预览变更匹配样例，确认后使用 --run 正式写入并备份数据库。

3. upgrade-db: 数据库表结构升级与历史元数据提取
   - 功能用途:
     (1) 结构升级: 自动检测 resources 表结构，升级对齐为标准的 12 字段并建立 url 唯一索引。
     (2) 元数据提取: 全量扫描历史记录的 title 与 resource_link，自动正则解析出视频大小 (size)、清晰度/格式 (resource_format) 及 PikPak 分享链接 (pikpak_link) 并批量回填。

4. fetch-sizes: Darklyn API 磁力链接大小批量补全与看门狗守护
   - 功能用途: 扫描数据库中缺失 size 的磁力链接，并发调用 Darklyn API 批量查询真实文件大小并回填数据库。
   - 健壮性保障: 自动识别并过滤假种子与蜜罐文件；结果持久化缓存在统一导出目录的 result_darklyn.json 中。
   - 运行模式:
     * 常规抓取: 并发抓取指定条数 (--limit) 并通过 --run (或 --apply) 写库。
     * 看门狗模式 (--watch): 遇 API 故障或不可达时进入守护探测循环，一旦服务恢复自动触发抓取并入库。

5. fetch-empty-links: Playwright 重新访问页面抓取并回填空资源链接
   - 功能用途: 针对数据库中 resource_link 为空的记录，根据 url 过滤指定站点 (--site，默认 seju.life)，拉起 Playwright 无头浏览器重新请求页面，解析正文回填 resource_link。

用法与命令示例:
  python fixes/data_cleaner.py                                       # 进入交互式主菜单 (常驻循环)
  python fixes/data_cleaner.py clean-noise                           # 预览待清洗的链接噪音
  python fixes/data_cleaner.py clean-noise --run --sync-supabase     # 正式执行清洗并同步到 Supabase
  python fixes/data_cleaner.py replace-domain --old OLD --new NEW    # 预览域名替换
  python fixes/data_cleaner.py replace-domain --old OLD --new NEW --run # 正式执行域名替换
  python fixes/data_cleaner.py upgrade-db --run                      # 升级表结构并提取历史元数据
  python fixes/data_cleaner.py fetch-sizes --limit 100 --run         # 抓取 100 条磁力大小并写回数据库
  python fixes/data_cleaner.py fetch-sizes --watch --interval 600    # 启动看门狗守护模式
  python fixes/data_cleaner.py fetch-empty-links --site seju.life --run # 重新抓取回填空资源链接
"""

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# ========== 路径引导与环境初始化 ==========
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fixes.db_utils import (  # noqa: E402
    setup_fixes_module,
    get_connection,
    get_db_path,
    get_total_count,
    backup_db,
    vacuum_db,
    get_export_dir,
    get_timestamp,
    export_records_to_db,
    export_to_csv,
    print_banner,
    print_section,
    print_step,
    print_success,
    print_warning,
    print_error,
    confirm_action,
    pause_for_user,
)

setup_fixes_module()

from config import (  # noqa: E402
    DB_PATHS,
    SUPABASE_URL,
    SUPABASE_KEY,
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    AWS_REGION,
)
from utils.logger import get_logger  # noqa: E402
from utils.metadata_parser import parse_title, parse_pikpak_link  # noqa: E402
from utils.resource_link_cleaner import clean_resource_link, clean_resource_lines  # noqa: E402

logger = get_logger(__name__)

DYNAMODB_TABLE = "fuli_resources"


# ===================================================================
# 1. clean-noise: 清洗 resource_link 中的广告与标签行
# ===================================================================

def find_dirty_link_records(conn: Any) -> List[Tuple[int, str, str, str]]:
    """扫描全部记录，返回需要清洗的 (id, url, old_link, new_link) 列表"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, url, resource_link FROM resources "
        "WHERE resource_link IS NOT NULL AND resource_link != ''"
    )
    rows = cursor.fetchall()
    changes = []
    for row_id, url, old_link in rows:
        new_link = clean_resource_link(old_link)
        if new_link != old_link:
            changes.append((row_id, url, old_link, new_link))
    return changes


def sync_supabase_links(changes: List[Tuple[int, str, str, str]]) -> None:
    """将清洗结果同步到云端 Supabase"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print_warning("未配置 SUPABASE_URL / SUPABASE_KEY，跳过 Supabase 同步。")
        return

    from urllib.parse import urlparse
    from supabase import create_client

    parsed = urlparse(SUPABASE_URL.strip())
    clean_url = f"{parsed.scheme}://{parsed.netloc}"
    client = create_client(clean_url, SUPABASE_KEY.strip())

    success = 0
    failed = 0
    for idx, (row_id, url, old_link, new_link) in enumerate(changes, 1):
        if not url:
            continue
        try:
            client.table("resources").update(
                {"resource_link": new_link}
            ).eq("url", url).execute()
            success += 1
        except Exception as e:
            failed += 1
            print_error(f"Supabase 更新失败 (url={url[:60]}...): {e}")
        if idx % 200 == 0:
            print_step(f"Supabase 进度: {idx}/{len(changes)} (成功 {success}, 失败 {failed})")
    print_success(f"Supabase 同步完成: 成功 {success} 条, 失败 {failed} 条")


def sync_dynamodb_links(changes: List[Tuple[int, str, str, str]]) -> None:
    """将清洗结果同步到 AWS DynamoDB"""
    if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
        print_warning("未配置 AWS 凭证，跳过 DynamoDB 同步。")
        return

    import boto3
    from botocore.exceptions import ClientError

    dynamodb = boto3.client(
        "dynamodb",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )

    success = 0
    failed = 0
    skipped = 0
    for idx, (row_id, url, old_link, new_link) in enumerate(changes, 1):
        if not url:
            continue
        try:
            dynamodb.update_item(
                TableName=DYNAMODB_TABLE,
                Key={"url": {"S": url}},
                UpdateExpression="SET resource_link = :rl",
                ExpressionAttributeValues={":rl": {"S": new_link}},
                ConditionExpression="attribute_exists(url)",
            )
            success += 1
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                skipped += 1
            else:
                failed += 1
                print_error(f"DynamoDB 更新失败 (url={url[:60]}...): {e}")
        except Exception as e:
            failed += 1
            print_error(f"DynamoDB 更新失败 (url={url[:60]}...): {e}")
        if idx % 500 == 0:
            print_step(f"DynamoDB 进度: {idx}/{len(changes)} (成功 {success}, 失败 {failed})")
    print_success(f"DynamoDB 同步完成: 成功 {success} 条, 跳过 {skipped} 条, 失败 {failed} 条")


def run_clean_noise(args) -> None:
    """清洗 resource_link 中的广告与标签行"""
    db_path = get_db_path(getattr(args, "db", None))
    is_run = getattr(args, "run", False) or getattr(args, "yes", False)

    print_banner("清理 resource_link 残留标签行与推广噪音")
    print(f"[*] 运行模式: {'【正式执行模式 (RUN)】' if is_run else '【预览模式 (DRY RUN)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print_error(f"数据库文件不存在: {db_path}")
        return

    conn = get_connection(db_path)
    try:
        changes = find_dirty_link_records(conn)
    except Exception as e:
        print_error(f"扫描数据库失败: {e}")
        conn.close()
        return

    print_step(f"需要清洗的记录数: {len(changes)}")
    if not changes:
        print_success("没有需要清洗的记录。")
        conn.close()
        return

    print_section("样例预览（最多 8 条）")
    for row_id, url, old_link, new_link in changes[:8]:
        print(f"  ID={row_id}  url={url}")
        print(f"    BEFORE: {repr(old_link[:120])}")
        print(f"    AFTER : {repr(new_link[:120])}")
    print("─" * 60)

    if getattr(args, "export_csv", False):
        export_recs = [
            {"id": r[0], "url": r[1], "old_resource_link": r[2], "cleaned_resource_link": r[3]}
            for r in changes
        ]
        export_to_csv(export_recs, f"clean_noise_audit_{get_timestamp()}.csv")

    if not is_run:
        print_step("当前为预览模式，未执行任何写入操作。")
        print_step("确认无误后请附加 --run 或 -y 参数正式执行。")
        conn.close()
        return

    try:
        backup_db(db_path, prefix_tag="clean_noise")
    except Exception:
        conn.close()
        return

    cursor = conn.cursor()
    cursor.executemany(
        "UPDATE resources SET resource_link = ? WHERE id = ?",
        [(new_link, row_id) for row_id, url, old_link, new_link in changes],
    )
    conn.commit()
    print_success(f"本地 SQLite 更新完成: {len(changes)} 条记录。")

    if getattr(args, "sync_supabase", False):
        sync_supabase_links(changes)
    if getattr(args, "sync_dynamodb", False):
        sync_dynamodb_links(changes)

    conn.close()
    print_success("清洗完成！")


# ===================================================================
# 2. replace-domain: 批量替换 URL 域名或子串
# ===================================================================

def run_replace_domain(args) -> None:
    """批量替换 URL 中的指定域名或子串"""
    db_path = get_db_path(getattr(args, "db", None))
    old_val = getattr(args, "old", None) or "dyh.393659.xyz"
    new_val = getattr(args, "new", None) or "dtn.628563.xyz"
    is_run = getattr(args, "run", False) or getattr(args, "yes", False)

    print_banner("批量替换 URL 域名 / 镜像前缀")
    print(f"[*] 运行模式: {'【正式执行模式 (RUN)】' if is_run else '【预览模式 (DRY RUN)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] 待替换内容: {old_val} -> {new_val}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print_error(f"数据库文件不存在: {db_path}")
        return

    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, url FROM resources WHERE url LIKE ?", (f"%{old_val}%",))
    rows = cursor.fetchall()
    print_step(f"找到 {len(rows)} 条包含 '{old_val}' 的记录:")

    if not rows:
        print_success("没有需要替换的记录。")
        conn.close()
        return

    for r_id, url in rows[:10]:
        print(f"  ID={r_id}: {url} -> {url.replace(old_val, new_val)}")
    if len(rows) > 10:
        print(f"  ... 以及其他 {len(rows) - 10} 条记录")

    if not is_run:
        print_step("\n当前为预览模式，未修改数据库。")
        print_step(f"确认替换请附加 --run 参数。")
        conn.close()
        return

    backup_db(db_path, prefix_tag="replace_domain")

    updated_count = 0
    for row_id, old_url in rows:
        new_url = old_url.replace(old_val, new_val)
        cursor.execute("UPDATE resources SET url = ? WHERE id = ?", (new_url, row_id))
        updated_count += 1

    conn.commit()
    conn.close()
    print_success(f"成功更新 {updated_count} 条记录！")


# ===================================================================
# 3. upgrade-db: 数据库表结构迁移与元数据提取
# ===================================================================

def upgrade_single_database(db_path: str, is_run: bool = True) -> bool:
    """升级单个数据库的表结构并提取填入元数据"""
    print_section(f"开始处理数据库: {db_path}")
    if not os.path.exists(db_path):
        print_warning(f"数据库文件不存在，跳过: {db_path}")
        return False

    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("PRAGMA table_info(resources)")
        columns_info = cursor.fetchall()
        if not columns_info:
            print_warning("未找到 resources 表，可能尚未初始化。")
            conn.close()
            return False

        current_columns = [col[1] for col in columns_info]
        target_columns = [
            "id", "title", "publish_time", "category", "resource_link",
            "pikpak_link", "size", "resource_format", "link_type",
            "url", "pdf_path", "source"
        ]

        need_migration = (current_columns != target_columns)

        if need_migration:
            if not is_run:
                print_step(f"【预览】检测到表结构需要升级对齐 12 字段。当前字段: {current_columns}")
                conn.close()
                return True

            print_step("检测到表结构需要升级重构，正在备份并执行数据迁移...")
            backup_db(db_path, prefix_tag="schema_migration")

            cursor.execute("BEGIN TRANSACTION")
            cursor.execute("""
                CREATE TABLE resources_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    publish_time TEXT,
                    category TEXT,
                    resource_link TEXT NOT NULL,
                    pikpak_link TEXT,
                    size TEXT,
                    resource_format TEXT,
                    link_type TEXT,
                    url TEXT,
                    pdf_path TEXT,
                    source TEXT
                )
            """)

            select_parts = [col if col in current_columns else "NULL" for col in target_columns]
            copy_sql = f"INSERT INTO resources_new ({', '.join(target_columns)}) SELECT {', '.join(select_parts)} FROM resources"
            cursor.execute(copy_sql)
            cursor.execute("DROP TABLE resources")
            cursor.execute("ALTER TABLE resources_new RENAME TO resources")
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_url ON resources(url)")
            conn.commit()
            print_success("成功迁移表结构并对齐目标字段！")
        else:
            print_step("数据库表结构已是最新，无需迁移结构。")

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print_error(f"数据库结构迁移失败: {e}")
        conn.close()
        return False

    if not is_run:
        print_step("【预览模式】历史元数据提取预览完成。")
        conn.close()
        return True

    # 提取并更新历史元数据
    print_step("正在扫描历史数据进行元数据提取（size/format/pikpak）...")
    cursor.execute("SELECT id, title, resource_link FROM resources")
    rows = cursor.fetchall()
    total_records = len(rows)
    print_step(f"共找到 {total_records} 条记录。")

    update_data = []
    matched_count = 0
    pikpak_count = 0

    for row_id, title, res_link in rows:
        size_val, res_format = parse_title(title or "")
        pikpak_link = parse_pikpak_link(res_link or "")

        if size_val or res_format:
            matched_count += 1
        if pikpak_link:
            pikpak_count += 1

        update_data.append((size_val, res_format, pikpak_link, row_id))

    if update_data:
        print_step("正在批量写入元数据...")
        cursor.executemany(
            "UPDATE resources SET size = ?, resource_format = ?, pikpak_link = ? WHERE id = ?",
            update_data,
        )
        conn.commit()
        if total_records > 0:
            print_success(f"批量更新完成！成功解析填入 size/format: {matched_count}/{total_records} ({matched_count/total_records:.1%})")
            print_success(f"成功解析填入 PikPak 链接: {pikpak_count}/{total_records}")
    conn.close()
    return True


def run_upgrade_db(args) -> None:
    """升级数据库结构与元数据"""
    target_db = getattr(args, "db", None)
    is_run = getattr(args, "run", False) or getattr(args, "yes", False)
    paths = [target_db] if target_db else DB_PATHS
    updated_any = False
    for path in paths:
        if upgrade_single_database(path, is_run=is_run):
            updated_any = True
    if not updated_any:
        print_warning("未找到任何有效数据库文件，请检查配置。")


# ===================================================================
# 4. fetch-sizes: Darklyn 磁力链接大小抓取与看门狗
# ===================================================================

DARKLYN_API = "https://magnet-metadata-api.darklyn.org/api/v1/metadata"
FAKE_NAME = "001FF871FB8B9348FCD8ECBFDAACC85D3CCAB00F.exe"
FAKE_SIZE = 877597184
RESULT_FILE = os.path.join(get_export_dir(), "result_darklyn.json")
LOG_FILE = os.path.join(get_export_dir(), "darklyn_progress.log")

_darklyn_lock = threading.Lock()
_darklyn_results: Dict[str, dict] = {}
_darklyn_done = 0
_darklyn_total = 0


def _darklyn_log(line: str, prefix: str = "") -> None:
    tag = f" [{prefix}]" if prefix else ""
    log_line = f"{time.strftime('%H:%M:%S')}{tag} {line}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_line)
            f.flush()
    except Exception:
        pass
    print(log_line.strip())


def _load_empty_hashes(db_path: str, limit: int = 0) -> List[str]:
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT resource_link FROM resources "
        "WHERE (size IS NULL OR TRIM(size) = '') AND resource_link LIKE 'magnet:%'"
    )
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    hashes = []
    for r in rows:
        m = re.search(r"urn:btih:([0-9a-fA-F]{40})", r)
        if m:
            hashes.append(m.group(1).lower())
    hashes = sorted(set(hashes))
    return hashes[:limit] if limit > 0 else hashes


def _load_darklyn_existing() -> Dict[str, dict]:
    if os.path.exists(RESULT_FILE):
        try:
            with open(RESULT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            _darklyn_log(f"读取现有结果文件失败: {e}", prefix="warn")
    return {}


def _save_darklyn_results() -> None:
    with _darklyn_lock:
        try:
            with open(RESULT_FILE, "w", encoding="utf-8") as f:
                json.dump(_darklyn_results, f, ensure_ascii=False, indent=1)
        except Exception as e:
            _darklyn_log(f"保存结果失败: {e}", prefix="error")


def _darklyn_query(h: str, timeout: int = 40) -> Tuple[Optional[int], str]:
    import requests
    try:
        r = requests.post(DARKLYN_API, json={"magnet_uri": "magnet:?xt=urn:btih:" + h}, timeout=timeout)
        if r.status_code != 200:
            return None, f"HTTP{r.status_code}"
        d = r.json()
        size = d.get("size")
        if not size:
            return None, "notfound"
        if size == FAKE_SIZE or d.get("name") == FAKE_NAME:
            return None, "fake"
        return int(size), "ok"
    except requests.RequestException:
        return None, "network"


def _darklyn_worker(h: str) -> None:
    global _darklyn_done
    t0 = time.time()
    size, status = _darklyn_query(h)
    if size is None and status == "network":
        time.sleep(3)
        size, status = _darklyn_query(h)
    with _darklyn_lock:
        _darklyn_results[h] = {"size": size, "status": status}
        _darklyn_done += 1
        n = _darklyn_done
    elapsed = time.time() - t0
    if size:
        _darklyn_log(f"[{n}/{_darklyn_total}] {h[:12]} OK {size} ({elapsed:.0f}s)")
    else:
        _darklyn_log(f"[{n}/{_darklyn_total}] {h[:12]} {status} ({elapsed:.0f}s)")
    if n % 20 == 0:
        _save_darklyn_results()


def _darklyn_run_fetch(db_path: str, limit: int = 0, concurrency: int = 3, retry: bool = False) -> int:
    global _darklyn_total, _darklyn_done
    existing = _load_darklyn_existing()
    _darklyn_results.update(existing)
    hashes = _load_empty_hashes(db_path, limit)

    if retry:
        todo = [h for h in hashes if h not in _darklyn_results or not _darklyn_results.get(h, {}).get("size")]
    else:
        todo = [h for h in hashes if h not in _darklyn_results]

    _darklyn_total = len(todo)
    _darklyn_done = 0
    print_step(f"待处理 {_darklyn_total} 条（已跳过 {len(hashes) - _darklyn_total} 条）")
    _darklyn_log(f"开始: 待处理 {_darklyn_total} 条, 并发 {concurrency}")

    if not todo:
        ok = sum(1 for v in _darklyn_results.values() if v.get("size"))
        print_success(f"无需抓取: 已命中 {ok} / {len(_darklyn_results)} ({(100.0 * ok / max(len(_darklyn_results), 1)):.1f}%)")
        return ok

    t_start = time.time()
    last_report = 0
    idx = 0
    while idx < len(todo):
        batch = todo[idx : idx + concurrency]
        idx += concurrency
        threads = [threading.Thread(target=_darklyn_worker, args=(h,), daemon=True) for h in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if _darklyn_done - last_report >= 50:
            last_report = _darklyn_done
            rate = _darklyn_done / max(time.time() - t_start, 1)
            eta = (_darklyn_total - _darklyn_done) / max(rate, 1e-9)
            _darklyn_log(f"进度: {_darklyn_done}/{_darklyn_total} ({100.0 * _darklyn_done / _darklyn_total:.0f}%) 速率 {rate:.2f}条/秒 ETA {eta / 60:.0f} 分钟")

    _save_darklyn_results()
    ok = sum(1 for v in _darklyn_results.values() if v.get("size"))
    print_success(f"抓取完成: 命中 {ok} / {len(_darklyn_results)} ({(100.0 * ok / max(len(_darklyn_results), 1)):.1f}%)")
    return ok


def _darklyn_apply_to_db(db_path: str) -> int:
    existing = _load_darklyn_existing()
    if not existing:
        print_warning("未找到有效的结果文件或结果为空，取消写库。")
        return 0

    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, resource_link FROM resources "
        "WHERE (size IS NULL OR TRIM(size) = '') AND resource_link LIKE 'magnet:%'"
    )
    rows = cur.fetchall()
    updates = []
    for rid, link in rows:
        m = re.search(r"urn:btih:([0-9a-fA-F]{40})", link)
        if not m:
            continue
        v = existing.get(m.group(1).lower())
        if v and v.get("size"):
            updates.append((str(v["size"]), rid))

    if updates:
        cur.executemany("UPDATE resources SET size = ? WHERE id = ?", updates)
        conn.commit()
        print_success(f"已更新数据库 {len(updates)} 行")
        _darklyn_log(f"已写回数据库 {len(updates)} 条记录", prefix="db")
    else:
        print_step("数据库中没有需要更新的记录")
    conn.close()
    return len(updates)


def _darklyn_probe(h: str) -> Tuple[object, Optional[int]]:
    import requests
    try:
        r = requests.post(DARKLYN_API, json={"magnet_uri": "magnet:?xt=urn:btih:" + h}, timeout=20)
        if r.status_code != 200:
            return r.status_code, None
        j = r.json()
        name = j.get("name")
        size = j.get("size")
        if size == FAKE_SIZE or name == FAKE_NAME:
            return "fake", None
        return 200, size
    except requests.RequestException:
        return "ERR", None


def _darklyn_is_recovered(failed_hashes: List[str], probe_count: int = 3) -> bool:
    ok_count = 0
    test_sample = failed_hashes[:probe_count]
    for h in test_sample:
        code, size = _darklyn_probe(h)
        if code == 200 and size and size != FAKE_SIZE:
            ok_count += 1
        elif code == 500:
            ok_count += 1
    threshold = max(1, len(test_sample) // 2 + 1)
    return ok_count >= threshold


def _darklyn_run_watchdog(db_path: str, concurrency: int = 3, interval: int = 600, limit: int = 0) -> None:
    _darklyn_log("看门狗启动，准备检查待处理/失败列表...", prefix="watchdog")
    d = _load_darklyn_existing()
    failed = [h for h, v in d.items() if not v.get("size")]
    if not failed:
        all_empty = _load_empty_hashes(db_path, limit)
        failed = [h for h in all_empty if h not in d or not d.get(h, {}).get("size")]

    _darklyn_log(f"看门狗就绪: {len(failed)} 条待重试, 每 {interval} 秒探测一次", prefix="watchdog")

    while True:
        try:
            if _darklyn_is_recovered(failed):
                _darklyn_log(f"检测到服务已恢复, 开始执行抓取 (并发 {concurrency}) ...", prefix="watchdog")
                _darklyn_run_fetch(db_path=db_path, limit=limit, concurrency=concurrency, retry=True)
                _darklyn_log("抓取完成，开始写回数据库 ...", prefix="watchdog")
                _darklyn_apply_to_db(db_path=db_path)
                _darklyn_log("写库完成, 看门狗任务结束退出", prefix="watchdog")
                return
            _darklyn_log(f"服务未恢复(返回假种子或不可达), 将在 {interval} 秒后再次探测", prefix="watchdog")
            time.sleep(interval)
        except KeyboardInterrupt:
            _darklyn_log("收到中断信号，看门狗已安全退出", prefix="watchdog")
            break
        except Exception as e:
            _darklyn_log(f"探测循环异常: {e}，将在 {interval} 秒后重试", prefix="watchdog")
            time.sleep(interval)


def run_fetch_sizes(args) -> None:
    """通过 Darklyn API 批量补全磁力链接大小或运行看门狗"""
    db_path = get_db_path(getattr(args, "db", None))
    watch = getattr(args, "watch", False)
    limit = getattr(args, "limit", 0)
    concurrency = getattr(args, "concurrency", 3)
    retry = getattr(args, "retry", False)
    apply_db = getattr(args, "apply", False) or getattr(args, "run", False) or getattr(args, "yes", False)
    interval = getattr(args, "interval", 600)

    if watch:
        _darklyn_run_watchdog(db_path=db_path, concurrency=concurrency, interval=interval, limit=limit)
    elif apply_db and not retry and limit == 0:
        _darklyn_apply_to_db(db_path=db_path)
    else:
        _darklyn_run_fetch(db_path=db_path, limit=limit, concurrency=concurrency, retry=retry)
        if apply_db:
            _darklyn_apply_to_db(db_path=db_path)


# ===================================================================
# 5. fetch-empty-links: 使用 Playwright 重新抓取回填空资源链接
# ===================================================================

def run_fetch_empty_links(args) -> None:
    """使用 Playwright 访问页面重新抓取并回填空的 resource_link"""
    db_path = get_db_path(getattr(args, "db", None))
    domain_filter = getattr(args, "site", "") or "seju.life"
    is_run = getattr(args, "run", False) or getattr(args, "yes", False)
    limit = getattr(args, "limit", 0)

    print_banner("Playwright 重新抓取回填空 resource_link")
    print(f"[*] 运行模式: {'【正式执行 (RUN)】' if is_run else '【预览模式 (DRY RUN)】'}")
    print(f"[*] 数据库路径: {db_path}")
    print(f"[*] 域名匹配过滤: {domain_filter}")
    print("=" * 60)

    if not os.path.exists(db_path):
        print_error(f"数据库文件不存在: {db_path}")
        return

    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, url, title FROM resources WHERE resource_link IS NULL OR resource_link = ''"
    )
    records = cursor.fetchall()
    print_step(f"找到数据库中共有 {len(records)} 条 resource_link 为空的记录。")

    if domain_filter:
        records = [r for r in records if domain_filter in (r[1] or "")]
        print_step(f"匹配域名 '{domain_filter}' 的待处理记录数: {len(records)}")

    if limit > 0:
        records = records[:limit]
        print_step(f"限制处理条数: {limit}")

    if not records:
        print_success("没有需要修复的记录。")
        conn.close()
        return

    if not is_run:
        print_section("待处理记录样例预览 (Top 10)")
        for idx, (db_id, url, title) in enumerate(records[:10], 1):
            print(f"  {idx:3d}. ID={db_id}  title={(title or '')[:40]}  url={url}")
        print_step(f"共 {len(records)} 条记录待重新抓取。若确认执行，请附加 --run 或 -y 参数。")
        conn.close()
        return

    from playwright.sync_api import sync_playwright
    from utils.browser_manager import create_browser_context

    success_count = 0
    try:
        with sync_playwright() as p:
            browser, context = create_browser_context(p)
            page = context.new_page()

            for idx, (db_id, url, title) in enumerate(records, 1):
                print_step(f"[{idx}/{len(records)}] 正在处理: {(title or '')[:25]}... ID: {db_id}")
                try:
                    page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    page.wait_for_load_state("load", timeout=15000)

                    p_loc = page.locator('//article[@class="article-content"]//p')
                    p_count = p_loc.count()
                    p_texts = [p_loc.nth(i).text_content() for i in range(p_count)]
                    cleaned_p_texts = clean_resource_lines(p_texts)
                    res_link = "\n".join(cleaned_p_texts)

                    if res_link:
                        cursor.execute("UPDATE resources SET resource_link = ? WHERE id = ?", (res_link, db_id))
                        conn.commit()
                        success_count += 1
                        print_success(f"成功回填: {res_link[:50]}...")
                    else:
                        print_warning("提取结果仍为空")
                except Exception as page_e:
                    print_error(f"访问页面出错: {page_e}")

                time.sleep(random.uniform(1.5, 3.0))

            browser.close()
    except Exception as e:
        print_error(f"运行出错: {e}")
    finally:
        conn.close()
        print_success(f"修复完成！共成功回填了 {success_count} 条记录。")


# ===================================================================
# 6. 主入口与交互式菜单 (常驻循环)
# ===================================================================

def interactive_menu():
    """数据清洗与修复交互式主菜单 (常驻循环)"""
    while True:
        print_banner("数据清洗与元数据修复工具合集")
        print("  请选择要执行的操作：")
        print()
        print("    1. clean-noise       - 清理 resource_link 中的广告与标签行（支持同步云端）")
        print("    2. replace-domain    - 批量替换 URL 域名 / 镜像子串")
        print("    3. upgrade-db        - 升级数据库表结构并自动提取补全 size/format/pikpak")
        print("    4. fetch-sizes       - 通过 Darklyn API 批量补全磁力链接文件大小")
        print("    5. fetch-empty-links - 使用 Playwright 重新访问页面抓取并回填空资源链接")
        print()
        print("    0. 退出程序")
        print("=" * 60)

        try:
            choice = input("  请输入序号 [0-5] (直接回车默认 1): ").strip()
            if not choice:
                choice = "1"
        except (KeyboardInterrupt, EOFError):
            print("\n[*] 操作已取消。")
            break

        if choice in ("0", "q", "quit", "exit"):
            print_step("已退出程序。")
            break

        args = argparse.Namespace()
        setattr(args, "db", None)

        if choice == "1":
            confirm_run = confirm_action("是否正式执行写入？", default=False)
            setattr(args, "run", confirm_run)
            setattr(args, "sync_supabase", False)
            setattr(args, "sync_dynamodb", False)
            setattr(args, "export_csv", True)
            if confirm_run:
                setattr(args, "sync_supabase", confirm_action("是否同时同步到 Supabase？", default=False))
                setattr(args, "sync_dynamodb", confirm_action("是否同时同步到 DynamoDB？", default=False))
            run_clean_noise(args)

        elif choice == "2":
            old_val = input("  请输入旧域名/子串 (默认 dyh.393659.xyz): ").strip() or "dyh.393659.xyz"
            new_val = input("  请输入新域名/子串 (默认 dtn.628563.xyz): ").strip() or "dtn.628563.xyz"
            confirm_run = confirm_action("是否正式执行替换？", default=False)
            setattr(args, "old", old_val)
            setattr(args, "new", new_val)
            setattr(args, "run", confirm_run)
            run_replace_domain(args)

        elif choice == "3":
            if confirm_action("确定要升级数据库表结构并重新提取元数据？", default=False):
                setattr(args, "run", True)
                run_upgrade_db(args)
            else:
                print_step("已取消操作。")

        elif choice == "4":
            print("\n  1. 抓取磁力大小并写库")
            print("  2. 仅抓取磁力大小 (缓存到 json)")
            print("  3. 启动看门狗守护模式 (检测到服务恢复后自动抓取并入库)")
            sub_c = input("  请选择模式 [1-3] (默认 1): ").strip() or "1"
            limit_str = input("  限制处理条数 (0 为全部, 默认 50): ").strip() or "50"
            setattr(args, "limit", int(limit_str))
            setattr(args, "concurrency", 3)
            setattr(args, "retry", False)
            setattr(args, "interval", 600)
            setattr(args, "apply", sub_c == "1")
            setattr(args, "run", sub_c == "1")
            setattr(args, "watch", sub_c == "3")
            run_fetch_sizes(args)

        elif choice == "5":
            site_filter = input("  请输入待处理站点域名过滤 (默认 seju.life): ").strip() or "seju.life"
            confirm_run = confirm_action("是否立即启动 Playwright 正式执行抓取写入？", default=False)
            setattr(args, "site", site_filter)
            setattr(args, "limit", 0)
            setattr(args, "run", confirm_run)
            run_fetch_empty_links(args)

        else:
            print_warning("无效的序号。")

        pause_for_user()


def main():
    parser = argparse.ArgumentParser(
        description="数据清洗与元数据修复工具合集 (链接噪音清洗 / 域名替换 / 数据库升级 / 磁力大小补全 / 空链接回填)"
    )
    subparsers = parser.add_subparsers(dest="command", help="可用的子命令")

    # clean-noise
    p_noise = subparsers.add_parser("clean-noise", help="清理 resource_link 残留标签行与推广噪音")
    p_noise.add_argument("--run", action="store_true", default=False, help="正式执行写入（默认预览）")
    p_noise.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_noise.add_argument("--sync-supabase", action="store_true", default=False, help="同步更新 Supabase")
    p_noise.add_argument("--sync-dynamodb", action="store_true", default=False, help="同步更新 DynamoDB")
    p_noise.add_argument("--export-csv", action="store_true", default=False, help="导出清洗对照 CSV 审计表")
    p_noise.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_noise.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_noise.set_defaults(func=run_clean_noise)

    # replace-domain
    p_rep = subparsers.add_parser("replace-domain", help="批量替换 URL 域名或子串")
    p_rep.add_argument("--old", type=str, default="dyh.393659.xyz", help="待替换的旧域名/子串")
    p_rep.add_argument("--new", type=str, default="dtn.628563.xyz", help="替换后的新域名/子串")
    p_rep.add_argument("--run", action="store_true", default=False, help="正式执行替换（默认预览）")
    p_rep.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_rep.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_rep.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_rep.set_defaults(func=run_replace_domain)

    # upgrade-db
    p_up = subparsers.add_parser("upgrade-db", help="升级数据库结构并解析填充元数据 (size/format/pikpak)")
    p_up.add_argument("--run", action="store_true", default=False, help="正式执行表结构升级与元数据提取")
    p_up.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_up.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_up.add_argument("--db", type=str, default=None, help="指定单一数据库文件路径")
    p_up.set_defaults(func=run_upgrade_db)

    # fetch-sizes
    p_sizes = subparsers.add_parser("fetch-sizes", help="通过 Darklyn API 批量补全磁力文件大小及看门狗")
    p_sizes.add_argument("--limit", type=int, default=0, help="限制处理条数 (0 表示全部)")
    p_sizes.add_argument("--concurrency", type=int, default=3, help="并发抓取线程数 (默认 3)")
    p_sizes.add_argument("--retry", action="store_true", help="重试所有未命中的条目")
    p_sizes.add_argument("--apply", action="store_true", help="将结果写回数据库")
    p_sizes.add_argument("--run", action="store_true", default=False, help="执行抓取并写回数据库")
    p_sizes.add_argument("--watch", action="store_true", help="守护模式: 探测服务恢复后自动重试并写库")
    p_sizes.add_argument("--interval", type=int, default=600, help="守护模式探测间隔(秒, 默认 600)")
    p_sizes.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_sizes.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_sizes.set_defaults(func=run_fetch_sizes)

    # fetch-empty-links
    p_empty = subparsers.add_parser("fetch-empty-links", help="使用 Playwright 重新抓取页面回填空链接")
    p_empty.add_argument("--site", type=str, default="seju.life", help="目标站点域名过滤")
    p_empty.add_argument("--limit", type=int, default=0, help="限制抓取条数 (0 为全部)")
    p_empty.add_argument("--run", action="store_true", default=False, help="正式启动浏览器抓取并写入数据库（默认预览）")
    p_empty.add_argument("--dry-run", action="store_true", default=False, help="显式指定预览模式")
    p_empty.add_argument("--yes", "-y", action="store_true", default=False, help="跳过确认提示直接执行")
    p_empty.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_empty.set_defaults(func=run_fetch_empty_links)

    args = parser.parse_args()

    if args.command is None:
        interactive_menu()
    else:
        args.func(args)


if __name__ == "__main__":
    main()

