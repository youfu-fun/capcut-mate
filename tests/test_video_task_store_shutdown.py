"""Shutdown persistence has one bounded budget, not five seconds per task."""

from datetime import datetime
import sqlite3
import threading
import time

import pytest

from src.utils import video_task_store as store


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "terminal.sqlite3"
    monkeypatch.setattr(store.config, "VIDEO_GEN_TASK_DB_PATH", str(path))
    with sqlite3.connect(path) as conn:
        store._init_schema(conn)
    return path


def _record(index=1, status="failed"):
    return {
        "draft_id": f"draft-{index}",
        "draft_url": f"https://example.invalid/draft?draft_id=draft-{index}",
        "status": status,
        "progress": 100 if status == "completed" else 0,
        "video_url": "https://example.invalid/final.mp4"
        if status == "completed"
        else "",
        "error_message": ""
        if status == "completed"
        else "[SERVICE_STOPPING] cancelled",
        "created_at": datetime(2026, 9, 10, 12),
        "started_at": datetime(2026, 9, 10, 12, 1) if index % 2 else None,
        "completed_at": datetime(2026, 9, 10, 12, 2),
    }


def test_batch_persists_all_terminal_records_and_existing_query_contract(database):
    records = [_record(1), _record(2, "completed"), _record(3)]
    store.save_completed_results_batch(records)
    for record in records:
        result = store.get_completed_by_draft_id(record["draft_id"])
        assert result["status"] == record["status"]
        assert result["error_message"] == record["error_message"]
        assert result["video_url"] == record["video_url"]
        assert result["created_at"] == record["created_at"].isoformat()
        assert result["completed_at"] == record["completed_at"].isoformat()
        assert result["started_at"] == (
            record["started_at"].isoformat() if record["started_at"] else None
        )


def test_existing_single_record_writer_uses_bounded_batch(database, monkeypatch):
    real_batch = store.save_completed_results_batch
    calls = []

    def batch(records, wait_timeout_seconds=0.2):
        calls.append((records, wait_timeout_seconds))
        return real_batch(records, wait_timeout_seconds)

    monkeypatch.setattr(store, "save_completed_results_batch", batch)
    record = _record()
    store.save_completed_result(**record)
    assert calls == [([record], 0.2)]
    assert store.get_completed_by_draft_id(record["draft_id"])["status"] == "failed"


def test_thread_lock_wait_is_bounded_for_entire_batch(database):
    held = threading.Event()
    release = threading.Event()

    def hold_lock():
        with store._lock:
            held.set()
            release.wait(timeout=3)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert held.wait(timeout=1)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="VIDEO_TASK_STORE_LOCK_TIMEOUT"):
            store.save_completed_results_batch(
                [_record(index) for index in range(32)], wait_timeout_seconds=0.05
            )
        assert time.monotonic() - started < 0.4
    finally:
        release.set()
        holder.join(timeout=1)
    assert not holder.is_alive()
    assert store.get_completed_by_draft_id("draft-1") is None


def test_sqlite_writer_lock_is_bounded_and_no_partial_batch_is_saved(database):
    blocking = sqlite3.connect(database)
    blocking.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.save_completed_results_batch(
                [_record(index) for index in range(32)], wait_timeout_seconds=0.05
            )
        assert time.monotonic() - started < 0.4
        assert store._lock.acquire(blocking=False)
        store._lock.release()
    finally:
        blocking.rollback()
        blocking.close()
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM video_gen_task_results").fetchone()[0]
            == 0
        )


def test_thread_and_sqlite_waits_share_one_deadline(database, monkeypatch):
    blocking = sqlite3.connect(database)
    blocking.execute("BEGIN IMMEDIATE")
    held = threading.Event()

    def hold_briefly():
        with store._lock:
            held.set()
            time.sleep(0.08)

    holder = threading.Thread(target=hold_briefly)
    original_connect = store._connect
    remaining_timeouts = []

    def connect(*, timeout=None):
        remaining_timeouts.append(timeout)
        return original_connect(timeout=timeout)

    monkeypatch.setattr(store, "_connect", connect)
    holder.start()
    assert held.wait(timeout=1)
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.save_completed_results_batch([_record()], wait_timeout_seconds=0.15)
        assert time.monotonic() - started < 0.4
        assert 0 < remaining_timeouts[0] < 0.1
    finally:
        holder.join(timeout=1)
        blocking.rollback()
        blocking.close()


def test_commit_wait_for_reader_lock_is_bounded_and_rolls_back_batch(database):
    blocking_reader = sqlite3.connect(database)
    blocking_reader.execute("BEGIN")
    blocking_reader.execute("SELECT * FROM video_gen_task_results").fetchall()
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.save_completed_results_batch(
                [_record(1), _record(2)], wait_timeout_seconds=0.05
            )
        assert time.monotonic() - started < 0.4
    finally:
        blocking_reader.rollback()
        blocking_reader.close()
    assert store.get_completed_by_draft_id("draft-1") is None
    assert store.get_completed_by_draft_id("draft-2") is None


def test_invalid_second_row_rolls_back_first_row(database):
    second = _record(2)
    second["draft_url"] = None
    with pytest.raises(sqlite3.IntegrityError):
        store.save_completed_results_batch([_record(1), second])
    assert store.get_completed_by_draft_id("draft-1") is None
    assert store.get_completed_by_draft_id("draft-2") is None
    # Failure must not retain the process-level lock or an SQLite transaction.
    store.save_completed_results_batch([_record(3)])
    assert store.get_completed_by_draft_id("draft-3")["status"] == "failed"


def test_empty_batch_does_not_acquire_lock_or_connect(database, monkeypatch):
    def no_connect(**kwargs):
        raise AssertionError("An empty batch must not touch SQLite")

    monkeypatch.setattr(store, "_connect", no_connect)
    with store._lock:
        store.save_completed_results_batch([])


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_invalid_wait_budget_is_rejected(database, timeout):
    with pytest.raises(ValueError, match="positive and finite"):
        store.save_completed_results_batch([_record()], wait_timeout_seconds=timeout)


def test_nonterminal_task_is_not_persisted(database):
    with pytest.raises(ValueError, match="terminal"):
        store.save_completed_results_batch([_record(status="processing")])
