"""Offline Windows UI fakes: preserve the app when export evidence is missing."""

from unittest.mock import MagicMock, patch

import pytest

# Reuse the suite's explicit Windows/UI dependency fakes; no real GUI is opened.
from tests.test_jianying_controller_com import JianyingController
from src.pyJianYingDraft.exceptions import AutomationError

MODULE = "src.pyJianYingDraft.jianying_controller"


def _controller(status="pre_export", substatus="exporting"):
    ctrl = JianyingController.__new__(JianyingController)
    ctrl.app = MagicMock()
    ctrl.app_status = status
    ctrl.app_sub_status = substatus
    return ctrl


@pytest.mark.parametrize("path", [None, ""])
def test_no_path_never_waits_or_moves(path, tmp_path):
    ctrl = _controller()
    with patch(f"{MODULE}.shutil.move") as move:
        with pytest.raises(AutomationError, match="EXPORT_PATH_UNRESOLVED"):
            ctrl.move_exported_file(path, str(tmp_path / "out.mp4"))
        with pytest.raises(AutomationError, match="EXPORT_PATH_UNRESOLVED"):
            ctrl.wait_for_export_completion(300, path)
    move.assert_not_called()


def test_successful_59_export_still_clicks_semantic_success_button(tmp_path):
    ctrl = _controller()
    output = tmp_path / "output.mp4"
    output.write_bytes(b"nonempty-video")
    with (
        patch.object(ctrl, "get_window"),
        patch.object(ctrl, "_raise_if_export_resource_blocked"),
        patch.object(ctrl, "_find_export_succeed_close_btn", return_value=MagicMock()),
        patch.object(ctrl, "_safe_click") as click,
        patch(f"{MODULE}.time.sleep"),
        patch(f"{MODULE}.pyautogui.press") as press,
    ):
        assert ctrl.wait_for_export_completion(10, str(output))
    assert click.call_count == 1
    assert click.call_args.args[1] == "wait_for_export_completion.close_success"
    press.assert_not_called()


def test_success_signal_without_output_keeps_window_open(tmp_path):
    ctrl = _controller()
    with (
        patch.object(ctrl, "get_window"),
        patch.object(ctrl, "_raise_if_export_resource_blocked"),
        patch.object(ctrl, "_find_export_succeed_close_btn", return_value=MagicMock()),
        patch.object(ctrl, "_safe_click") as click,
        pytest.raises(AutomationError, match="EXPORT_OUTPUT_MISSING"),
    ):
        ctrl.wait_for_export_completion(300, str(tmp_path / "absent.mp4"))
    click.assert_not_called()


def test_resource_warning_never_clicks_continue_export(tmp_path):
    ctrl = _controller()
    with (
        patch.object(ctrl, "get_window"),
        patch.object(ctrl, "_exists_with_com_retry", return_value=True),
        patch.object(ctrl, "_safe_click") as safe_click,
        patch(f"{MODULE}.pyautogui.click") as click,
        patch(f"{MODULE}.pyautogui.press") as press,
        pytest.raises(AutomationError, match="EXPORT_RESOURCE_BLOCKED"),
    ):
        ctrl.wait_for_export_completion(300, str(tmp_path / "output.mp4"))
    safe_click.assert_not_called()
    click.assert_not_called()
    press.assert_not_called()


@pytest.mark.parametrize("text,is_blocked", [("继续导出", True), ("媒体格式不支持", True),
                                            ("导出文件缺失/损坏", True), ("正在导出", False)])
def test_warning_detector_uses_visible_error_semantics(text, is_blocked):
    ctrl = _controller()
    visible = MagicMock()
    visible.Name = text
    visible.GetPropertyValue.return_value = ""
    ctrl.app.TextControl.side_effect = lambda **kw: MagicMock(Exists=lambda *a: kw["Compare"](visible, 2))
    if is_blocked:
        with pytest.raises(AutomationError, match="EXPORT_RESOURCE_BLOCKED"):
            ctrl._raise_if_export_resource_blocked()
    else:
        ctrl._raise_if_export_resource_blocked()


