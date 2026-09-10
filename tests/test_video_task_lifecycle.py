"""Task-manager lifecycle regression tests; never call Jianying or a network API.

The pipeline stubs only its external phases. Queueing, semaphore admission,
worker shutdown and child-process cancellation use the real implementation.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
import importlib
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.utils.video_task_manager as manager_module
from src.utils.isolated_process import IsolatedProcessRunner
from src.utils.video_task_manager import TaskStatus, VideoGenTaskManager


def _url(draft_id: str) -> str:
    return f"http://example.invalid/get_draft?draft_id={draft_id}"


def _wait_until(predicate, *, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "task manager did not reach the expected state in time"


async def _wait_event(event: threading.Event) -> None:
    while not event.is_set():
        await asyncio.sleep(0.005)


@pytest.fixture
def isolated_manager(monkeypatch, tmp_path):
    # Bypass __new__, without replacing the application singleton used elsewhere.
    manager = object.__new__(VideoGenTaskManager)
    manager.__init__()
    manager._process_runner = IsolatedProcessRunner(
        poll_interval=0.01, terminate_timeout=0.2, kill_timeout=0.2
    )
    persisted = []

    def persist(task):
        task.completed_at = datetime.now()
        persisted.append((task.draft_id, task.status, task.error_message))

    monkeypatch.setattr(manager, "_persist_terminal_task", persist)
    monkeypatch.setattr(manager, "_cleanup_files", MagicMock())
    monkeypatch.setattr(manager_module, "dequeue_path", MagicMock())
    monkeypatch.setattr(manager_module.config, "DRAFT_SAVE_PATH", str(tmp_path))
    yield manager, persisted
    manager.stop(timeout=5)
    assert manager._process_runner.active_count == 0


def _prepare_output(task, tmp_path):
    task.outfile = str(tmp_path / f"{task.draft_id}.mp4")
    task.export_outfile_history = [task.outfile]


def test_more_than_a_thread_pool_of_tasks_complete_with_bounded_phase_concurrency(
    isolated_manager, monkeypatch, tmp_path
):
    manager, persisted = isolated_manager
    active = Counter()
    peak = Counter()
    exports = []
    release_export = threading.Event()

    async def phase(task, name):
        active[name] += 1
        peak[name] = max(peak[name], active[name])
        try:
            if name == "download":
                _prepare_output(task, tmp_path)
                await asyncio.sleep(0.01)
                return ""
            if name == "probe":
                return True
            if name == "export":
                exports.append(task.draft_id)
                await _wait_event(release_export)
                await asyncio.sleep(0.002)
                Path(task.outfile).write_bytes(b"verified-export-test-fixture")
                return ""
            if name == "upload":
                await asyncio.sleep(0.02)
                return f"http://example.invalid/{task.draft_id}.mp4", ""
            raise AssertionError(name)
        finally:
            active[name] -= 1

    monkeypatch.setattr(manager, "_run_phase", phase)
    count = 45
    try:
        for index in range(count):
            manager.submit_task(_url(f"concurrent-{index}"))
        _wait_until(lambda: bool(exports))
        _wait_until(lambda: any(t.phase == "waiting_export" for t in manager.tasks.values()))
        waiting = next(t for t in manager.tasks.values() if t.phase == "waiting_export")
        assert manager.get_task_status(waiting.draft_url)["queue_position"] >= 1
        assert manager.get_active_render_count() == count
        loop = manager._worker_event_loop
        assert loop is not None and getattr(loop, "_default_executor", None) is None
        assert not hasattr(manager, "_download_executor")
        assert not hasattr(manager, "_upload_executor")
    finally:
        release_export.set()

    _wait_until(lambda: manager.get_active_render_count() == 0)
    assert all(task.status == TaskStatus.COMPLETED for task in manager.tasks.values())
    assert len(exports) == count and len(set(exports)) == count
    assert len(persisted) == count
    assert peak["download"] == 3
    assert peak["export"] == 1
    assert peak["upload"] == 2
    assert manager._cleanup_files.call_count == count


def test_hung_export_fails_queued_tasks_and_new_request_can_recover_at_home(
    isolated_manager, monkeypatch, tmp_path
):
    manager, persisted = isolated_manager
    submitted = threading.Event()
    exports = []
    uploaded = []

    async def phase(task, name):
        if name == "download":
            _prepare_output(task, tmp_path)
            await asyncio.sleep(0.002)
            return ""
        if name == "probe":
            return True
        if name == "export":
            exports.append(task.draft_id)
            if len(exports) == 1:
                # All requests must belong to the same node generation before
                # the first timeout quarantines the desktop.
                await _wait_event(submitted)
                await manager._process_runner.run(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    timeout=0.1,
                )
                raise AssertionError("hung child unexpectedly returned")
            Path(task.outfile).write_bytes(b"verified-export-test-fixture")
            return ""
        if name == "upload":
            uploaded.append(task.draft_id)
            return f"http://example.invalid/{task.draft_id}.mp4", ""
        raise AssertionError(name)

    monkeypatch.setattr(manager, "_run_phase", phase)
    count = 45
    try:
        for index in range(count):
            manager.submit_task(_url(f"timeout-{index}"))
    finally:
        submitted.set()

    _wait_until(lambda: manager.get_active_render_count() == 0)
    assert len(exports) == 1
    first = manager.tasks[_url(exports[0])]
    assert first.status == TaskStatus.FAILED
    assert "deadline" in first.error_message
    queued = [t for t in manager.tasks.values() if t is not first]
    assert all(t.status == TaskStatus.FAILED for t in queued)
    assert all("RPA_NODE_NOT_READY" in t.error_message for t in queued)
    assert len(persisted) == count
    assert manager._process_runner.active_count == 0
    assert manager._node_error
    assert not uploaded
    manager._cleanup_files.assert_not_called()

    manager.submit_task(_url("recovered"))
    _wait_until(lambda: manager.tasks[_url("recovered")].status == TaskStatus.COMPLETED)
    recovered = manager.tasks[_url("recovered")]
    assert recovered.video_url.endswith("/recovered.mp4")
    assert manager._node_error == ""
    assert exports[-1] == "recovered"
    assert uploaded == ["recovered"]
    manager._cleanup_files.assert_called_once_with(recovered)


def test_export_timeout_promptly_cancels_already_running_download_children(
    isolated_manager, monkeypatch, tmp_path
):
    manager, persisted = isolated_manager
    downloads_started = set()
    three_downloads_running = threading.Event()
    exports = []
    calls = []

    async def phase(task, name):
        calls.append((task.draft_id, name))
        if name == "download":
            _prepare_output(task, tmp_path)
            if task.draft_id == "fault-trigger":
                return ""

            def mark_started():
                downloads_started.add(task.draft_id)
                if len(downloads_started) == 3:
                    three_downloads_running.set()

            await manager._process_runner.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=30,
                on_poll=mark_started,
            )
            raise AssertionError("quarantined download was not cancelled")
        if name == "probe":
            return True
        if name == "export":
            exports.append(task.draft_id)
            await _wait_event(three_downloads_running)
            await manager._process_runner.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=0.1,
            )
            raise AssertionError("hung export was not terminated")
        raise AssertionError("quarantined node must not reach upload")

    monkeypatch.setattr(manager, "_run_phase", phase)
    manager.submit_task(_url("fault-trigger"))
    for index in range(12):
        manager.submit_task(_url(f"downloading-when-node-fails-{index}"))
    assert three_downloads_running.wait(timeout=3)
    started = time.monotonic()
    _wait_until(lambda: manager.get_active_render_count() == 0, timeout=3)

    assert time.monotonic() - started < 3, "node failure must not wait for download deadlines"
    assert len(downloads_started) == 3
    assert exports == ["fault-trigger"]
    assert all(name != "upload" for _, name in calls)
    assert manager._process_runner.active_count == 0
    assert all(task.status == TaskStatus.FAILED for task in manager.tasks.values())
    other_tasks = [task for task in manager.tasks.values() if task.draft_id != "fault-trigger"]
    assert all("RPA_NODE_NOT_READY" in task.error_message for task in other_tasks)
    assert len(persisted) == 13
    manager._cleanup_files.assert_not_called()
    assert manager.worker_thread is not None and manager.worker_thread.is_alive()


@pytest.mark.parametrize("blocked_phase", ["download", "export", "upload"])
def test_stop_cancels_owned_children_running_and_queued_tasks_without_cleanup(
    isolated_manager, monkeypatch, tmp_path, blocked_phase
):
    manager, persisted = isolated_manager
    child_started = threading.Event()
    collect_terminal = manager._persist_terminal_task
    batches = []

    def persist_terminal(task):
        collect_terminal(task)
        VideoGenTaskManager._persist_terminal_task(manager, task)

    def save_batch(records):
        assert manager._process_runner.active_count == 0, "reap children before waiting for SQLite"
        batches.append(list(records))

    monkeypatch.setattr(manager, "_persist_terminal_task", persist_terminal)
    monkeypatch.setattr(manager_module, "save_completed_results_batch", save_batch)

    async def phase(task, name):
        if name == blocked_phase:
            await manager._process_runner.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=30,
                on_poll=child_started.set,
            )
            raise AssertionError("cancelled child unexpectedly completed")
        if name == "download":
            _prepare_output(task, tmp_path)
            return ""
        if name == "probe":
            return True
        if name == "export":
            Path(task.outfile).write_bytes(b"preserved-export-test-fixture")
            return ""
        if name == "upload":
            raise AssertionError("upload must not follow a blocked earlier phase")
        raise AssertionError(name)

    monkeypatch.setattr(manager, "_run_phase", phase)
    manager.submit_task(_url("stop-first"))
    assert child_started.wait(timeout=3), "expected an owned child before shutdown"
    for index in range(40):
        manager.submit_task(_url(f"stop-waiting-{index}"))
    loop = manager._worker_event_loop
    assert loop is not None and getattr(loop, "_default_executor", None) is None
    started = time.monotonic()
    manager.stop(timeout=5)

    assert time.monotonic() - started < 5
    assert manager.worker_thread is not None and not manager.worker_thread.is_alive()
    assert manager._worker_event_loop is None
    assert manager._process_runner.active_count == 0
    assert manager._process_runner.stopped
    assert manager.get_active_render_count() == 0
    assert manager.task_queue.empty()
    assert len(persisted) == 41
    assert len(batches) == 1 and len(batches[0]) == 41
    assert not manager._shutdown_results
    assert all(task.status == TaskStatus.FAILED for task in manager.tasks.values())
    assert all("SERVICE_STOPPING" in task.error_message for task in manager.tasks.values())
    manager._cleanup_files.assert_not_called()
    for task in manager.tasks.values():
        if task.outfile and Path(task.outfile).exists():
            assert Path(task.outfile).read_bytes() == b"preserved-export-test-fixture"
    with pytest.raises(RuntimeError, match="SERVICE_STOPPING"):
        manager.submit_task(_url("rejected-after-stop"))
    manager.stop(timeout=0)
    assert len(persisted) == 41, "idempotent stop must not persist duplicate terminal transitions"


@pytest.mark.parametrize("file_state", ["missing", "empty"])
def test_export_without_verified_output_cannot_upload(
    isolated_manager, monkeypatch, tmp_path, file_state
):
    manager, _ = isolated_manager
    calls = []

    async def phase(task, name):
        calls.append(name)
        if name == "download":
            _prepare_output(task, tmp_path)
            return ""
        if name == "probe":
            return True
        if name == "export":
            if file_state == "empty":
                Path(task.outfile).touch()
            return ""
        raise AssertionError("unverified output must never reach upload")

    monkeypatch.setattr(manager, "_run_phase", phase)
    manager.submit_task(_url("unverified"))
    _wait_until(lambda: manager.get_active_render_count() == 0)
    task = manager.tasks[_url("unverified")]
    assert task.status == TaskStatus.FAILED
    assert "EXPORT_OUTPUT_MISSING" in task.error_message
    assert calls == ["download", "probe", "export"]
    assert task.video_url == ""
    manager._cleanup_files.assert_not_called()


@pytest.mark.parametrize("body_raises", [False, True])
def test_fastapi_lifespan_stops_admission_then_background_tasks_and_worker(
    monkeypatch, body_raises
):
    app_module = importlib.import_module("main")
    cleanup_module = importlib.import_module("src.utils.draft_cleanup")
    delete_module = importlib.import_module("src.utils.deferred_delete")
    events = []

    async def background(name):
        events.append(f"{name}-started")
        try:
            await asyncio.Future()
        finally:
            events.append(f"{name}-cancelled")

    async def cleanup():
        await background("cleanup")

    async def deferred_delete():
        await background("delete")

    def request_stop():
        events.append("request-stop")

    async def astop():
        events.append("astop")

    monkeypatch.setattr(cleanup_module, "draft_cleanup_background_loop", cleanup)
    monkeypatch.setattr(delete_module, "deferred_delete_background_loop", deferred_delete)
    monkeypatch.setattr(
        manager_module, "task_manager", SimpleNamespace(request_stop=request_stop, astop=astop)
    )

    async def exercise():
        async with app_module.lifespan(app_module.app):
            await asyncio.sleep(0)
            events.append("body")
            if body_raises:
                raise ValueError("simulated lifespan body error")

    if body_raises:
        with pytest.raises(ValueError, match="simulated lifespan body error"):
            asyncio.run(exercise())
    else:
        asyncio.run(exercise())
    assert events == [
        "cleanup-started", "delete-started", "body", "request-stop",
        "cleanup-cancelled", "delete-cancelled", "astop",
    ]
