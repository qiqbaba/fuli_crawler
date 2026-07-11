"""
将本地数据库中的 url 和 resource_link（磁力链接）字段导出到单独的 db 文件中，保存在 D 盘根目录
"""
import sqlite3
import os
from datetime import datetime

# 源数据库路径
SRC_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_data.db")
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