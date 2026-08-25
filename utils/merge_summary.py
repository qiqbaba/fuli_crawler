"""
汇总所有爬虫模块的运行摘要 (summary_*.json)，生成 GitHub Actions Step Summary Markdown 表格，
并提取熔断爬虫列表写入 stats/circuit_break_crawlers.txt。
"""
import glob
import json
import os
import sys
from typing import Any, Dict, List

# 确保项目根目录在 sys.path 中并设置控制台 utf-8 编码
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from utils import setup_console_utf8
    setup_console_utf8()
except Exception:
    pass

CRAWLER_ORDER = [
    "seju",
    "u3c3",
    "datang",
    "gcbt",
    "madou",
    "jingpin_toupai",
    "taose",
    "dashen",
    "tanhua",
    "jingpin",
    "mianfei_guochan",
]

# 板块失败原因类型 -> 展示文案
CATEGORY_REASON_LABELS = {
    "network": "网络请求失败/超时",
    "empty_parse": "解析为空/结构变更",
    "unknown": "原因未知",
}


def merge_summaries(
    stats_dir: str = "downloaded-stats",
    circuit_break_file: str = "stats/circuit_break_crawlers.txt",
    step_summary_file: str = None,
) -> None:
    # 查找 summary_*.json 文件，优先从 stats_dir 查找，若无则尝试 logs/
    summary_files = glob.glob(os.path.join(stats_dir, "summary_*.json"))
    if not summary_files and os.path.exists("logs"):
        summary_files = glob.glob(os.path.join("logs", "summary_*.json"))

    summaries: List[Dict[str, Any]] = []
    circuit_broken: List[str] = []

    for sf in summary_files:
        try:
            with open(sf, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 容错：若 json 中无 crawler 字段或为空，从文件名中提取
                if not data.get("crawler"):
                    basename = os.path.basename(sf)
                    cname = basename.replace("summary_", "").replace(".json", "")
                    data["crawler"] = cname

                summaries.append(data)
                if data.get("circuit_break"):
                    circuit_broken.append(data.get("crawler", "unknown"))
        except Exception as e:
            print(f"[!] 读取摘要文件 {sf} 失败: {e}", file=sys.stderr)

    # 按照标准爬虫顺序排序
    def get_sort_key(item: Dict[str, Any]) -> int:
        cname = item.get("crawler", "")
        if cname in CRAWLER_ORDER:
            return CRAWLER_ORDER.index(cname)
        return len(CRAWLER_ORDER) + 1

    summaries.sort(key=get_sort_key)

    # 确保输出目录存在
    os.makedirs(os.path.dirname(circuit_break_file) or ".", exist_ok=True)
    with open(circuit_break_file, "w", encoding="utf-8") as f:
        f.write(",".join(circuit_broken))

    # 生成 Markdown 汇总表格
    if not step_summary_file:
        step_summary_file = os.environ.get("GITHUB_STEP_SUMMARY")

    if summaries:
        tot_ins = sum(s.get("total_inserted", 0) for s in summaries)
        tot_skp = sum(s.get("total_skipped", 0) for s in summaries)
        tot_pgs = sum(s.get("total_crawled_pages", 0) for s in summaries)
        tot_fpgs = sum(s.get("total_failed_pages", 0) for s in summaries)

        md_lines = [
            "## 🚀 爬虫任务运行汇总\n",
            "| 爬虫模块 | 运行状态 | 入库条数 | 跳过/重复 | 成功抓取页数 | 失败/空页数 | 耗时 | 备注 / 诊断 |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
        ]

        for s in summaries:
            circuit_break = s.get("circuit_break", False)
            failed_cats = s.get("failed_categories", [])
            completed_cats = s.get("completed_categories", [])
            status_str = s.get("status", "")

            if circuit_break:
                status = "🔴 **已熔断**"
                reason = s.get("circuit_break_reason") or "网络超时或连续失败熔断"
                rsn = f"⚠️ {reason}"
            elif failed_cats or status_str == "partial_success":
                status = "🟡 **部分完成**"
                failed_reasons = s.get("failed_category_reasons", {}) or {}
                skipped_info = ", ".join(
                    f"{cat}({CATEGORY_REASON_LABELS.get(failed_reasons.get(cat, 'unknown'), '原因未知')})"
                    for cat in failed_cats
                )
                if completed_cats:
                    comp_info = ", ".join(completed_cats)
                    rsn = f"⚠️ 跳过异常板块: {skipped_info} (已完成: {comp_info})"
                else:
                    rsn = f"⚠️ 跳过异常板块: {skipped_info}"
            else:
                status = "🟢 **正常完成**"
                rsn = "正常完成"

            cname = s.get("crawler", "")
            ins = s.get("total_inserted", 0)
            skp = s.get("total_skipped", 0)
            pgs = s.get("total_crawled_pages", 0)
            fpgs = s.get("total_failed_pages", 0)
            dur = s.get("duration", "-")
            md_lines.append(f"| `{cname}` | {status} | {ins} | {skp} | {pgs} | {fpgs} | {dur} | {rsn} |")

        if len(summaries) > 1:
            md_lines.append(
                f"| **总计 ({len(summaries)} 个模块)** | - | **{tot_ins}** | **{tot_skp}** | **{tot_pgs}** | **{tot_fpgs}** | - | - |"
            )

        md_content = "\n".join(md_lines) + "\n\n"

        print("=== 爬虫任务运行汇总 ===")
        print(md_content)

        if step_summary_file:
            try:
                with open(step_summary_file, "a", encoding="utf-8") as f:
                    f.write(md_content)
                print(f"[+] 已将汇总表写入 GITHUB_STEP_SUMMARY: {step_summary_file}")
            except Exception as e:
                print(f"[!] 写入 GITHUB_STEP_SUMMARY 失败: {e}", file=sys.stderr)
    else:
        print("[*] 未找到任何爬虫运行摘要文件")


if __name__ == "__main__":
    merge_summaries()
