"""The supervisor must reap hung children before returning or freeing a slot."""

import asyncio
import os
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from src.utils import isolated_process
from src.utils.isolated_process import (
    IsolatedProcessRunner,
    ProcessFailed,
    ProcessRunnerStopped,
    ProcessTerminationError,
    ProcessTimedOut,
)


@pytest.fixture
def children(monkeypatch):
    """Retain independent Popen handles to detect and clean any test leak."""
    real_popen = subprocess.Popen
    created = []

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(isolated_process.subprocess, "Popen", start)
    yield created
    leaked = [process for process in created if process.poll() is None]
    for process in leaked:
        process.kill()
        process.wait(timeout=3)
    assert not leaked, f"Supervisor leaked children: {[p.pid for p in leaked]}"


def _python(code):
    return [sys.executable, "-c", code]


async def _wait_children(runner, count):
    deadline = time.monotonic() + 3
    while runner.active_count < count:
        assert time.monotonic() < deadline, "Child was not started"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_success_and_nonzero_exit_are_reaped(children):
    runner = IsolatedProcessRunner(poll_interval=0.01)
    await runner.run(_python("pass"), timeout=3)
    assert runner.active_count == 0
    with pytest.raises(ProcessFailed) as error:
        await runner.run(_python("raise SystemExit(7)"), timeout=3)
    assert error.value.returncode == 7
    assert error.value.pid == children[-1].pid
    assert all(process.poll() is not None for process in children)
    assert runner.active_count == 0


@pytest.mark.asyncio
async def test_hung_child_has_real_deadline_and_next_task_can_run(children):
    runner = IsolatedProcessRunner(poll_interval=0.01)
    started = time.monotonic()
    with pytest.raises(ProcessTimedOut) as error:
        await runner.run(_python("import time; time.sleep(60)"), timeout=0.15)
    assert time.monotonic() - started < 3
    assert error.value.timeout == 0.15
    assert children[0].poll() is not None
    assert not runner.owned_pids
    await runner.run(_python("pass"), timeout=3)


@pytest.mark.asyncio
async def test_cancellation_waits_for_child_to_exit(children):
    runner = IsolatedProcessRunner(poll_interval=0.01)
    task = asyncio.create_task(
        runner.run(_python("import time; time.sleep(60)"), timeout=60)
    )
    await _wait_children(runner, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].poll() is not None
    assert runner.active_count == 0


@pytest.mark.skipif(os.name == "nt", reason="Windows terminate is already forceful")
@pytest.mark.asyncio
async def test_repeated_cancel_during_terminate_still_kills_and_reaps(
    children, tmp_path
):
    ready = tmp_path / "ready"
    code = (
        "import signal,time,pathlib;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).touch();"
        "time.sleep(60)"
    )
    runner = IsolatedProcessRunner(
        poll_interval=0.01, terminate_timeout=0.2, kill_timeout=0.2
    )
    task = asyncio.create_task(runner.run(_python(code), timeout=60))
    deadline = time.monotonic() + 3
    while not ready.exists():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    task.cancel()
    for _ in range(3):
        await asyncio.sleep(0.03)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].poll() == -signal.SIGKILL
    assert runner.active_count == 0


@pytest.mark.asyncio
async def test_stop_cleans_concurrent_children_and_rejects_new_work(children):
    runner = IsolatedProcessRunner(poll_interval=0.01)
    tasks = [
        asyncio.create_task(
            runner.run(_python("import time; time.sleep(60)"), timeout=60)
        )
        for _ in range(3)
    ]
    await _wait_children(runner, 3)
    await runner.stop()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, ProcessRunnerStopped) for result in results)
    assert all(process.poll() is not None for process in children)
    assert not runner.owned_pids
    assert runner.stopped
    await runner.stop()  # Idempotent after confirmed termination.
    with pytest.raises(ProcessRunnerStopped):
        await runner.run(_python("pass"), timeout=3)
    assert len(children) == 3


@pytest.mark.asyncio
async def test_callback_failure_cleans_up_child(children):
    runner = IsolatedProcessRunner(poll_interval=0.01)

    def callback():
        raise ValueError("invalid phase progress")

    with pytest.raises(ValueError, match="invalid phase progress"):
        await runner.run(
            _python("import time; time.sleep(60)"), timeout=60, on_poll=callback
        )
    assert runner.active_count == 0


@pytest.mark.asyncio
async def test_on_poll_callback_is_called_and_cwd_is_used(children, tmp_path):
    calls = []
    runner = IsolatedProcessRunner(poll_interval=0.01)
    await runner.run(
        _python(f"import os; assert os.getcwd() == {str(tmp_path)!r}"),
        timeout=3,
        on_poll=lambda: calls.append(True),
        cwd=tmp_path,
    )
    assert calls


