"""Cancellation must reap children before any potentially busy terminal store."""

import asyncio
import sqlite3
import sys
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.utils import video_task_manager as module
from src.utils.isolated_process import IsolatedProcessRunner, ProcessTimedOut


@pytest.mark.parametrize("store_busy", [False, True])
def test_shutdown_batches_results_only_after_children_exit(monkeypatch, store_busy):
    manager = object.__new__(module.VideoGenTaskManager)
    manager.__init__()
    manager._process_runner = IsolatedProcessRunner(poll_interval=0.01)
    running = threading.Event()
    batches = []

    async def phase(task, name):
        assert name == "download"
        await manager._process_runner.run(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout=30, on_poll=running.set,
        )

    def save_batch(records):
        assert manager._process_runner.active_count == 0
        assert manager._process_runner.stopped
        assert all(record["status"] == "failed" for record in records)
        batches.append(records)
        if store_busy:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(manager, "_run_phase", phase)
    monkeypatch.setattr(manager, "_cleanup_files", MagicMock())
    monkeypatch.setattr(module, "dequeue_path", MagicMock())
    monkeypatch.setattr(module, "save_completed_results_batch", save_batch)
    try:
        for index in range(45):
            manager.submit_task(f"https://example.invalid/?draft_id=shutdown-{index}")
        assert running.wait(3)
        started = time.monotonic()
        manager.stop(timeout=5)
        assert time.monotonic() - started < 3
        assert len(batches) == 1 and len(batches[0]) == 45
        assert manager.get_active_render_count() == 0
        assert bool(manager._shutdown_results) == store_busy
        manager._cleanup_files.assert_not_called()
    finally:
        manager.stop(timeout=5)


def test_phase_timeout_has_actionable_code(monkeypatch):
    manager = object.__new__(module.VideoGenTaskManager)
    manager.__init__()
    task = module.VideoGenTask(
        draft_id="timeout", draft_url="https://example.invalid/?draft_id=timeout",
        status=module.TaskStatus.PROCESSING, created_at=datetime.now(),
    )

    async def timed_out(*args):
        raise ProcessTimedOut(123, 0.1)

    monkeypatch.setattr(module, "run_video_phase", timed_out)
    with pytest.raises(RuntimeError, match="EXPORT_TIMEOUT"):
        asyncio.run(manager._run_phase(task, "export"))


def test_status_schema_preserves_phase_fields():
    from src.schemas.gen_video_status import GenVideoStatusResponse

    result = GenVideoStatusResponse(
        draft_url="https://example.invalid/?draft_id=status", status="processing",
        progress=40, phase="waiting_export", queue_position=2, node_error="",
    )
    assert result.model_dump()["queue_position"] == 2
    assert result.model_dump()["phase"] == "waiting_export"
