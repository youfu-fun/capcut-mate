"""Phase IPC and read-only desktop readiness contracts; no real UI or network."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.utils import video_phase_process as bridge


def make_task(tmp_path):
    return SimpleNamespace(
        draft_url="https://example.invalid/draft?draft_id=test_draft",
        draft_id="test_draft", api_key="test-secret-not-for-logs",
        progress=20, outfile=str(tmp_path / "output.mp4"),
        export_outfile_history=[], status="processing", video_url="",
    )


def make_request(task, phase="download"):
    return {
        "version": 1, "nonce": "a" * 32, "parent_pid": os.getpid(), "phase": phase,
        "task": {key: getattr(task, key) for key in (
            "draft_url", "draft_id", "api_key", "progress", "outfile", "export_outfile_history"
        )},
    }


def write_child_documents(command, value, *, status_overrides=None, result_overrides=None):
    request_path, status_path, result_path = map(Path, command[-3:])
    request = bridge._read_json(request_path)
    status = {
        **bridge._identity(request), "progress": 70, "outfile": request["task"]["outfile"],
        "export_outfile_history": [request["task"]["outfile"]],
        **(status_overrides or {}),
    }
    result = {**bridge._identity(request), "ok": True, "value": value, **(result_overrides or {})}
    bridge._atomic_json(status_path, status)
    bridge._atomic_json(result_path, result)
    return request, status, result


class FakeRunner:
    def __init__(self, value="", **document_options):
        self.value = value
        self.document_options = document_options
        self.calls = []

    async def run(self, command, *, timeout, on_poll=None, cwd=None, env=None):
        self.calls.append((command, timeout, cwd))
        assert env["CAPCUT_MATE_PHASE_WORKER"] == "1"
        write_child_documents(command, self.value, **self.document_options)
        if on_poll:
            on_poll()


@pytest.mark.asyncio
async def test_private_request_and_allowlisted_progress(tmp_path):
    task = make_task(tmp_path)
    runner = FakeRunner("download failed safely")
    assert await bridge.run_video_phase(runner, task, "download", 10) == "download failed safely"
    command, timeout, cwd = runner.calls[0]
    assert command[:3] == [sys.executable, "-m", "src.utils.video_phase_process"]
    assert task.api_key not in " ".join(command)
    assert task.draft_url not in " ".join(command)
    assert len(command) == 6
    assert timeout == 10
    assert Path(cwd) == bridge.REPOSITORY_ROOT
    assert not Path(command[-3]).parent.exists()
    assert task.progress == 70
    assert task.outfile == str(tmp_path / "output.mp4")
    assert task.export_outfile_history == [task.outfile]
    assert task.status == "processing"
    assert task.video_url == ""


@pytest.mark.asyncio
async def test_temp_request_permissions_and_no_key_in_public_documents(tmp_path):
    task = make_task(tmp_path)

    class InspectingRunner:
        async def run(self, command, **kwargs):
            request_path = Path(command[-3])
            if os.name != "nt":
                assert request_path.parent.stat().st_mode & 0o777 == 0o700
                assert request_path.stat().st_mode & 0o777 == 0o600
            request, status, result = write_child_documents(command, "")
            assert request["task"]["api_key"] == task.api_key
            assert task.api_key not in json.dumps(status)
            assert task.api_key not in json.dumps(result)

    assert await bridge.run_video_phase(InspectingRunner(), task, "download", 10) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("phase", "upload"), ("draft_id", "another"), ("nonce", "b" * 32)])
async def test_wrong_result_identity_rejected(tmp_path, field, value):
    runner = FakeRunner(result_overrides={field: value})
    with pytest.raises(bridge.VideoPhaseProtocolError, match="IDENTITY_MISMATCH"):
        await bridge.run_video_phase(runner, make_task(tmp_path), "download", 10)


@pytest.mark.asyncio
async def test_child_cannot_write_terminal_status(tmp_path):
    task = make_task(tmp_path)
    runner = FakeRunner(status_overrides={"status": "completed"})
    with pytest.raises(bridge.VideoPhaseProtocolError, match="STATUS_INVALID"):
        await bridge.run_video_phase(runner, task, "download", 10)
    assert task.status == "processing"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, True, 0, {}, []])
async def test_invalid_download_result_is_not_success(tmp_path, value):
    with pytest.raises(bridge.VideoPhaseProtocolError, match="RESULT_INVALID"):
        await bridge.run_video_phase(FakeRunner(value), make_task(tmp_path), "download", 10)


@pytest.mark.asyncio
async def test_child_failure_not_treated_as_success(tmp_path):
    with pytest.raises(bridge.VideoPhaseProtocolError, match="CHILD_FAILED"):
        await bridge.run_video_phase(
            FakeRunner(result_overrides={"ok": False}), make_task(tmp_path), "download", 10
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [["", ""], [None, ""], ["https://example.invalid/video.mp4"], "url"])
async def test_invalid_upload_result_rejected(tmp_path, value):
    with pytest.raises(bridge.VideoPhaseProtocolError, match="RESULT_INVALID"):
        await bridge.run_video_phase(FakeRunner(value), make_task(tmp_path), "upload", 10)


@pytest.mark.asyncio
async def test_upload_success_and_error_preserve_contract(tmp_path):
    result = await bridge.run_video_phase(
        FakeRunner(["https://example.invalid/video.mp4", ""]), make_task(tmp_path), "upload", 10
    )
    assert result == ("https://example.invalid/video.mp4", "")
    error = await bridge.run_video_phase(FakeRunner(["", "upload failed"]), make_task(tmp_path), "upload", 10)
    assert error == ("", "upload failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("exists", [False, True])
async def test_export_missing_or_empty_output_rejected(tmp_path, exists):
    task = make_task(tmp_path)
    if exists:
        Path(task.outfile).touch()
    with pytest.raises(bridge.VideoPhaseProtocolError, match="OUTPUT_MISSING"):
        await bridge.run_video_phase(FakeRunner(), task, "export", 10)


@pytest.mark.asyncio
async def test_export_nonempty_output_and_probe_bool(tmp_path):
    task = make_task(tmp_path)
    Path(task.outfile).write_bytes(b"test-output")
    assert await bridge.run_video_phase(FakeRunner(), task, "export", 10) == ""
    assert await bridge.run_video_phase(FakeRunner(False), task, "probe", 10) is False
    with pytest.raises(bridge.VideoPhaseProtocolError, match="RESULT_INVALID"):
        await bridge.run_video_phase(FakeRunner("false"), task, "probe", 10)


@pytest.mark.asyncio
async def test_missing_child_result_is_not_success(tmp_path):
    class MissingRunner:
        async def run(self, *args, **kwargs):
            return None

    with pytest.raises(bridge.VideoPhaseProtocolError):
        await bridge.run_video_phase(MissingRunner(), make_task(tmp_path), "download", 10)


@pytest.mark.asyncio
async def test_timeout_keeps_last_paths_without_changing_terminal_state(tmp_path):
    task = make_task(tmp_path)

    class TimeoutRunner:
        async def run(self, command, **kwargs):
            write_child_documents(command, "", status_overrides={"outfile": str(tmp_path / "retry.mp4")})
            raise TimeoutError("phase expired")

    with pytest.raises(TimeoutError, match="phase expired"):
        await bridge.run_video_phase(TimeoutRunner(), task, "export", 10)
    assert task.outfile == str(tmp_path / "retry.mp4")
    assert task.status == "processing"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,timeout", [("invalid", 10), ("download", 0), ("export", float("inf")), ("export", True)])
async def test_bad_call_does_not_spawn(tmp_path, phase, timeout):
    runner = FakeRunner()
    with pytest.raises(ValueError):
        await bridge.run_video_phase(runner, make_task(tmp_path), phase, timeout)
    assert not runner.calls


def cli_files(tmp_path, phase="download"):
    task = make_task(tmp_path)
    request = make_request(task, phase)
    paths = [tmp_path / name for name in ("request.json", "status.json", "result.json")]
    bridge._atomic_json(paths[0], request)
    return task, request, paths


def test_cli_redacts_credentials_in_result(tmp_path, monkeypatch):
    task, request, paths = cli_files(tmp_path)
    monkeypatch.setattr(bridge, "_start_parent_watchdog", lambda pid: None)
    monkeypatch.setattr(bridge, "_execute_request", lambda req, status: f"failure with {task.api_key}")
    assert bridge.main(list(map(str, paths))) == 0
    result_text = paths[2].read_text()
    assert task.api_key not in result_text
    assert "[redacted]" in result_text
    assert json.loads(result_text)["value"].startswith("failure")


def test_cli_unhandled_error_does_not_leak_credential(tmp_path, monkeypatch):
    task, _, paths = cli_files(tmp_path)
    monkeypatch.setattr(bridge, "_start_parent_watchdog", lambda pid: None)

    def fail(*args):
        raise RuntimeError(f"secret was {task.api_key}")

    monkeypatch.setattr(bridge, "_execute_request", fail)
    assert bridge.main(list(map(str, paths))) == 1
    result = paths[2].read_text()
    assert task.api_key not in result
    assert json.loads(result)["ok"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="Real Windows download would access network")
def test_actual_module_dispatch_nonwindows_download_without_network(tmp_path):
    task, _, paths = cli_files(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "src.utils.video_phase_process", *map(str, paths)],
        cwd=bridge.REPOSITORY_ROOT, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = bridge._read_json(paths[2])
    assert result["ok"] is True
    assert "Windows" in result["value"]
    assert task.api_key not in completed.stdout + completed.stderr + paths[2].read_text() + paths[1].read_text()
    status = bridge._read_json(paths[1])
    assert status["outfile"].endswith(".mp4")
    assert status["progress"] == 20


def test_invalid_request_cannot_dispatch(tmp_path, monkeypatch):
    _, request, paths = cli_files(tmp_path)
    request["task"]["draft_id"] = "../outside"
    bridge._atomic_json(paths[0], request)
    called = []
    monkeypatch.setattr(bridge, "_execute_request", lambda *args: called.append(True))
    assert bridge.main(list(map(str, paths))) == 2
    assert not called


def control(name="", class_name="", pid=100, children=None, offscreen=False, enabled=True, type_name="PaneControl"):
    return SimpleNamespace(
        Name=name, ClassName=class_name, ProcessId=pid, IsOffscreen=offscreen,
        IsEnabled=enabled, ControlTypeName=type_name, GetChildren=lambda: children or [],
    )


def probe(*roots):
    return bridge._probe_homepage(SimpleNamespace(GetRootControl=lambda: control(children=list(roots))))


def home(**kwargs):
    return control("剪映专业版", "JianyingHomepage", **kwargs)


def test_probe_unique_homepage_without_any_ui_mutation_methods():
    assert probe(home(), control(name="Other app", pid=200)) is True


@pytest.mark.parametrize("roots", [
    [], [home(), home(pid=101)],
    [home(), control(name="剪映专业版", class_name="MainWindow")],
    [home(), control(name="剪映专业版", class_name="MainWindow", pid=200)],
    [home(), control(name="导出", class_name="ExportDialog")],
    [home(enabled=False)],
    [home(children=[control(class_name="PopupDialog")])],
    [home(children=[control(type_name="WindowControl")])],
])
def test_probe_rejects_editor_export_modal_or_ambiguous_desktop(roots):
    assert probe(*roots) is False


def test_probe_ignores_hidden_editor_but_not_a_visible_one():
    assert probe(home(), control(class_name="MainWindow", offscreen=True)) is True


def test_probe_fail_closed_on_uia_error():
    def fail():
        raise RuntimeError("COM element unavailable")

    assert bridge._probe_homepage(SimpleNamespace(GetRootControl=fail)) is False


def test_watchdog_rejects_wrong_parent_before_starting_thread(monkeypatch):
    started = []
    monkeypatch.setattr(bridge.sys, "platform", "linux")
    monkeypatch.setattr(bridge.threading, "Thread", lambda **kwargs: started.append(kwargs))
    with pytest.raises(bridge.VideoPhaseProtocolError, match="PARENT_MISMATCH"):
        bridge._start_parent_watchdog(os.getppid() + 1)
    assert not started


@pytest.mark.asyncio
async def test_child_bootstrap_error_is_returned_instead_of_bare_exit_code(tmp_path):
    from src.utils.isolated_process import ProcessFailed

    class FailedRunner:
        async def run(self, command, **kwargs):
            _, _, result_path = map(Path, command[-3:])
            request = bridge._read_json(Path(command[-3]))
            bridge._atomic_json(result_path, {
                **bridge._identity(request), "ok": False,
                "error": "VIDEO_PHASE_PARENT_MISMATCH",
            })
            raise ProcessFailed(123, 2)

    with pytest.raises(bridge.VideoPhaseProtocolError, match="VIDEO_PHASE_PARENT_MISMATCH"):
        await bridge.run_video_phase(FailedRunner(), make_task(tmp_path), "download", 10)


def test_bootstrap_failure_records_safe_reason(tmp_path, monkeypatch):
    request = make_request(make_task(tmp_path))
    request_path, status_path, result_path = (tmp_path / name for name in ("request", "status", "result"))
    bridge._atomic_json(request_path, request)
    def mismatch(parent_pid):
        raise bridge.VideoPhaseProtocolError("VIDEO_PHASE_PARENT_MISMATCH")
    monkeypatch.setattr(bridge, "_start_parent_watchdog", mismatch)
    assert bridge.main([str(request_path), str(status_path), str(result_path)]) == 2
    result = bridge._read_json(result_path)
    assert result["error"] == "VIDEO_PHASE_PARENT_MISMATCH"
    assert request["task"]["api_key"] not in result_path.read_text()


@pytest.mark.parametrize("handle", [0, 4242])
@pytest.mark.parametrize("immediate_parent", [12345, 54321])
def test_windows_watchdog_waits_on_owned_parent_and_exits_only_child(monkeypatch, handle, immediate_parent):
    import ctypes

    class ChildExited(BaseException):
        pass

    calls = []

    class NativeFunction:
        def __init__(self, name, result):
            self.name, self.result = name, result

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.result

    kernel32 = SimpleNamespace(
        OpenProcess=NativeFunction("open", handle),
        WaitForSingleObject=NativeFunction("wait", 0),
        CloseHandle=NativeFunction("close", 1),
    )

    def exit_child(code):
        calls.append(("exit", (code,)))
        raise ChildExited(code)

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel32, raising=False)
    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge.os, "getppid", lambda: immediate_parent)
    monkeypatch.setattr(bridge.os, "_exit", exit_child)
    monkeypatch.setattr(
        bridge.threading, "Thread",
        lambda *, target, **kwargs: SimpleNamespace(start=target),
    )
    with pytest.raises(ChildExited, match="70"):
        bridge._start_parent_watchdog(12345)
    assert calls[0] == ("open", (0x00100000, False, 12345))
    assert ("exit", (70,)) in calls
    if handle:
        assert ("wait", (4242, 0xFFFFFFFF)) in calls
        # Our fake exit raises, so the finally block is observable here; the real
        # OS closes all handles on os._exit without running Python cleanup.
        assert ("close", (4242,)) in calls
    else:
        assert not any(name in {"wait", "close"} for name, _ in calls)


@pytest.mark.skipif(os.name == "nt", reason="The real orphan-process test uses POSIX select and SIGKILL")
def test_real_watchdog_exits_after_owning_parent_is_force_killed():
    import select

    token = "capcut-watchdog-test-" + uuid4().hex
    child_code = f"""
