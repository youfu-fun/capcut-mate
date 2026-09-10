"""Bounded, cancellable supervision of direct child processes.

All methods must run on the same event loop.  Blocking native automation belongs
in the child, never in an executor thread: cancelling a thread cannot stop COM.
Only processes created here are terminated; their descendants (including an
editor they may have launched) are deliberately left alone.
"""

from __future__ import annotations

import asyncio
import math
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


_IS_WINDOWS = sys.platform == "win32"


class ProcessFailed(RuntimeError):
    def __init__(self, pid: int, returncode: int):
        self.pid = pid
        self.returncode = returncode
        super().__init__(f"Child process {pid} exited with code {returncode}")


class ProcessTimedOut(TimeoutError):
    def __init__(self, pid: int, timeout: float):
        self.pid = pid
        self.timeout = timeout
        super().__init__(f"Child process {pid} exceeded its {timeout:g}s deadline")


class ProcessTerminationError(RuntimeError):
    def __init__(self, pid: int):
        self.pid = pid
        super().__init__(
            f"Cannot confirm child process {pid} has stopped; runner is unavailable"
        )


class ProcessRunnerStopped(RuntimeError):
    """The runner is shutting down and accepts no more work."""


@dataclass(eq=False)
class _OwnedProcess:
    process: subprocess.Popen
    cleanup: asyncio.Task | None = None


class IsolatedProcessRunner:
    def __init__(
        self,
        *,
        poll_interval: float = 0.1,
        terminate_timeout: float = 2.0,
        kill_timeout: float = 2.0,
    ):
        for name, value in (
            ("poll_interval", poll_interval),
            ("terminate_timeout", terminate_timeout),
            ("kill_timeout", kill_timeout),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite duration")
        if terminate_timeout > 2 or kill_timeout > 2:
            raise ValueError("Each termination grace period must be at most 2s")
        self._poll_interval = poll_interval
        self._terminate_timeout = terminate_timeout
        self._kill_timeout = kill_timeout
        self._owned: dict[int, _OwnedProcess] = {}
        self._stopped = False
        self._poisoned = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def owned_pids(self) -> tuple[int, ...]:
        return tuple(self._owned)

    @property
    def active_count(self) -> int:
        return len(self._owned)

    @property
    def stopped(self) -> bool:
        return self._stopped

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("IsolatedProcessRunner must use a single event loop")

    async def run(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        on_poll: Callable[[], None] | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Run one child; return only after a confirmed successful exit.

        A timeout or cancellation first terminates and reaps the direct child.
        Repeated cancellation cannot interrupt that bounded cleanup.  A failed
        cleanup retains ownership and prevents any new children from starting.
        ``on_poll`` is synchronous and must not block (e.g. read a small status
        file); its exceptions also trigger cleanup.
        """
        self._check_loop()
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite duration")
        if not command or isinstance(command, (str, bytes)):
            raise ValueError("command must be a non-empty argument sequence")
        if self._stopped:
            raise ProcessRunnerStopped("Process runner has stopped")
        if self._poisoned:
            raise ProcessTerminationError(next(iter(self._owned), -1))

        started = time.monotonic()
        # No await between the acceptance check, creation and registration:
        # stop() cannot miss a process being started on this event loop.
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            close_fds=True,
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                if _IS_WINDOWS
                else 0
            ),
        )
        owned = _OwnedProcess(process)
        self._owned[process.pid] = owned
        try:
            while True:
                if self._stopped:
                    raise ProcessRunnerStopped(
                        "Process runner stopped during execution"
                    )
                returncode = process.poll()
                if returncode is not None:
                    if returncode:
                        raise ProcessFailed(process.pid, returncode)
                    self._owned.pop(process.pid, None)
                    return
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise ProcessTimedOut(process.pid, timeout)
                if on_poll is not None:
                    on_poll()
                await asyncio.sleep(min(self._poll_interval, remaining))
        except BaseException:
            # shield() alone is insufficient: it returns early when its caller
            # is cancelled. Keep awaiting the same cleanup task until it ends.
            cancelled = await self._await_cleanup(self._start_cleanup(owned))
            if cancelled:
                raise asyncio.CancelledError
            raise

    async def stop(self) -> None:
        """Reject new work and stop all owned children concurrently.

        Call on the runner's loop, e.g. via run_coroutine_threadsafe from the
        service's shutdown thread. Idempotent; an unconfirmed termination can
        be retried by another stop() call but never allows the runner to reopen.
        """
        self._check_loop()
        self._stopped = True
        cleanup_tasks = [
            self._start_cleanup(owned) for owned in list(self._owned.values())
        ]
        cancelled = False
        first_error: BaseException | None = None
        # All cleanup tasks start together; wait for every one even if an
        # earlier child cannot be terminated or stop() itself gets cancelled.
        for task in cleanup_tasks:
            try:
                cancelled = await self._await_cleanup(task) or cancelled
            except BaseException as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
        if cancelled:
            raise asyncio.CancelledError

    def _start_cleanup(self, owned: _OwnedProcess) -> asyncio.Task:
        if owned.cleanup is None or (
            owned.cleanup.done() and owned.process.pid in self._owned
        ):
            owned.cleanup = asyncio.create_task(self._terminate(owned))
        return owned.cleanup

    @staticmethod
    async def _await_cleanup(task: asyncio.Task) -> bool:
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()  # A termination failure takes priority over cancellation.
        return cancelled

    async def _wait_exit(self, process: subprocess.Popen, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if process.poll() is not None:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self._poll_interval, remaining))

    async def _terminate(self, owned: _OwnedProcess) -> None:
        process = owned.process
        try:
            if process.poll() is not None:
                self._owned.pop(process.pid, None)
                return
            try:
                process.terminate()
            except OSError:
                pass  # It may have exited between poll() and terminate().
            if not await self._wait_exit(process, self._terminate_timeout):
                try:
                    process.kill()
                except OSError:
                    pass
                if not await self._wait_exit(process, self._kill_timeout):
                    raise ProcessTerminationError(process.pid)
            self._owned.pop(process.pid, None)
        except BaseException as exc:
            self._poisoned = True
            # Keep the Popen handle so a later stop() can retry and no caller
            # mistakes an unconfirmed process for a freed automation slot.
            if isinstance(exc, ProcessTerminationError):
                raise
            raise ProcessTerminationError(process.pid) from exc
