# -*- coding: utf-8 -*-
"""通过 magnet-metadata-api.darklyn.org 批量补全磁力链接的文件大小及服务恢复监控

用法:
  python fixes/fetch_size_darklyn.py --limit 50 --concurrency 3       # 抓取前 50 条
  python fixes/fetch_size_darklyn.py --limit 0 --concurrency 4        # 抓取全部未处理条目
  python fixes/fetch_size_darklyn.py --retry                          # 重试所有未命中的条目
  python fixes/fetch_size_darklyn.py --apply                          # 把已获取结果写回数据库
  python fixes/fetch_size_darklyn.py --watch                          # 看门狗守护模式：探测服务恢复后自动重试并写库
  python fixes/fetch_size_darklyn.py --watch --interval 300           # 守护模式(自定义探测间隔，单位:秒)
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fixes.db_utils import setup_fixes_module, get_connection, get_db_path

setup_fixes_module()

from utils import setup_console_utf8  # noqa: E402

API = "https://magnet-metadata-api.darklyn.org/api/v1/metadata"
FAKE_NAME = "001FF871FB8B9348FCD8ECBFDAACC85D3CCAB00F.exe"  # 服务端缓存BUG的假种子
FAKE_SIZE = 877597184

RESULT_FILE = os.path.join(PROJECT_ROOT, "result_darklyn.json")
LOG_FILE = os.path.join(PROJECT_ROOT, "darklyn_progress.log")

_lock = threading.Lock()
_results: Dict[str, dict] = {}
_done = 0
_total = 0


def log(line: str, prefix: str = "") -> None:
    """写入日志文件并输出到控制台"""
    tag = f" [{prefix}]" if prefix else ""
    log_line = f"{time.strftime('%H:%M:%S')}{tag} {line}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_line)
            f.flush()
    except Exception as e:
        print(f"[-] 写入日志失败: {e}", file=sys.stderr)
    print(log_line.strip())


def load_empty_hashes(db_path: str, limit: int = 0) -> List[str]:
    """从数据库中读取缺少 size 的磁力链接 BTIH hash"""
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


def load_existing() -> Dict[str, dict]:
    """加载已保存的结果缓存"""
    if os.path.exists(RESULT_FILE):
        try:
            with open(RESULT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"读取现有结果文件失败: {e}", prefix="warn")
    return {}


def save_results() -> None:
    """持久化保存当前结果"""
    with _lock:
        try:
            with open(RESULT_FILE, "w", encoding="utf-8") as f:
                json.dump(_results, f, ensure_ascii=False, indent=1)
        except Exception as e:
            log(f"保存结果失败: {e}", prefix="error")


def query(h: str, timeout: int = 40) -> Tuple[Optional[int], str]:
    """查询单条 hash 的元数据"""
    import requests
    try:
        r = requests.post(API, json={"magnet_uri": "magnet:?xt=urn:btih:" + h}, timeout=timeout)
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


def _worker(h: str) -> None:
    """多线程单个任务处理"""
    global _done
    t0 = time.time()
    size, status = query(h)
    if size is None and status == "network":
        time.sleep(3)
        size, status = query(h)
    with _lock:
        _results[h] = {"size": size, "status": status}
        _done += 1
        n = _done
    elapsed = time.time() - t0
    if size:
        log(f"[{n}/{_total}] {h[:12]} OK {size} ({elapsed:.0f}s)")
    else:
        log(f"[{n}/{_total}] {h[:12]} {status} ({elapsed:.0f}s)")
    if n % 20 == 0:
        save_results()


def run_fetch(db_path: str, limit: int = 0, concurrency: int = 3, retry: bool = False) -> int:
    """执行批量抓取逻辑"""
    global _total, _done
    existing = load_existing()
    _results.update(existing)
    hashes = load_empty_hashes(db_path, limit)

    if retry:
        todo = [h for h in hashes if h not in _results or not _results.get(h, {}).get("size")]
    else:
        todo = [h for h in hashes if h not in _results]

    _total = len(todo)
    _done = 0
    print(f"待处理 {_total} 条（已跳过 {len(hashes) - _total} 条）")
    log(f"开始: 待处理 {_total} 条, 并发 {concurrency}")

    if not todo:
        ok = sum(1 for v in _results.values() if v.get("size"))
        print(f"\n无需抓取: 已命中 {ok} / {len(_results)} ({(100.0 * ok / max(len(_results), 1)):.1f}%)")
        return ok

    t_start = time.time()
    last_report = 0
    idx = 0
    while idx < len(todo):
        batch = todo[idx : idx + concurrency]
        idx += concurrency
        threads = [threading.Thread(target=_worker, args=(h,), daemon=True) for h in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if _done - last_report >= 50:
            last_report = _done
            rate = _done / max(time.time() - t_start, 1)
            eta = (_total - _done) / max(rate, 1e-9)
            log(f"进度: {_done}/{_total} ({100.0 * _done / _total:.0f}%) 速率 {rate:.2f}条/秒 ETA {eta / 60:.0f} 分钟")

    save_results()
    ok = sum(1 for v in _results.values() if v.get("size"))
    print(f"\n完成: 命中 {ok} / {len(_results)} ({(100.0 * ok / max(len(_results), 1)):.1f}%)")
    return ok


def apply_to_db(db_path: str) -> int:
    """将抓取到的 size 写回 SQLite 数据库"""
    existing = load_existing()
    if not existing:
        print("[-] 未找到有效的结果文件或结果为空，取消写库。")
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
        print(f"[+] 已更新数据库 {len(updates)} 行")
        log(f"已写回数据库 {len(updates)} 条记录", prefix="db")
    else:
        print("[*] 数据库中没有需要更新的记录")
    conn.close()
    return len(updates)


def probe(h: str) -> Tuple[object, Optional[int]]:
    """探测单条 hash 是否能得到有效非假响应"""
    import requests
    try:
        r = requests.post(API, json={"magnet_uri": "magnet:?xt=urn:btih:" + h}, timeout=20)
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


def is_recovered(failed_hashes: List[str], probe_count: int = 3) -> bool:
    """判断服务是否已恢复（通过测试若干未命中的 hash）"""
    ok_count = 0
    test_sample = failed_hashes[:probe_count]
    for h in test_sample:
        code, size = probe(h)
        if code == 200 and size and size != FAKE_SIZE:
            ok_count += 1  # 真实解析成功 → 恢复
        elif code == 500:
            ok_count += 1  # 走真实 DHT 路径但超时 → 也视为服务缓存已清除/恢复
    # 只要有多数测试样本有效，即判定为恢复
    threshold = max(1, len(test_sample) // 2 + 1)
    return ok_count >= threshold


def run_watchdog(db_path: str, concurrency: int = 3, interval: int = 600, limit: int = 0) -> None:
    """看门狗守护模式：周期性探测并在服务恢复后自动抓取和入库"""
    log("看门狗启动，准备检查待处理/失败列表...", prefix="watchdog")
    d = load_existing()
    failed = [h for h, v in d.items() if not v.get("size")]
    if not failed:
        all_empty = load_empty_hashes(db_path, limit)
        failed = [h for h in all_empty if h not in d or not d.get(h, {}).get("size")]

    log(f"看门狗就绪: {len(failed)} 条待重试, 每 {interval} 秒探测一次", prefix="watchdog")

    while True:
        try:
            if is_recovered(failed):
                log(f"检测到服务已恢复, 开始执行抓取 (并发 {concurrency}) ...", prefix="watchdog")
                run_fetch(db_path=db_path, limit=limit, concurrency=concurrency, retry=True)
                log("抓取完成，开始写回数据库 ...", prefix="watchdog")
                apply_to_db(db_path=db_path)
                log("写库完成, 看门狗任务结束退出", prefix="watchdog")
                return
            log(f"服务未恢复(返回假种子或不可达), 将在 {interval} 秒后再次探测", prefix="watchdog")
            time.sleep(interval)
        except KeyboardInterrupt:
            log("收到中断信号，看门狗已安全退出", prefix="watchdog")
            break
        except Exception as e:
            log(f"探测循环异常: {e}，将在 {interval} 秒后重试", prefix="watchdog")
            time.sleep(interval)


def main():
    setup_console_utf8()
    ap = argparse.ArgumentParser(description="批量补全磁力链接文件大小及 Darklyn 恢复看门狗")
    ap.add_argument("--limit", type=int, default=0, help="限制处理条数 (0 表示全部)")
    ap.add_argument("--concurrency", type=int, default=3, help="并发抓取线程数 (默认 3)")
    ap.add_argument("--retry", action="store_true", help="重试所有未命中的条目")
    ap.add_argument("--apply", action="store_true", help="将结果写回 all_data.db 数据库")
    ap.add_argument("--watch", action="store_true", help="守护模式: 探测服务恢复后自动重试并写库")
    ap.add_argument("--interval", type=int, default=600, help="守护模式下探测间隔(秒, 默认 600)")
    args = ap.parse_args()

    db_path = get_db_path()

    if args.watch:
        run_watchdog(db_path=db_path, concurrency=args.concurrency, interval=args.interval, limit=args.limit)
    elif args.apply and not args.retry and args.limit == 0:
        apply_to_db(db_path=db_path)
    else:
        run_fetch(db_path=db_path, limit=args.limit, concurrency=args.concurrency, retry=args.retry)
        if args.apply:
            apply_to_db(db_path=db_path)


if __name__ == "__main__":
    main()