# {token}
import os, threading
from src.utils import video_phase_process as bridge
original_exit = os._exit
def observed_exit(code):
    try:
        os.write(1, ('WATCHDOG_EXIT:%s\\n' % code).encode())
    finally:
        original_exit(code)
bridge.os._exit = observed_exit
bridge._start_parent_watchdog(os.getppid())
print('CHILD_READY', flush=True)
threading.Event().wait()
"""
    parent_code = f"""
import subprocess, sys, threading
child = subprocess.Popen([sys.executable, '-u', '-c', {child_code!r}])
print('CHILD_PID:%s' % child.pid, flush=True)
threading.Event().wait()
"""
    parent = subprocess.Popen(
        [sys.executable, "-u", "-c", parent_code],
        cwd=bridge.REPOSITORY_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "CAPCUT_MATE_PHASE_WORKER": "1"},
    )
    output = b""
    child_pid = None
    child_exit_observed = False
    try:
        deadline = time.monotonic() + 10
        while b"CHILD_READY\n" not in output and time.monotonic() < deadline:
            ready, _, _ = select.select([parent.stdout], [], [], 0.1)
            if ready:
                chunk = os.read(parent.stdout.fileno(), 4096)
                if not chunk:
                    break
                output += chunk
                match = re.search(rb"CHILD_PID:(\d+)", output)
                if match:
                    child_pid = int(match.group(1))
        assert b"CHILD_READY\n" in output, output.decode(errors="replace")
        assert child_pid is not None
        # Kill exactly the test-owned Popen process, not the server, Python by
        # name, or a process tree. Its automation child must terminate itself.
        parent.kill()
        remaining, _ = parent.communicate(timeout=5)
        output += remaining
        child_exit_observed = b"WATCHDOG_EXIT:70\n" in output
        assert parent.returncode == -signal.SIGKILL
        assert child_exit_observed, output.decode(errors="replace")
        # communicate reached EOF although the child inherited stdout: the
        # orphaned child exited and closed its descriptor, rather than hanging.
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        if child_pid is not None and not child_exit_observed:
            # Only test cleanup: validate the unique helper token before killing
            # an orphan PID, protecting against accidental PID reuse.
            command = subprocess.run(
                ["ps", "-p", str(child_pid), "-o", "args="],
                capture_output=True, text=True, timeout=3,
            ).stdout
            if token in command:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if parent.stdout is not None:
            parent.stdout.close()
