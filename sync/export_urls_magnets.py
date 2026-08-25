"""本地数据库 URL 与磁力链接轻量化独立导出工具 (sync/export_urls_magnets.py)

主要用途与核心功能：
1. 轻量化独立库导出:
   - 从项目本地主 SQLite 数据库 (resources 表) 中提取全部有效的 url 记录与 resource_link (磁力链接) 记录。
   - 在 D 盘根目录生成结构精炼、体积小巧的独立 SQLite 数据库文件 (D:\\urls_only.db)。

2. 独立表结构与索引优化:
   - 表 1 [urls]: 包含 (id INTEGER PRIMARY KEY, url TEXT, exported_at TEXT)，并在 url 字段上建立 idx_url 索引。
   - 表 2 [magnets]: 包含 (id INTEGER PRIMARY KEY, resource_link TEXT, exported_at TEXT)，并在 resource_link 字段上建立 idx_resource_link 索引。

3. 批量高效写入与完整性校验:
   - 采用 500 条/批次事务批量插入，附加当前导出时间戳。
   - 导出结束后自动查询并汇报 urls 表与 magnets 表的实际写入总数。

4. 适用场景:
   - 用于向外部工具、第三方下载器或团队成员分发轻量级纯链接库，无需携带完整的 PDF 元数据与大体积主数据库。

用法:
  python sync/export_urls_magnets.py
"""
import sqlite3
import os
from datetime import datetime

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_db_path

# 源数据库路径
SRC_DB = get_db_path()
# 目标数据库路径（D 盘根目录）
DST_DB = r"D:\urls_only.db"


def export_urls():
    if not os.path.exists(SRC_DB):
        print(f"[-] 源数据库不存在: {SRC_DB}")
        return

    print(f"[*] 源数据库: {SRC_DB}")
    print(f"[*] 目标数据库: {DST_DB}")

    # 连接源数据库
    src_conn = sqlite3.connect(SRC_DB)
    src_cursor = src_conn.cursor()

    # 查询所有 url（排除 NULL 和空字符串）
    src_cursor.execute("SELECT url FROM resources WHERE url IS NOT NULL AND url != ''")
    rows = src_cursor.fetchall()
    total = len(rows)
    print(f"[+] 共读取到 {total} 条 url 记录")

    # 创建目标数据库
    if os.path.exists(DST_DB):
        os.remove(DST_DB)
        print("[*] 已删除旧的目标数据库")

    dst_conn = sqlite3.connect(DST_DB)
    dst_cursor = dst_conn.cursor()

    # 建表 - urls 表
    dst_cursor.execute("""
        CREATE TABLE IF NOT EXISTS urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            exported_at TEXT NOT NULL
        )
    """)
    dst_cursor.execute("CREATE INDEX IF NOT EXISTS idx_url ON urls(url)")

    # 批量插入 urls
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    batch_size = 500
    inserted = 0

    for i in range(0, total, batch_size):
        batch = rows[i:i + batch_size]
        dst_cursor.executemany(
            "INSERT INTO urls (url, exported_at) VALUES (?, ?)",
            [(row[0], now) for row in batch]
        )
        inserted += len(batch)
        print(f"[*] url 已写入 {inserted}/{total} 条...")

    dst_conn.commit()

    # ====== 导出 resource_link（磁力链接） ======
    src_cursor.execute(
        "SELECT resource_link FROM resources WHERE resource_link IS NOT NULL AND resource_link != ''"
    )
    magnet_rows = src_cursor.fetchall()
    magnet_total = len(magnet_rows)
    print(f"\n[+] 共读取到 {magnet_total} 条 resource_link（磁力链接）记录")

    # 建表 - magnets 表
    dst_cursor.execute("""
        CREATE TABLE IF NOT EXISTS magnets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_link TEXT NOT NULL,
            exported_at TEXT NOT NULL
        )
    """)
    dst_cursor.execute("CREATE INDEX IF NOT EXISTS idx_resource_link ON magnets(resource_link)")

    # 批量插入 magnets
    magnet_inserted = 0
    for i in range(0, magnet_total, batch_size):
        batch = magnet_rows[i:i + batch_size]
        dst_cursor.executemany(
            "INSERT INTO magnets (resource_link, exported_at) VALUES (?, ?)",
            [(row[0], now) for row in batch]
        )
        magnet_inserted += len(batch)
        print(f"[*] 磁力链接已写入 {magnet_inserted}/{magnet_total} 条...")

    dst_conn.commit()
    dst_conn.close()
    src_conn.close()

    # 验证
    verify_conn = sqlite3.connect(DST_DB)
    verify_cursor = verify_conn.cursor()
    verify_cursor.execute("SELECT COUNT(*) FROM urls")
    url_count = verify_cursor.fetchone()[0]
    verify_cursor.execute("SELECT COUNT(*) FROM magnets")
    magnet_count = verify_cursor.fetchone()[0]
    verify_conn.close()

    print(f"\n[✓] 导出完成！目标数据库: {DST_DB}")
    print(f"[✓] 共导出 {url_count} 条 url 记录")
    print(f"[✓] 共导出 {magnet_count} 条 磁力链接 记录")


if __name__ == "__main__":
    export_urls()