"""
视频生成任务完成结果持久化（SQLite）。
进行中的任务仅存在于内存，完成（成功或失败）后写入本模块。
"""

from __future__ import annotations

import os
import math
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

import config
from src.utils.logger import logger

_MAX_ROWS = 100_000
_PRUNE_BATCH = 1_000
# 高并发下避免每次状态查询都跑 COUNT/竞争锁；超过间隔才真正检查是否需清理
_PRUNE_MIN_INTERVAL_SEC = 60.0
_last_prune_at: Optional[float] = None

_lock = threading.Lock()


def _connect(*, timeout: Optional[float] = None) -> sqlite3.Connection:
    path = config.VIDEO_GEN_TASK_DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kwargs = {"timeout": timeout} if timeout is not None else {}
    conn = sqlite3.connect(path, check_same_thread=False, **kwargs)
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS video_gen_task_results (
            draft_id TEXT PRIMARY KEY NOT NULL,
            draft_url TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL,
            video_url TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT,
            started_at TEXT,
            completed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vgtr_completed_at
        ON video_gen_task_results(completed_at)
        """
    )
    conn.commit()


def _ensure_schema() -> None:
    with _lock:
        conn = _connect()
        try:
            _init_schema(conn)
        finally:
            conn.close()


_ensure_schema()


def prune_if_needed() -> None:
    """
    若表中记录数超过 10 万，按 completed_at 最旧优先删除 1000 条。
    在查询已落库任务前可调用；高并发下通过时间间隔节流，避免每次请求都 COUNT(*)。
    """
    global _last_prune_at
    now = time.monotonic()
    if _last_prune_at is not None and now - _last_prune_at < _PRUNE_MIN_INTERVAL_SEC:
        return
    with _lock:
        now2 = time.monotonic()
        if (
            _last_prune_at is not None
            and now2 - _last_prune_at < _PRUNE_MIN_INTERVAL_SEC
        ):
            return
        _last_prune_at = now2
        conn = _connect()
        try:
            (cnt,) = conn.execute(
                "SELECT COUNT(*) FROM video_gen_task_results"
            ).fetchone()
            if cnt <= _MAX_ROWS:
                return
            conn.execute(
                """
                DELETE FROM video_gen_task_results WHERE rowid IN (
                    SELECT rowid FROM video_gen_task_results
                    ORDER BY completed_at ASC, draft_id ASC
                    LIMIT ?
                )
                """,
                (_PRUNE_BATCH,),
            )
            conn.commit()
            logger.info(
                "video_gen_task_results pruned %s rows (count was %s)",
                _PRUNE_BATCH,
                cnt,
            )
        finally:
            conn.close()


def save_completed_result(
    draft_id: str,
    draft_url: str,
    status: str,
    progress: int,
    video_url: str,
    error_message: str,
    created_at: datetime,
    started_at: Optional[datetime],
    completed_at: datetime,
) -> None:
    """持久化已完成任务（成功或失败）。不在此保存进行中的任务。"""
    save_completed_results_batch(
        [
            {
                "draft_id": draft_id,
                "draft_url": draft_url,
                "status": status,
                "progress": progress,
                "video_url": video_url,
                "error_message": error_message,
                "created_at": created_at,
                "started_at": started_at,
                "completed_at": completed_at,
            }
        ]
    )


def save_completed_results_batch(
    records: list[dict[str, Any]],
    wait_timeout_seconds: float = 0.2,
) -> None:
    """一次事务保存整批终态，共享有界的线程锁/SQLite 锁等待预算。

    超时或任意记录失败会抛异常并回滚整批，不将部分持久化伪装成成功。
    截止时间同时用于 Python 锁、BEGIN IMMEDIATE 和 commit，避免每条记录
    分别等待 SQLite 默认的 5 秒。磁盘/操作系统 I/O 本身不是可抢占操作。
    """
    if (
        isinstance(wait_timeout_seconds, bool)
        or not math.isfinite(wait_timeout_seconds)
        or wait_timeout_seconds <= 0
    ):
        raise ValueError("wait_timeout_seconds must be positive and finite")
    if not records:
        return
    deadline = time.monotonic() + wait_timeout_seconds
    values = []
    for record in records:
        if record["status"] not in {"completed", "failed"}:
            raise ValueError("Only terminal video tasks may be persisted")
        values.append(
            (
                record["draft_id"],
                record["draft_url"],
                record["status"],
                record["progress"],
                record["video_url"],
                record["error_message"],
                record["created_at"].isoformat(),
                record["started_at"].isoformat() if record["started_at"] else None,
                record["completed_at"].isoformat(),
            )
        )

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("[VIDEO_TASK_STORE_TIMEOUT] 终态持久化等待超时")
        return seconds

    if not _lock.acquire(timeout=remaining()):
        raise TimeoutError("[VIDEO_TASK_STORE_LOCK_TIMEOUT] 终态持久化线程锁等待超时")
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect(timeout=remaining())
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        # Reserve the writer once; executemany then cannot accrue one lock wait
        # per record. Readers can still delay COMMIT, so reset its remaining
        # budget immediately before committing as well.
        conn.execute(f"PRAGMA busy_timeout={int(remaining() * 1000)}")
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            """
            INSERT OR REPLACE INTO video_gen_task_results (
                draft_id, draft_url, status, progress,
                video_url, error_message,
                created_at, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        conn.execute(f"PRAGMA busy_timeout={int(remaining() * 1000)}")
        conn.commit()
    except BaseException:
        if conn is not None:
            conn.set_progress_handler(None, 0)
            conn.rollback()
        raise
    finally:
        try:
            if conn is not None:
                conn.close()
        finally:
            _lock.release()


def get_completed_by_draft_id(draft_id: str) -> Optional[Dict[str, Any]]:
    """按 draft_id 读取已持久化的任务结果；无记录则返回 None。"""
    if not draft_id:
        return None
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT draft_url, status, progress, video_url, error_message,
                       created_at, started_at, completed_at
                FROM video_gen_task_results
                WHERE draft_id = ?
                """,
                (draft_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "draft_url": row["draft_url"],
                "status": row["status"],
                "progress": row["progress"],
                "video_url": row["video_url"] or "",
                "error_message": row["error_message"] or "",
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
            }
        finally:
            conn.close()