def test_generic_close_button_is_not_export_success_evidence():
    ctrl = _controller()
    operations = []

    def exists(_factory, operation, **_kwargs):
        operations.append(operation)
        return ".name[" in operation  # Only a generic close would exist.

    with patch.object(ctrl, "_safe_exists", side_effect=exists):
        assert ctrl._find_export_succeed_close_btn() is None
    assert not any(".name[" in operation for operation in operations)


def test_59_semantic_success_control_is_still_found():
    ctrl = _controller()
    with patch.object(ctrl, "_safe_exists", side_effect=lambda _f, op, **kw: ".semantic" in op):
        assert ctrl._find_export_succeed_close_btn() is not None


def test_stalled_edit_page_fails_before_sixteen_export_attempts():
    ctrl = _controller("edit", "none")
    with (
        patch.object(ctrl, "get_window"),
        patch.object(ctrl, "switch_to_home"),
        patch.object(ctrl, "_JianyingController__ensure_window_focus"),
        patch.object(ctrl, "click_export_button") as export,
        patch.object(ctrl, "return_to_home") as home,
        patch.object(ctrl, "move_exported_file") as move,
        pytest.raises(AutomationError, match="EXPORT_STATE_STALLED"),
    ):
        ctrl.export_draft("test", "output.mp4")
    assert export.call_count == 3
    home.assert_not_called()
    move.assert_not_called()


def test_exporting_without_reading_path_fails_immediately_and_keeps_app():
    ctrl = _controller()
    with (
        patch.object(ctrl, "get_window"),
        patch.object(ctrl, "switch_to_home"),
        patch.object(ctrl, "_JianyingController__ensure_window_focus"),
        patch.object(ctrl, "_raise_if_export_resource_blocked"),
        patch.object(ctrl, "return_to_home") as home,
        patch.object(ctrl, "move_exported_file") as move,
        pytest.raises(AutomationError, match="EXPORT_PATH_UNRESOLVED"),
    ):
        ctrl.export_draft("test", "output.mp4")
    home.assert_not_called()
    move.assert_not_called()


def test_existing_success_page_without_this_attempt_path_is_not_moved_or_dismissed():
    ctrl = _controller("pre_export", "export_succeed")
    with (
        patch.object(ctrl, "get_window"),
        patch.object(ctrl, "switch_to_home"),
        patch.object(ctrl, "_JianyingController__ensure_window_focus"),
        patch.object(ctrl, "_raise_if_export_resource_blocked"),
        patch.object(ctrl, "return_to_home") as home,
        patch.object(ctrl, "move_exported_file") as move,
        pytest.raises(AutomationError, match="EXPORT_PATH_UNRESOLVED"),
    ):
        ctrl.export_draft("test", "output.mp4")
    home.assert_not_called()
    move.assert_not_called()


def test_controller_happy_path_verifies_then_returns_and_moves(tmp_path):
    ctrl = _controller("home", "none")
    output = tmp_path / "source.mp4"
    output.write_bytes(b"nonempty-video")

    def state(status, substatus):
        ctrl.app_status, ctrl.app_sub_status = status, substatus

    with (
        patch.object(ctrl, "get_window"),
        patch.object(ctrl, "switch_to_home"),
        patch.object(ctrl, "_JianyingController__ensure_window_focus"),
        patch.object(ctrl, "find_and_click_draft", side_effect=lambda *a, **kw: state("edit", "none")),
        patch.object(ctrl, "retry_failed_audio_downloads"),
        patch.object(ctrl, "click_export_button", side_effect=lambda: state("pre_export", "export_start")),
        patch.object(ctrl, "_raise_if_export_resource_blocked"),
        patch.object(ctrl, "get_original_export_path", return_value=str(output)),
        patch.object(ctrl, "set_export_resolution"),
        patch.object(ctrl, "set_export_framerate"),
        patch.object(ctrl, "click_final_export_button", side_effect=lambda: state("pre_export", "exporting")),
        patch.object(ctrl, "wait_for_export_completion", return_value=True),
        patch.object(ctrl, "return_to_home") as home,
        patch.object(ctrl, "move_exported_file") as move,
    ):
        ctrl.export_draft("test", "destination.mp4")
    home.assert_called_once()
    move.assert_called_once_with(str(output), "destination.mp4")
