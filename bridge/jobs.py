# -*- coding: utf-8 -*-
"""后台任务管理与 SSE 日志流。

每个任务在独立子进程中运行 bridge.worker，父进程逐行读取合并后的
stdout/stderr 写入内存环形缓冲；前端通过 SSE 增量消费日志。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import PROJECT_ROOT

from .metrics import extract_metrics
from .registry import TOOLS, normalize_params

MAX_CONCURRENT = 2
MAX_LOG_LINES = 4000
MAX_JOBS = 100
SSE_TIMEOUT_SECONDS = 1800
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


@dataclass
class Job:
    id: str
    tool_id: str
    title: str
    params: Dict[str, Any]
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    status: str = "pending"
    exit_code: Optional[int] = None
    error: str = ""
    logs: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    seq: int = 0
    proc: Optional[subprocess.Popen] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    metrics: Optional[List[Dict[str, Any]]] = None
    ack: bool = False

    def append(self, line: str) -> None:
        with self.lock:
            self.logs.append(line)
            self.seq += 1

    def snapshot(self, cursor: int = 0) -> Tuple[List[str], int]:
        """返回序号大于 cursor 的新日志，以及当前最新序号。"""
        with self.lock:
            all_logs = list(self.logs)
            seq = self.seq
        oldest_seq = seq - len(all_logs)
        start = max(0, cursor - oldest_seq)
        return all_logs[start:], seq

    def to_dict(self, include_logs: bool = False) -> Dict[str, Any]:
        with self.lock:
            data: Dict[str, Any] = {
                "id": self.id,
                "tool_id": self.tool_id,
                "title": self.title,
                "params": self.params,
                "status": self.status,
                "exit_code": self.exit_code,
                "error": self.error,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "seq": self.seq,
                "log_count": len(self.logs),
            }
            if include_logs:
                data["logs"] = list(self.logs)
            logs_snapshot = list(self.logs)
            status = self.status

        # 任务结束后一次性抽取指标，避免前端反复解析日志
        if status in TERMINAL_STATUSES:
            if self.metrics is None:
                self.metrics = extract_metrics(logs_snapshot)
            data["metrics"] = self.metrics
        return data


_JOBS: Dict[str, Job] = {}
_GLOBAL_LOCK = threading.Lock()


def get_job(job_id: str) -> Optional[Job]:
    with _GLOBAL_LOCK:
        return _JOBS.get(job_id)


def list_jobs() -> List[Dict[str, Any]]:
    with _GLOBAL_LOCK:
        snapshot = list(_JOBS.values())
    return [job.to_dict() for job in sorted(snapshot, key=lambda j: j.created_at, reverse=True)]


def create_job(tool_id: str, raw_params: Dict[str, Any]) -> Job:
    tool = TOOLS.get(tool_id)
    if tool is None:
        raise KeyError(f"未注册的工具: {tool_id}")
    raw = raw_params or {}
    ack = raw.get("_ack") is True
    if tool.danger and not ack:
        raise ValueError("该工具可能改动数据，请先在页面勾选风险确认")
    # _ack 只作为任务级确认标志，不进入规范化后的工具参数白名单
    params = normalize_params(tool, raw)

    with _GLOBAL_LOCK:
        active = sum(1 for job in _JOBS.values() if job.status not in TERMINAL_STATUSES)
        if active >= MAX_CONCURRENT:
            raise RuntimeError(f"同时运行的任务已达上限 {MAX_CONCURRENT}，请等待现有任务结束")
        if len(_JOBS) >= MAX_JOBS:
            finished = [j for j in _JOBS.values() if j.status in TERMINAL_STATUSES]
            for old in sorted(finished, key=lambda j: j.created_at)[: len(_JOBS) - MAX_JOBS + 1]:
                _JOBS.pop(old.id, None)
        job = Job(
            id=uuid.uuid4().hex[:12], tool_id=tool_id, title=tool.title,
            params=params, ack=ack,
        )
        _JOBS[job.id] = job

    try:
        threading.Thread(target=_execute, args=(job,), daemon=True).start()
    except Exception:
        with _GLOBAL_LOCK:
            _JOBS.pop(job.id, None)
        raise
    return job


def _execute(job: Job) -> None:
    # _ack 随任务下发，子进程据此做第二道风险确认；worker 的参数白名单
    # 仍只接受注册表声明的字段，不会把它拼进 CLI。
    payload = {**job.params, "_ack": job.ack}
    command = [
        sys.executable, "-m", "bridge.worker", job.tool_id,
        json.dumps(payload, ensure_ascii=False),
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
    except Exception as exc:
        with job.lock:
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = time.time()
        job.append(f"[任务失败] 无法启动子进程: {exc}")
        return

    with job.lock:
        job.proc = proc
        if job.status != "cancelling":
            job.status = "running"
        job.started_at = time.time()

    if job.status == "cancelling":
        try:
            proc.terminate()
        except OSError:
            pass

    code = -1
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            job.append(line.rstrip("\r\n"))
        code = proc.wait()
    except Exception as exc:
        job.error = str(exc)
        job.append(f"[任务异常] {exc}")
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
        code = proc.poll() if proc.poll() is not None else -1
    finally:
        with job.lock:
            cancelled = job.status == "cancelling"
            job.status = "cancelled" if cancelled else ("succeeded" if code == 0 else "failed")
            job.exit_code = code
            job.finished_at = time.time()
            job.proc = None
        label = "已取消" if cancelled else ("完成" if code == 0 else "失败")
        job.append(f"[任务结束] {label}，退出码 {code}")
        # 维护任务可能增删了 PDF / 数据库记录，丢弃本进程的文件扫描缓存
        try:
            from . import db

            db.invalidate_scans()
        except Exception:
            pass


def cancel_job(job_id: str) -> Optional[Job]:
    job = get_job(job_id)
    if job is None:
        return None
    with job.lock:
        if job.status in TERMINAL_STATUSES:
            return job
        job.status = "cancelling"
        proc = job.proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    return job


def _format(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_events(job_id: str):
    """SSE 增量日志流：log 事件推送新行，status 事件推送任务状态。"""
    cursor = 0
    last_status = ""
    tick = 0
    deadline = time.monotonic() + SSE_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        job = get_job(job_id)
        if job is None:
            yield _format("done", {"reason": "job_not_found"})
            return

        lines, cursor = job.snapshot(cursor)
        for line in lines:
            yield "data: " + json.dumps({"type": "log", "line": line}, ensure_ascii=False) + "\n\n"

        state = job.to_dict()
        status = state["status"]
        if status != last_status:
            yield _format("status", state)
            last_status = status

        if status in TERMINAL_STATUSES and not lines:
            yield _format("done", state)
            return

        tick += 1
        if tick % 20 == 0:
            yield ": keepalive\n\n"
        await asyncio.sleep(0.25)

    yield _format("done", {"reason": "timeout"})
