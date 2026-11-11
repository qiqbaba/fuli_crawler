# -*- coding: utf-8 -*-
"""运维 / 同步工具注册表。

每个工具描述一个可由工作台触发的后台任务。工具统一在子进程中运行：
- mode="module"：拼好 CLI 参数后直接调用模块的 main()（等价于 python -m）
- mode="callable"：执行 worker 内置函数（用于没有 main() 的公共库）

参数严格按各 CLI 的 argparse 定义构造；除少数无预览能力的云端工具外，
默认一律 Dry Run，只有显式打开 run 才允许写操作。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

from config import get_db_path


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    type: str = "bool"  # bool | int | text | select
    default: Any = False
    options: Tuple[str, ...] = ()
    min_value: int | None = None
    max_value: int | None = None
    help: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "options": list(self.options),
            "min": self.min_value,
            "max": self.max_value,
            "help": self.help,
        }


@dataclass(frozen=True)
class Tool:
    id: str
    group: str  # maintenance | sync
    title: str
    description: str
    mode: str = "module"
    module: str = ""
    command: Tuple[str, ...] = ()
    runner: str = ""
    params: Tuple[Param, ...] = ()
    danger: bool = False
    argv_builder: Callable[[Dict[str, Any]], List[str]] | None = None

    def to_dict(self) -> Dict[str, Any]:
        section = SECTIONS.get(self.id, "其他")
        return {
            "id": self.id,
            "group": self.group,
            "title": self.title,
            "description": self.description,
            "danger": self.danger,
            "section": section,
            "section_order": SECTION_ORDER.index(section) if section in SECTION_ORDER else 99,
            "params": [p.to_dict() for p in self.params],
        }


def _db(p: Dict[str, Any]) -> str:
    return str(p.get("db") or get_db_path())


def _run_flags(p: Dict[str, Any]) -> List[str]:
    """适用于同时支持 --run / --dry-run / --yes 的 CLI。"""
    return ["--run", "--yes"] if p.get("run") else ["--dry-run"]


def _pdf_dedup_argv(p: Dict[str, Any]) -> List[str]:
    argv = [
        "--mode", str(p.get("mode", "all")),
        "--keep", str(p.get("keep", "primary")),
        "--workers", str(p.get("workers", 8)),
        "--db", _db(p),
    ]
    argv += ["--run", "--yes"] if p.get("run") else ["--dry-run"]
    if p.get("export_csv"):
        argv.append("--export-csv")
    if p.get("trash"):
        argv.append("--trash")
    return argv


def _pdf_maint_builder(command: str) -> Callable[[Dict[str, Any]], List[str]]:
    """fixes.pdf_maintenance 各子命令的参数并不统一，按子命令分别拼装。"""
    def build(p: Dict[str, Any]) -> List[str]:
        if command == "orphan":
            return []  # 该子命令没有任何 argparse 参数
        argv = list(_run_flags(p))
        if command == "rebuild":
            argv += ["--workers", str(p.get("workers", 4)), "--db", _db(p)]
            if p.get("skip_download"):
                argv.append("--skip-download")
        elif command == "associate":
            argv += ["--db", _db(p)]
        elif command == "clean-missing":
            argv += ["--scope", str(p.get("scope", "unknown")), "--db", _db(p)]
            if p.get("export_csv"):
                argv.append("--export-csv")
        elif command in ("fix-paths", "redownload") and p.get("verbose"):
            argv.append("--verbose")
        return argv
    return build


def _cleaner_argv(p: Dict[str, Any]) -> List[str]:
    argv = _run_flags(p) + ["--db", _db(p)]
    if p.get("export_csv"):
        argv.append("--export-csv")
    return argv


def _clean_noise_argv(p: Dict[str, Any]) -> List[str]:
    """clean-noise 额外支持把清洗结果同步回 Supabase / DynamoDB。"""
    argv = _cleaner_argv(p)
    if p.get("sync_supabase"):
        argv.append("--sync-supabase")
    if p.get("sync_dynamodb"):
        argv.append("--sync-dynamodb")
    return argv


_DOMAIN_PRESETS: Dict[str, Tuple[str, str]] = {
    "大唐 (datang)": ("dyh.393659.xyz", "dtn.628563.xyz"),
    "大神 (dashen)": ("dsh.123456.xyz", "dsh.789012.xyz"),
    "精品 (jingpin)": ("jpbt1.com", "jpbt3.com"),
    "探花 (tanhua)": ("thbt1.com", "thbt8.com"),
}


def _replace_domain_argv(p: Dict[str, Any]) -> List[str]:
    """常用域名预设与旧工作台一致；选「自定义」时直接使用表单里的 old/new。"""
    preset = str(p.get("preset") or "自定义")
    old, new = _DOMAIN_PRESETS.get(
        preset, (str(p.get("old", "")), str(p.get("new", "")))
    )
    return [
        "--old", old,
        "--new", new,
        "--db", _db(p),
        *_run_flags(p),
    ]


def _fetch_sizes_argv(p: Dict[str, Any]) -> List[str]:
    # 该 CLI 没有 --dry-run；不加 --run 时函数内部即为预览
    argv = [
        "--limit", str(p.get("limit", 0)),
        "--concurrency", str(p.get("concurrency", 3)),
        "--db", _db(p),
    ]
    if p.get("retry"):
        argv.append("--retry")
    if p.get("apply"):
        argv.append("--apply")
    if p.get("run"):
        argv += ["--run", "--yes"]
    return argv


def _fetch_links_argv(p: Dict[str, Any]) -> List[str]:
    return [
        "--site", str(p.get("site", "")),
        "--limit", str(p.get("limit", 0)),
        "--db", _db(p),
        *_run_flags(p),
    ]


def _duplicates_argv(p: Dict[str, Any]) -> List[str]:
    argv = [
        "--field", str(p.get("field", "all")),
        "--keep", str(p.get("keep", "newest")),
        "--db", _db(p),
        *_run_flags(p),
    ]
    if p.get("only_no_pdf"):
        argv.append("--only-no-pdf")
    if p.get("only_http"):
        argv.append("--only-http")
    if p.get("export_db"):
        argv.append("--export-db")
    if p.get("export_csv"):
        argv.append("--export-csv")
    return argv


def _fanhao_argv(p: Dict[str, Any]) -> List[str]:
    argv = ["--mode", str(p.get("mode", "scan")), "--db", _db(p), *_run_flags(p)]
    if p.get("output"):
        argv += ["--output", str(p["output"])]
    if p.get("export_csv"):
        argv.append("--export-csv")
    return argv


def _cloud_stats_argv(p: Dict[str, Any]) -> List[str]:
    argv = []
    for flag, key in (("--supabase", "supabase"), ("--r2", "r2"), ("--dynamodb", "dynamodb")):
        if p.get(key):
            argv.append(flag)
    if not argv:
        argv = ["--supabase", "--r2", "--dynamodb"]
    year = str(p.get("year") or "").strip()
    if year:
        argv += ["--year", year]
    return argv


def _supabase_sync_argv(p: Dict[str, Any]) -> List[str]:
    argv = ["--db", _db(p)]
    if not p.get("backup"):
        argv.append("--no-backup")
    if p.get("delete_cloud"):
        argv.append("--delete-cloud")
    return argv


def _supabase_clean_argv(p: Dict[str, Any]) -> List[str]:
    # 该 CLI 默认即执行，只有 --dry-run 才预览，没有 --run 参数
    argv = ["--clean-only", "--limit", str(p.get("limit", 0)), "--db", _db(p)]
    if not p.get("run"):
        argv.append("--dry-run")
    return argv


def _ddb_sync_keys_argv(p: Dict[str, Any]) -> List[str]:
    argv = [
        "--db", _db(p),
        "--limit", str(p.get("limit", 0)),
        "--workers", str(p.get("workers", 10)),
    ]
    if p.get("qps"):
        argv += ["--qps", str(p["qps"])]
    if p.get("sources"):
        argv += ["--sources", *str(p["sources"]).split()]
    return argv


def _r2_builder(delete_only: bool) -> Callable[[Dict[str, Any]], List[str]]:
    def build(p: Dict[str, Any]) -> List[str]:
        argv: List[str] = []
        year = str(p.get("year") or "").strip()
        if year:
            argv += ["--year", year]
        if p.get("max_files"):
            argv += ["--max", str(p["max_files"])]
        if p.get("workers"):
            argv += ["--workers", str(p["workers"])]
        if delete_only:
            argv += ["--delete-only", "--dry-run"]
            prefix = str(p.get("prefix") or "").strip()
            if prefix:
                argv += ["--delete-prefix", prefix]
            local_dir = str(p.get("local_dir") or "").strip()
            if local_dir:
                argv += ["--output", local_dir]
        else:
            output = str(p.get("output") or "").strip()
            if output:
                argv += ["--output", output]
            if p.get("resume") is False:
                argv.append("--no-resume")
            # 对齐旧版「下载完成后安全清理云端」与「本地无副本对象」策略：
            # 子进程把 input() 固定应答为 n，故只有显式 delete_missing 才会
            # 通过 --delete-force 让底层跳过询问、连同无副本对象一并删除。
            if p.get("delete_after"):
                argv.append("--delete")
                if p.get("delete_missing"):
                    argv.append("--delete-force")
        return argv
    return build


def _r2_clean_argv(p: Dict[str, Any]) -> List[str]:
    """R2 清理第二阶段：仅在显式 run 时去掉 --dry-run。

    不带 --delete-force 时底层会跳过「本地无副本」的对象，只删除本地已有
    副本的云端文件；勾选 delete_missing 才会连无副本对象一并删除。
    """
    prefix = str(p.get("prefix") or "").strip() or "pdf/"
    argv = ["--delete-only", "--delete-prefix", prefix]
    local_dir = str(p.get("local_dir") or "").strip()
    if local_dir:
        argv += ["--output", local_dir]
    if p.get("delete_missing"):
        argv.append("--delete-force")
    if not p.get("run"):
        argv.append("--dry-run")
    return argv


_TOOL_LIST: Tuple[Tool, ...] = (
    # ---------------- 系统 / 任务通道 ----------------
    Tool(
        id="system-health", group="maintenance", title="系统自检",
        description="只读检查数据库、PDF 目录与云端配置状态。",
        mode="callable", runner="system_health",
        params=(Param("detail", "输出详细路径信息", "bool", True),),
    ),
    Tool(
        id="channel-probe", group="maintenance", title="任务通道压测",
        description="按固定间隔流式输出日志，用于验证 SSE 实时推送、任务取消与并发上限。",
        mode="callable", runner="channel_probe",
        params=(
            Param("lines", "输出行数", "int", 20, min_value=1, max_value=200),
            Param("interval_ms", "每行间隔（毫秒）", "int", 500, min_value=100, max_value=5000),
        ),
    ),
    # ---------------- PDF 维护 ----------------
    Tool(
        id="pdf-dedup", group="maintenance", title="PDF 多维查重去重",
        description="扫描本地 PDF 并生成查重计划；默认仅预览。",
        module="fixes.pdf_dedup", argv_builder=_pdf_dedup_argv, danger=True,
        params=(
            Param("mode", "查重模式", "select", "all", ("all", "hash", "name", "title", "db")),
            Param("keep", "保留策略", "select", "primary", ("primary", "larger", "newest", "oldest")),
            Param("workers", "并发数", "int", 8, min_value=1, max_value=32),
            Param("export_csv", "导出 CSV 审计表", "bool", False),
            Param("trash", "移入回收目录而非直接删除", "bool", True),
            Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    Tool(
        id="check-dates", group="maintenance", title="日期比对审计",
        description="扫描 PDF 文件名与数据库发布日期的差异，只读操作。",
        module="fixes.pdf_maintenance", command=("check-dates",),
        argv_builder=lambda p: [],
    ),
    Tool(
        id="fix-paths", group="maintenance", title="PDF 路径与文件名纠偏",
        description="修正 Unknown_Year 归档与文件名日期不匹配，默认仅预览。",
        module="fixes.pdf_maintenance", command=("fix-paths",),
        argv_builder=_pdf_maint_builder("fix-paths"), danger=True,
        params=(
            Param("verbose", "输出逐文件分析计划", "bool", False),
            Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    Tool(
        id="redownload-small", group="maintenance", title="损坏小 PDF 重抓",
        description="重新抓取体积小于 20KB 的可疑 PDF，涉及网络，默认仅预览。",
        module="fixes.pdf_maintenance", command=("redownload",),
        argv_builder=_pdf_maint_builder("redownload"), danger=True,
        params=(
            Param("verbose", "详细输出", "bool", False),
            Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    Tool(
        id="rebuild-pdfs", group="maintenance", title="缺失 PDF 并发重建",
        description="重新下载物理缺失的 PDF 并相对化路径，默认仅预览。",
        module="fixes.pdf_maintenance", command=("rebuild",),
        argv_builder=_pdf_maint_builder("rebuild"), danger=True,
        params=(
            Param("workers", "并发下载数", "int", 4, min_value=1, max_value=20),
            Param("skip_download", "仅纠偏路径、不重新下载", "bool", False),
            Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    Tool(
        id="orphan-pdfs", group="maintenance", title="孤立 PDF 隔离/归位",
        description="扫描年份目录中数据库无记录的 PDF 并隔离到根目录，或把根目录孤儿归位到年份目录；默认仅预览。",
        mode="callable", runner="orphan_manage", danger=True,
        params=(
            Param("mode", "操作模式", "select", "隔离到根目录",
                  ("隔离到根目录", "归位到年份目录"), help="隔离=把无记录 PDF 移到 PDF 根目录，归位=把根目录孤儿移回年份目录"),
            Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    Tool(
        id="associate-pdfs", group="maintenance", title="磁盘未关联 PDF 回填",
        description="按标题与站点智能关联磁盘 PDF 并回填数据库，默认仅预览。",
        module="fixes.pdf_maintenance", command=("associate",),
        argv_builder=_pdf_maint_builder("associate"), danger=True,
        params=(Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),),
    ),
    Tool(
        id="clean-missing", group="maintenance", title="清理失效死链记录",
        description="清理物理 PDF 已不存在的脏记录，默认仅预览。",
        module="fixes.pdf_maintenance", command=("clean-missing",),
        argv_builder=_pdf_maint_builder("clean-missing"), danger=True,
        params=(
            Param("scope", "扫描范围", "select", "unknown", ("unknown", "all")),
            Param("export_csv", "导出 CSV 审计表", "bool", False),
            Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    # ---------------- 数据清洗 ----------------
    Tool(
        id="clean-noise", group="maintenance", title="数据噪声清洗",
        description="清理标题与链接中的噪声数据，默认仅预览。",
        module="fixes.data_cleaner", command=("clean-noise",), argv_builder=_clean_noise_argv, danger=True,
        params=(
            Param("export_csv", "导出 CSV 审计表", "bool", True),
            Param("sync_supabase", "同步更新至 Supabase", "bool", False),
            Param("sync_dynamodb", "同步更新至 DynamoDB", "bool", False),
            Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    Tool(
        id="replace-domain", group="maintenance", title="批量域名替换",
        description="替换 URL 中的旧域名或子串，默认仅预览。",
        module="fixes.data_cleaner", command=("replace-domain",),
        argv_builder=_replace_domain_argv, danger=True,
        params=(
            Param("preset", "常用预设快捷填入", "select", "自定义",
                  ("自定义", "大唐 (datang)", "大神 (dashen)", "精品 (jingpin)", "探花 (tanhua)"),
                  help="选择预设后自动填入下方域名；选「自定义」则使用手动输入的值"),
            Param("old", "旧域名 / 子串", "text", "dyh.393659.xyz"),
            Param("new", "新域名 / 子串", "text", "dtn.628563.xyz"),
            Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    Tool(
        id="upgrade-db", group="maintenance", title="表结构升级与元数据回填",
        description="检查表结构缺失并回填元数据，默认仅预览。",
        module="fixes.data_cleaner", command=("upgrade-db",), argv_builder=_cleaner_argv, danger=True,
        params=(Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),),
    ),
    Tool(
        id="fetch-sizes", group="maintenance", title="磁力文件大小补全",
        description="通过 Darklyn API 补全大小；不加正式执行参数时为预览。",
        module="fixes.data_cleaner", command=("fetch-sizes",),
        argv_builder=_fetch_sizes_argv, danger=True,
        params=(
            Param("limit", "限制处理条数（0 为全部）", "int", 5, min_value=0, max_value=100000),
            Param("concurrency", "并发抓取数", "int", 3, min_value=1, max_value=20),
            Param("retry", "重试所有未命中条目", "bool", False),
            Param("apply", "将结果写回数据库", "bool", False),
            Param("run", "正式抓取并写库", "bool", False),
        ),
    ),
    Tool(
        id="fetch-empty-links", group="maintenance", title="空链接 Playwright 回填",
        description="启动浏览器重新抓取页面回填空链接，默认仅预览。",
        module="fixes.data_cleaner", command=("fetch-empty-links",),
        argv_builder=_fetch_links_argv, danger=True,
        params=(
            Param("site", "目标站点域名", "text", "seju.life"),
            Param("limit", "限制抓取条数（0 为全部）", "int", 5, min_value=0, max_value=100000),
            Param("run", "正式启动浏览器抓取", "bool", False),
        ),
    ),
    # ---------------- 记录过滤 ----------------
    Tool(
        id="record-duplicates", group="maintenance", title="重复记录过滤",
        description="按标题/链接等字段分组查找重复记录，默认仅预览。",
        module="fixes.record_filter", command=("duplicates",), argv_builder=_duplicates_argv, danger=True,
        params=(
            Param("field", "查重字段", "select", "pdf_path",
                  ("pdf_path", "url", "resource_link", "title_link", "all")),
            Param("keep", "保留策略", "select", "newest", ("newest", "oldest")),
            Param("only_no_pdf", "仅处理无 PDF 的重复记录", "bool", True),
            Param("only_http", "仅处理 http 链接的重复记录", "bool", False),
            Param("export_db", "导出独立 SQLite 库", "bool", True),
            Param("export_csv", "导出 CSV 审计表", "bool", False),
            Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    Tool(
        id="fanhao", group="maintenance", title="番号识别与清理",
        description="严格日本番号扫描统计、导出或清理，默认仅预览。",
        module="fixes.record_filter", command=("fanhao",), argv_builder=_fanhao_argv, danger=True,
        params=(
            Param("mode", "操作模式", "select", "scan", ("scan", "export", "delete")),
            Param("output", "导出文件路径（留空用默认）", "text", ""),
            Param("export_csv", "导出为 CSV 审计表", "bool", False),
            Param("run", "正式执行（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    # ---------------- 缓存与数据库 ----------------
    Tool(
        id="warmup-cache", group="maintenance", title="PDF 封面缓存预热",
        description="并发渲染 PDF 页图写入缩略图缓存；条数填 0 表示全量预热，已缓存的文档自动跳过。",
        mode="callable", runner="warmup_cache",
        params=(
            Param("limit", "处理记录数上限（0 为全量）", "int", 50, min_value=0, max_value=1000000),
            Param("workers", "并发渲染数", "int", 8, min_value=1, max_value=32),
            Param("dpi", "渲染 DPI", "int", 105, min_value=30, max_value=200),
            Param("quality", "JPEG 质量", "int", 75, min_value=30, max_value=95),
        ),
    ),
    Tool(
        id="db-backup", group="maintenance", title="数据库备份",
        description="生成当前 SQLite 数据库的 .bak 备份文件。",
        mode="callable", runner="db_backup",
        params=(Param("tag", "备份标签", "text", "web"),),
    ),
    Tool(
        id="db-vacuum", group="maintenance", title="数据库 VACUUM 压缩",
        description="压缩数据库文件、回收空间；大库可能耗时较长。",
        mode="callable", runner="db_vacuum", danger=True,
        params=(),
    ),
    # ---------------- 云端同步 ----------------
    Tool(
        id="cloud-connectivity", group="sync", title="云端连接诊断",
        description="只读探测 Supabase / R2 / DynamoDB 连通性并输出延迟与数据摘要；不写入云端数据、不显示凭据。",
        mode="callable", runner="cloud_probe",
        params=(
            Param("supabase", "探测 Supabase", "bool", True),
            Param("r2", "探测 Cloudflare R2", "bool", True),
            Param("dynamodb", "探测 DynamoDB", "bool", True),
            Param("r2_bucket", "R2 bucket 覆盖值（留空用项目配置）", "text", ""),
        ),
    ),
    Tool(
        id="cloud-stats", group="sync", title="多云用量监控",
        description="汇总 Supabase / R2 / DynamoDB 的存储与配额水位（需云端凭据）。",
        module="sync.stats", argv_builder=_cloud_stats_argv,
        params=(
            Param("supabase", "查询 Supabase", "bool", True),
            Param("r2", "查询 Cloudflare R2", "bool", True),
            Param("dynamodb", "查询 DynamoDB", "bool", True),
            Param("year", "R2 统计年份（留空为全部）", "text", ""),
        ),
    ),
    Tool(
        id="supabase-sync", group="sync", title="Supabase 云端归档同步",
        description="拉取云端记录并幂等合并进本地库；默认执行前先备份本地库。",
        module="sync.supabase_sync", argv_builder=_supabase_sync_argv, danger=True,
        params=(
            Param("db", "本地 SQLite 路径（留空用主库）", "text", ""),
            Param("backup", "同步前备份本地数据库", "bool", True),
            Param("delete_cloud", "同步后清理云端已同步记录", "bool", False),
        ),
    ),
    Tool(
        id="supabase-clean", group="sync", title="Supabase 已同步数据清理",
        description="校验并清理云端已同步记录，默认仅预览。",
        module="sync.supabase_sync", argv_builder=_supabase_clean_argv, danger=True,
        params=(
            Param("db", "本地比对库路径（留空用主库）", "text", ""),
            Param("limit", "限制处理条数（0 为不限）", "int", 0, min_value=0, max_value=1000000),
            Param("run", "正式清理（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    Tool(
        id="r2-download", group="sync", title="R2 PDF 并发下载",
        description="从 Cloudflare R2 下载 PDF 到本地；默认限量处理，不触发云端删除。",
        module="sync.r2_sync", argv_builder=_r2_builder(False), danger=True,
        params=(
            Param("year", "仅处理年份（留空为全部）", "text", ""),
            Param("max_files", "最多下载文件数（0 为不限）", "int", 10, min_value=0, max_value=100000),
            Param("workers", "并发下载数", "int", 8, min_value=1, max_value=50),
            Param("output", "本地保存目录（留空用 PDF 根目录）", "text", ""),
            Param("resume", "启用断点续传", "bool", True),
            Param("delete_after", "下载完成后清理云端已有本地副本的文件", "bool", False),
            Param("delete_missing", "上述清理连本地无副本对象一并删除（高风险）", "bool", False,
                  help="仅在勾选「下载完成后清理」时生效；不勾选时底层安全跳过本地无副本的云端孤本"),
        ),
    ),
    Tool(
        id="r2-delete-preview", group="sync", title="R2 云端清理预览",
        description="以 --delete-only --dry-run 列出云端待删除对象，绝不实际删除。",
        module="sync.r2_sync", argv_builder=_r2_builder(True),
        params=(
            Param("year", "仅处理年份（留空为全部）", "text", ""),
            Param("prefix", "对象前缀（如 pdf/2025/）", "text", ""),
            Param("local_dir", "本地比对目录（留空用 PDF 根目录）", "text", ""),
        ),
    ),
    Tool(
        id="r2-clean", group="sync", title="R2 云端清理执行",
        description="先跑「R2 云端清理预览」核对候选对象，再在此正式执行；默认只删除本地已有副本的文件。",
        module="sync.r2_sync", argv_builder=_r2_clean_argv, danger=True,
        params=(
            Param("prefix", "待清理前缀（如 pdf/ 或 pdf/2025/）", "text", "pdf/"),
            Param("local_dir", "本地比对目录（留空用 PDF 根目录）", "text", ""),
            Param("delete_missing", "连本地无副本的对象一并删除（高风险）", "bool", False),
            Param("run", "正式删除（关闭则为 Dry Run）", "bool", False),
        ),
    ),
    Tool(
        id="ddb-upload", group="sync", title="DynamoDB 增量上传",
        description="比对本地库与 DynamoDB，增量上传缺失的 URL 与磁力，会写云端。",
        module="sync.dynamodb_sync", command=("upload",),
        argv_builder=lambda p: ["--db", _db(p)], danger=True,
        params=(
            Param("db", "本地 SQLite 路径（留空用主库）", "text", ""),
        ),
    ),
    Tool(
        id="ddb-sync-keys", group="sync", title="DynamoDB 去重键同步",
        description="生成规范化去重键并写入 DynamoDB / Bloom Filter，会写云端。",
        module="sync.dynamodb_sync", command=("sync-keys",), argv_builder=_ddb_sync_keys_argv, danger=True,
        params=(
            Param("db", "本地 SQLite 路径（留空用主库）", "text", ""),
            Param("limit", "限制处理条数（0 为不限）", "int", 0, min_value=0, max_value=1000000),
            Param("workers", "并发数", "int", 10, min_value=1, max_value=30),
            Param("qps", "写入限速 QPS（0 为不限）", "int", 24, min_value=0, max_value=200),
            Param("sources", "站点来源过滤（空格分隔）", "text", ""),
        ),
    ),
    Tool(
        id="export-links", group="sync", title="纯链接独立库导出",
        description="抽取有效 URL 与磁力链接，生成带索引的独立轻量 SQLite 库；拒绝覆盖已有文件。",
        mode="callable", runner="export_links", danger=True,
        params=(
            Param("src_path", "源 SQLite 主库路径（留空用主库）", "text", ""),
            Param("dst_path", "目标库路径（留空用 cache/urls_only.db）", "text", ""),
            Param("limit", "每条管道导出上限（0 为全部）", "int", 0, min_value=0, max_value=1000000),
            Param("overwrite", "允许覆盖已存在的目标文件", "bool", False),
        ),
    ),
)

# 分组与旧工作台的子页签 / 卡片面板一一对应，便于迁移后按相同心智模型查找工具。
SECTION_ORDER: Tuple[str, ...] = (
    "PDF 维护与重建",
    "PDF 多维查重去重",
    "数据清洗与元数据",
    "记录过滤与番号分离",
    "缓存预热与系统运维",
    "多云用量监控",
    "Supabase 归档",
    "R2 PDF 管理",
    "DynamoDB 去重",
    "纯链接独立导出",
)

SECTIONS: Dict[str, str] = {
    "system-health": "缓存预热与系统运维",
    "channel-probe": "缓存预热与系统运维",
    "warmup-cache": "缓存预热与系统运维",
    "db-backup": "缓存预热与系统运维",
    "db-vacuum": "缓存预热与系统运维",
    "check-dates": "PDF 维护与重建",
    "fix-paths": "PDF 维护与重建",
    "redownload-small": "PDF 维护与重建",
    "rebuild-pdfs": "PDF 维护与重建",
    "orphan-pdfs": "PDF 维护与重建",
    "associate-pdfs": "PDF 维护与重建",
    "clean-missing": "PDF 维护与重建",
    "pdf-dedup": "PDF 多维查重去重",
    "clean-noise": "数据清洗与元数据",
    "replace-domain": "数据清洗与元数据",
    "upgrade-db": "数据清洗与元数据",
    "fetch-sizes": "数据清洗与元数据",
    "fetch-empty-links": "数据清洗与元数据",
    "record-duplicates": "记录过滤与番号分离",
    "fanhao": "记录过滤与番号分离",
    "cloud-connectivity": "多云用量监控",
    "cloud-stats": "多云用量监控",
    "supabase-sync": "Supabase 归档",
    "supabase-clean": "Supabase 归档",
    "r2-download": "R2 PDF 管理",
    "r2-delete-preview": "R2 PDF 管理",
    "r2-clean": "R2 PDF 管理",
    "ddb-upload": "DynamoDB 去重",
    "ddb-sync-keys": "DynamoDB 去重",
    "export-links": "纯链接独立导出",
}

TOOLS: Dict[str, Tool] = {tool.id: tool for tool in _TOOL_LIST}


def tool_catalog() -> List[Dict[str, Any]]:
    catalog = []
    for tool in _TOOL_LIST:
        spec = tool.to_dict()
        if tool.id == "replace-domain":
            # 与 argv_builder 使用的同一份预设，供前端选预设时自动填值
            spec["presets"] = {
                name: {"old": old, "new": new}
                for name, (old, new) in _DOMAIN_PRESETS.items()
            }
        catalog.append(spec)
    return catalog


def normalize_params(tool: Tool, raw: Dict[str, Any]) -> Dict[str, Any]:
    """只接受注册表声明的参数，并按声明类型强制转换，避免任意值注入 CLI。"""
    result: Dict[str, Any] = {}
    for spec in tool.params:
        value = raw.get(spec.name, spec.default)
        if spec.type == "bool":
            value = bool(value)
        elif spec.type == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = int(spec.default)
            if spec.min_value is not None:
                value = max(spec.min_value, value)
            if spec.max_value is not None:
                value = min(spec.max_value, value)
        elif spec.type == "select":
            value = str(value)
            if spec.options and value not in spec.options:
                value = str(spec.default)
        else:
            value = str(value).strip()[:200]
        result[spec.name] = value
    return result