@pytest.mark.asyncio
async def test_explicit_child_environment_is_available_before_import(children):
    runner = IsolatedProcessRunner(poll_interval=0.01)
    original_value = os.environ.get("CAPCUT_MATE_PHASE_WORKER")
    child_env = {**os.environ, "CAPCUT_MATE_PHASE_WORKER": "1"}
    await runner.run(
        _python("import os; assert os.environ['CAPCUT_MATE_PHASE_WORKER'] == '1'"),
        timeout=3,
        env=child_env,
    )
    assert os.environ.get("CAPCUT_MATE_PHASE_WORKER") == original_value


@pytest.mark.parametrize("windows,flags", [(True, 0x200), (False, 0)])
@pytest.mark.asyncio
async def test_creation_flags_and_inherited_output(monkeypatch, windows, flags):
    process = Mock(pid=4321)
    process.poll.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr(isolated_process, "_IS_WINDOWS", windows)
    monkeypatch.setattr(isolated_process.subprocess, "Popen", popen)
    runner = IsolatedProcessRunner()
    await runner.run(["python", "worker.py"], timeout=3)
    kwargs = popen.call_args.kwargs
    assert kwargs["creationflags"] == flags
    assert kwargs["stdout"] is None
    assert kwargs["stderr"] is None
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["close_fds"] is True


@pytest.mark.asyncio
async def test_unconfirmed_exit_retains_handle_and_fails_closed(monkeypatch):
    process = Mock(pid=4321)
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(isolated_process.subprocess, "Popen", popen)
    runner = IsolatedProcessRunner(
        poll_interval=0.005, terminate_timeout=0.01, kill_timeout=0.01
    )
    with pytest.raises(ProcessTerminationError):
        await runner.run(["blocked-worker"], timeout=0.01)
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert runner.owned_pids == (4321,)
    with pytest.raises(ProcessTerminationError):
        await runner.run(["must-not-start"], timeout=1)
    assert popen.call_count == 1
    with pytest.raises(ProcessTerminationError):
        await runner.stop()
    assert runner.owned_pids == (4321,)
    process.poll.return_value = 1
    await runner.stop()  # Reap if the owned process subsequently does exit.
    assert not runner.owned_pids
    with pytest.raises(ProcessRunnerStopped):
        await runner.run(["must-not-start"], timeout=1)


@pytest.mark.asyncio
async def test_cancelling_stop_still_waits_for_every_child(monkeypatch):
    class SlowTermination:
        pid = 1234
        returncode = None
        killed = False

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = SlowTermination()
    monkeypatch.setattr(isolated_process.subprocess, "Popen", lambda *a, **k: process)
    runner = IsolatedProcessRunner(
        poll_interval=0.005, terminate_timeout=0.1, kill_timeout=0.1
    )
    running = asyncio.create_task(runner.run(["fake"], timeout=60))
    await _wait_children(runner, 1)
    stopping = asyncio.create_task(runner.stop())
    await asyncio.sleep(0.02)
    stopping.cancel()
    await asyncio.sleep(0.02)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    with pytest.raises(ProcessRunnerStopped):
        await running
    assert process.killed
    assert not runner.owned_pids


@pytest.mark.asyncio
async def test_stop_cleans_other_children_even_when_one_cannot_terminate(monkeypatch):
    stuck = Mock(pid=111)
    stuck.poll.return_value = None
    normal = Mock(pid=222)
    normal.poll.return_value = None
    normal.terminate.side_effect = lambda: setattr(normal.poll, "return_value", -15)
    popen = Mock(side_effect=[stuck, normal])
    monkeypatch.setattr(isolated_process.subprocess, "Popen", popen)
    runner = IsolatedProcessRunner(
        poll_interval=0.005, terminate_timeout=0.02, kill_timeout=0.02
    )
    tasks = [asyncio.create_task(runner.run(["fake"], timeout=60)) for _ in range(2)]
    await _wait_children(runner, 2)
    with pytest.raises(ProcessTerminationError):
        await runner.stop()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[0], ProcessTerminationError)
    assert isinstance(results[1], ProcessRunnerStopped)
    normal.terminate.assert_called_once_with()
    assert runner.owned_pids == (111,)
    stuck.poll.return_value = -9
    await runner.stop()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
@pytest.mark.asyncio
async def test_invalid_timeout_never_starts_child(monkeypatch, timeout):
    popen = Mock()
    monkeypatch.setattr(isolated_process.subprocess, "Popen", popen)
    with pytest.raises(ValueError):
        await IsolatedProcessRunner().run(["python"], timeout=timeout)
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_stop_before_run_starts_no_process(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(isolated_process.subprocess, "Popen", popen)
    runner = IsolatedProcessRunner()
    await runner.stop()
    with pytest.raises(ProcessRunnerStopped):
        await runner.run(["python"], timeout=1)
    popen.assert_not_called()
