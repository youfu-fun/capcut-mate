"""Private file protocol for supervised video phases (never import the web server).

Only the parent owns public task state. Children may publish progress and output
paths, but cannot mark a task completed or failed. Credentials travel in a private
temporary request file, never in argv, status or result documents.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4


PHASES = frozenset({"download", "export", "upload", "probe"})
PROTOCOL_VERSION = 1
MAX_DOCUMENT_BYTES = 256 * 1024
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHILD_ERROR_CODES = frozenset({
    "VIDEO_PHASE_CHILD_FAILED", "VIDEO_PHASE_BOOTSTRAP_FAILED",
    "VIDEO_PHASE_PARENT_MISMATCH", "VIDEO_PHASE_PRIVATE_PATHS_INVALID",
    "VIDEO_PHASE_REQUEST_INVALID",
})


class VideoPhaseProtocolError(RuntimeError):
    """A child did not provide a trustworthy phase result."""


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError("oversized document")
        document = json.loads(data)
        if not isinstance(document, dict):
            raise ValueError("expected object")
        return document
    except (OSError, UnicodeError, ValueError):
        raise VideoPhaseProtocolError("VIDEO_PHASE_PROTOCOL_INVALID") from None


def _identity(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "nonce": request["nonce"],
        "phase": request["phase"],
        "draft_id": request["task"]["draft_id"],
    }


def _check_identity(document: dict[str, Any], request: dict[str, Any]) -> None:
    if any(document.get(key) != value for key, value in _identity(request).items()):
        raise VideoPhaseProtocolError("VIDEO_PHASE_IDENTITY_MISMATCH")


def _validate_paths(document: dict[str, Any]) -> tuple[int, str, list[str]]:
    progress = document.get("progress")
    outfile = document.get("outfile")
    history = document.get("export_outfile_history")
    if (
        type(progress) is not int
        or not 0 <= progress <= 100
        or not isinstance(outfile, str)
        or not isinstance(history, list)
        or len(history) > 100
        or any(not isinstance(path, str) or not path for path in history)
    ):
        raise VideoPhaseProtocolError("VIDEO_PHASE_STATUS_INVALID")
    return progress, outfile, list(history)


def _apply_status(path: Path, request: dict[str, Any], task: Any, *, required: bool = False) -> None:
    if not path.exists() and not required:
        return
    document = _read_json(path)
    _check_identity(document, request)
    if set(document) != set(_identity(request)) | {"progress", "outfile", "export_outfile_history"}:
        raise VideoPhaseProtocolError("VIDEO_PHASE_STATUS_INVALID")
    progress, outfile, history = _validate_paths(document)
    # In particular, never copy status, video_url or terminal timestamps here.
    task.progress = max(task.progress, progress)
    task.outfile = outfile
    task.export_outfile_history = history


def _redact(value: str, api_key: str | None) -> str:
    if api_key:
        value = value.replace(api_key, "[redacted]").replace(quote(api_key, safe=""), "[redacted]")
    return value


def _validate_value(phase: str, value: Any) -> Any:
    if phase == "probe":
        if type(value) is not bool:
            raise VideoPhaseProtocolError("VIDEO_PHASE_RESULT_INVALID")
        return value
    if phase in {"download", "export"}:
        if not isinstance(value, str):
            raise VideoPhaseProtocolError("VIDEO_PHASE_RESULT_INVALID")
        return value
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(not isinstance(item, str) for item in value)
        or (not value[1] and not value[0].strip())
    ):
        raise VideoPhaseProtocolError("VIDEO_PHASE_RESULT_INVALID")
    return tuple(value)


def _require_output(outfile: str) -> None:
    try:
        if not outfile or not Path(outfile).is_file() or Path(outfile).stat().st_size <= 0:
            raise ValueError("empty output")
    except (OSError, ValueError):
        raise VideoPhaseProtocolError("VIDEO_PHASE_OUTPUT_MISSING") from None


async def run_video_phase(runner: Any, task: Any, phase: str, timeout: float) -> Any:
    """Run one isolated synchronous phase; runner owns its hard wall-clock limit."""
    if phase not in PHASES or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Invalid video phase or timeout")
    request = {
        "version": PROTOCOL_VERSION,
        "nonce": uuid4().hex,
        "phase": phase,
        "parent_pid": os.getpid(),
        "task": {
            "draft_url": task.draft_url,
            "draft_id": task.draft_id,
            "api_key": task.api_key,
            "progress": task.progress,
            "outfile": task.outfile,
            "export_outfile_history": list(task.export_outfile_history),
        },
    }
    _validate_request(request)
    with tempfile.TemporaryDirectory(prefix="capcut-video-phase-") as directory:
        private_dir = Path(directory)
        os.chmod(private_dir, 0o700)
        request_path, status_path, result_path = (
            private_dir / name for name in ("request.json", "status.json", "result.json")
        )
        _atomic_json(request_path, request)
        command = [sys.executable, "-m", "src.utils.video_phase_process",
                   str(request_path), str(status_path), str(result_path)]
        try:
            await runner.run(
                command, timeout=timeout,
                on_poll=lambda: _apply_status(status_path, request, task),
                cwd=str(REPOSITORY_ROOT),
                # src.utils imports logging before this module's main executes.
                # Set this in the child environment to avoid shared file rotation.
                env={**os.environ, "CAPCUT_MATE_PHASE_WORKER": "1"},
            )
        except BaseException as exc:
            # Preserve the latest owned paths even when the last poll raced with
            # a timeout. A broken status must not hide cancellation/timeout.
            try:
                _apply_status(status_path, request, task)
            except VideoPhaseProtocolError:
                pass
            from src.utils.isolated_process import ProcessFailed
            if isinstance(exc, ProcessFailed):
                try:
                    failed = _read_json(result_path)
                    _check_identity(failed, request)
                    code = failed.get("error")
                    if failed.get("ok") is False and isinstance(code, str) and code in CHILD_ERROR_CODES:
                        raise VideoPhaseProtocolError(code) from exc
                except VideoPhaseProtocolError as protocol_error:
                    if str(protocol_error) in CHILD_ERROR_CODES:
                        raise
            raise
        _apply_status(status_path, request, task, required=True)
        result = _read_json(result_path)
        _check_identity(result, request)
        if result.get("ok") is not True or set(result) != set(_identity(request)) | {"ok", "value"}:
            raise VideoPhaseProtocolError("VIDEO_PHASE_CHILD_FAILED")
        value = _validate_value(phase, result["value"])
        if phase == "export" and not value:
            _require_output(task.outfile)
        if phase == "download" and not value and not task.outfile:
            raise VideoPhaseProtocolError("VIDEO_PHASE_OUTPUT_PATH_MISSING")
        return value


def _validate_request(request: dict[str, Any]) -> None:
    task = request.get("task")
    if (
        request.get("version") != PROTOCOL_VERSION
        or request.get("phase") not in PHASES
        or not isinstance(request.get("nonce"), str)
        or not re.fullmatch(r"[a-f0-9]{32}", request["nonce"])
        or type(request.get("parent_pid")) is not int
        or request["parent_pid"] <= 0
        or not isinstance(task, dict)
        or not isinstance(task.get("draft_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task["draft_id"])
        or not isinstance(task.get("draft_url"), str)
        or not task["draft_url"]
        or (task.get("api_key") is not None and not isinstance(task["api_key"], str))
    ):
        raise VideoPhaseProtocolError("VIDEO_PHASE_REQUEST_INVALID")
    _validate_paths(task)


def _start_parent_watchdog(parent_pid: int) -> None:
    """Exit only this automation child if its owning server is forcibly killed."""
    def watch_parent() -> None:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
            if not handle:
                os._exit(70)
            try:
                # The wait is native and does not run any UI Automation operation.
                result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
                if result != 0x00000102:  # WAIT_TIMEOUT (not expected with INFINITE)
                    os._exit(70)
            finally:
                kernel32.CloseHandle(handle)
        else:
            while os.getppid() == parent_pid:
                time.sleep(0.5)
            os._exit(70)

    # Windows venv python.exe is a redirector: the interpreter's immediate
    # parent may be that launcher, rather than the server. Monitor the explicit
    # server handle above; do not reject a valid launcher ancestry here.
    if sys.platform != "win32" and os.getppid() != parent_pid:
        raise VideoPhaseProtocolError("VIDEO_PHASE_PARENT_MISMATCH")
    threading.Thread(target=watch_parent, name="video-phase-parent-watch", daemon=True).start()


def _probe_homepage(uia: Any) -> bool:
    """Conservative read-only 5.9 desktop probe; unknown/ambiguous state is not ready."""
    try:
        roots = [item for item in uia.GetRootControl().GetChildren() if not item.IsOffscreen]
        homes = [item for item in roots if item.Name == "剪映专业版" and "homepage" in item.ClassName.lower()]
        if len(homes) != 1:
            return False
        home = homes[0]
        if not home.IsEnabled:
            return False
        process_id = home.ProcessId
        if not process_id:
            return False
        # A separate editor, export window or modal belonging to this Jianying
        # process is unsafe even if the home page remains visible behind it.
        if any(item is not home and (item.ProcessId == process_id or item.Name == "剪映专业版") for item in roots):
            return False
        pending = [(child, 1) for child in home.GetChildren()]
        visited = 0
        while pending:
            item, depth = pending.pop()
            visited += 1
            if visited > 512:
                return False
            if item.IsOffscreen:
                continue
            class_name = item.ClassName.lower()
            if (item.ControlTypeName == "WindowControl"
                    or any(marker in class_name for marker in ("mainwindow", "dialog", "popup", "export"))):
                return False
            if depth < 3:
                pending.extend((child, depth + 1) for child in item.GetChildren())
        return True
    except Exception:
        # Includes an unavailable/locked desktop or stale COM elements. No retry
        # and no focus/click fallback; the parent gives this probe a hard timeout.
        return False


def _execute_request(request: dict[str, Any], status_path: Path) -> Any:
    phase = request["phase"]
    fields = request["task"]

    def publish(task: Any = None) -> None:
        current = fields if task is None else {
            "progress": task.progress, "outfile": task.outfile,
            "export_outfile_history": list(task.export_outfile_history),
        }
        _atomic_json(status_path, {
            **_identity(request),
            **{key: current[key] for key in ("progress", "outfile", "export_outfile_history")},
        })

    publish()
    if phase == "probe":
        if sys.platform != "win32":
            return False
        import uiautomation as uia
        with uia.UIAutomationInitializerInThread():
            return _probe_homepage(uia)

    # Deliberately lazy: module import and probe do not initialize a manager, and
    # none of these synchronous methods calls submit_task or starts the server.
    from datetime import datetime
    from src.utils.video_task_manager import TaskStatus, VideoGenTask, VideoGenTaskManager

    class PublishingTask(VideoGenTask):
        def __setattr__(self, name: str, value: Any) -> None:
            super().__setattr__(name, value)
            if name in {"progress", "outfile", "export_outfile_history"} and getattr(self, "_publish_ready", False):
                publish(self)

    task = PublishingTask(
        draft_url=fields["draft_url"], draft_id=fields["draft_id"],
        status=TaskStatus.PROCESSING, created_at=datetime.now(),
        api_key=fields["api_key"], progress=fields["progress"],
        outfile=fields["outfile"], export_outfile_history=list(fields["export_outfile_history"]),
    )
    task._publish_ready = True
    if phase == "upload":
        _require_output(task.outfile)
    manager = VideoGenTaskManager()
    method = {
        "download": manager._phase_download_and_prepare,
        "export": manager._phase_export_only,
        "upload": manager._phase_cos_upload_finalize,
    }[phase]
    try:
        value = _validate_value(phase, method(task))
        if phase == "export" and not value:
            _require_output(task.outfile)
        return value
    finally:
        publish(task)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 3:
        return 2
    request_path, status_path, result_path = map(Path, arguments)
    private_paths_valid = False
    try:
        request = _read_json(request_path)
        _validate_request(request)
        if (not (request_path.parent == status_path.parent == result_path.parent)
                or len({request_path, status_path, result_path}) != 3):
            raise VideoPhaseProtocolError("VIDEO_PHASE_PRIVATE_PATHS_INVALID")
        private_paths_valid = True
        _start_parent_watchdog(request["parent_pid"])
    except Exception as exc:
        if private_paths_valid:
            code = str(exc) if isinstance(exc, VideoPhaseProtocolError) else "VIDEO_PHASE_BOOTSTRAP_FAILED"
            if code not in CHILD_ERROR_CODES:
                code = "VIDEO_PHASE_BOOTSTRAP_FAILED"
            _atomic_json(result_path, {**_identity(request), "ok": False, "error": code})
        return 2
    try:
        value = _execute_request(request, status_path)
        api_key = request["task"]["api_key"]
        if isinstance(value, str):
            value = _redact(value, api_key)
        elif isinstance(value, (list, tuple)):
            value = [_redact(item, api_key) for item in value]
        _atomic_json(result_path, {**_identity(request), "ok": True, "value": value})
        return 0
    except Exception:
        # Raw exception text can contain a URL or credential. Keep the IPC error
        # bounded and credential-free, and never mistake it for a successful phase.
        _atomic_json(result_path, {**_identity(request), "ok": False, "error": "VIDEO_PHASE_CHILD_FAILED"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
