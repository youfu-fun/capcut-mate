"""JianyingController 对 UI Automation COM 瞬时错误的容错。"""
import json
import sys
from unittest.mock import MagicMock, PropertyMock, patch

# jianying_controller 在导入时会加载 Windows 专用依赖，测试环境用 mock 占位
sys.modules.setdefault("uiautomation", MagicMock())
sys.modules.setdefault("pyautogui", MagicMock())
_logger_module = MagicMock()
_logger_module.logger = MagicMock()
sys.modules.setdefault("src.utils.logger", _logger_module)

import _ctypes
import pytest
from PIL import Image, ImageDraw

if not hasattr(_ctypes, "COMError"):
    class _TestCOMError(Exception):
        pass

    _ctypes.COMError = _TestCOMError  # type: ignore[attr-defined]

_original_platform = sys.platform
try:
    sys.platform = "win32"
    from src.pyJianYingDraft.jianying_controller import (
        COM_UIA_ERROR_HRESULT,
        COM_UIA_ERROR_MARKER,
        JianyingController,
        is_com_uia_error,
    )
finally:
    sys.platform = _original_platform


class TestIsComUiaError:
    def test_recognizes_com_error_by_hresult(self) -> None:
        exc = _ctypes.COMError(COM_UIA_ERROR_HRESULT, COM_UIA_ERROR_MARKER, None)
        assert is_com_uia_error(exc) is True

    def test_recognizes_com_error_by_message_string(self) -> None:
        exc = Exception(
            f"({COM_UIA_ERROR_HRESULT}, '{COM_UIA_ERROR_MARKER}', (None, None, None, 0, None))"
        )
        assert is_com_uia_error(exc) is True

    def test_ignores_unrelated_errors(self) -> None:
        assert is_com_uia_error(Exception("导出超时")) is False


class TestJianyingWindowCmp:
    def test_returns_false_when_class_name_com_error(self) -> None:
        ctrl = JianyingController.__new__(JianyingController)
        control = MagicMock()
        type(control).Name = PropertyMock(return_value="剪映专业版")
        type(control).ClassName = PropertyMock(
            side_effect=_ctypes.COMError(
                COM_UIA_ERROR_HRESULT, COM_UIA_ERROR_MARKER, None
            )
        )

        assert ctrl._JianyingController__jianying_window_cmp(control, 1) is False

    def test_returns_false_when_name_com_error(self) -> None:
        ctrl = JianyingController.__new__(JianyingController)
        control = MagicMock()
        type(control).Name = PropertyMock(
            side_effect=_ctypes.COMError(
                COM_UIA_ERROR_HRESULT, COM_UIA_ERROR_MARKER, None
            )
        )

        assert ctrl._JianyingController__jianying_window_cmp(control, 1) is False

    def test_matches_home_page_window(self) -> None:
        ctrl = JianyingController.__new__(JianyingController)
        control = MagicMock()
        type(control).Name = PropertyMock(return_value="剪映专业版")
        type(control).ClassName = PropertyMock(return_value="HomePageWindow")

        assert ctrl._JianyingController__jianying_window_cmp(control, 1) is True
        assert ctrl.app_status == "home"


class TestExistsWithComRetry:
    def test_retries_then_succeeds(self) -> None:
        ctrl = JianyingController.__new__(JianyingController)
        control = MagicMock()
        control.Exists.side_effect = [
            _ctypes.COMError(COM_UIA_ERROR_HRESULT, COM_UIA_ERROR_MARKER, None),
            True,
        ]

        with patch(
            "src.pyJianYingDraft.jianying_controller.time.sleep"
        ) as m_sleep:
            assert (
                ctrl._exists_with_com_retry(control, "test", timeout=0) is True
            )

        assert control.Exists.call_count == 2
        m_sleep.assert_called_once()

    def test_returns_false_when_raise_on_exhausted_false(self) -> None:
        ctrl = JianyingController.__new__(JianyingController)
        control = MagicMock()
        control.Exists.side_effect = _ctypes.COMError(
            COM_UIA_ERROR_HRESULT, COM_UIA_ERROR_MARKER, None
        )

        with patch("src.pyJianYingDraft.jianying_controller.time.sleep"):
            assert (
                ctrl._exists_with_com_retry(
                    control,
                    "test",
                    timeout=0,
                    max_retries=2,
                    raise_on_exhausted=False,
                )
                is False
            )

        assert control.Exists.call_count == 2


class TestJianying59AudioDownloadRetry:
    def test_matches_audio_download_failure_text(self) -> None:
        control = type(
            "FakeControl",
            (),
            {
                "Name": "音乐下载失败，点击重试",
                "GetPropertyValue": lambda self, _property_id: "",
            },
        )()

        assert JianyingController._audio_download_retry_cmp(control, 3) is True

    def test_ignores_unrelated_retry_text(self) -> None:
        control = type(
            "FakeControl",
            (),
            {
                "Name": "导出失败",
                "GetPropertyValue": lambda self, _property_id: "",
            },
        )()

        assert JianyingController._audio_download_retry_cmp(control, 3) is False

    def test_finds_retry_icons_in_self_drawn_timeline(self) -> None:
        image = Image.new("RGB", (1000, 600), (20, 24, 30))
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 260, 900, 330), fill=(8, 29, 49))
        draw.rectangle((350, 360, 650, 430), fill=(24, 39, 53))
        draw.ellipse((100, 280, 124, 304), outline=(220, 35, 35), width=5)
        draw.ellipse((370, 380, 394, 404), outline=(220, 35, 35), width=5)

        points = JianyingController._find_visual_audio_retry_points(image)

        assert points == [(112, 292), (382, 392)]

    def test_finds_visible_timeline_clip_centers(self) -> None:
        image = Image.new("RGB", (1200, 800), (20, 24, 30))
        draw = ImageDraw.Draw(image)
        draw.rectangle((180, 450, 720, 505), fill=(20, 82, 94))
        draw.rectangle((260, 575, 640, 625), fill=(35, 52, 76))

        points = JianyingController._find_visual_timeline_clip_points(image)

        assert (450, 477) in points
        assert (450, 600) in points

    def test_counts_only_native_audio_resources(self, tmp_path) -> None:
        (tmp_path / "draft_content.json").write_text(
            json.dumps(
                {
                    "materials": {
                        "audios": [
                            {"effect_id": "sound-1"},
                            {"music_id": "music-1"},
                            {"resource_id": "resource-1"},
                            {"path": "C:/voice.mp3"},
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        assert JianyingController._count_native_audio_resources(str(tmp_path)) == 3

    def test_counts_native_sticker_resources(self, tmp_path) -> None:
        (tmp_path / "draft_content.json").write_text(
            json.dumps(
                {
                    "materials": {
                        "stickers": [
                            {"sticker_id": "sticker-1"},
                            {"resource_id": "sticker-2"},
                            {"path": "C:/local.png"},
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        assert JianyingController._count_native_sticker_resources(str(tmp_path)) == 2

    def test_scans_timeline_when_draft_has_only_native_sticker(self, tmp_path) -> None:
        (tmp_path / "draft_content.json").write_text(
            json.dumps({"materials": {"stickers": [{"sticker_id": "sticker-1"}]}}),
            encoding="utf-8",
        )
        ctrl = JianyingController.__new__(JianyingController)
        signature = bytes([10]) * (64 * 24)

        with (
            patch.object(ctrl, "_scroll_timeline") as scroll_timeline,
            patch.object(
                ctrl,
                "_find_visual_timeline_clip_points_on_screen",
                return_value=[(450, 600)],
            ),
            patch.object(ctrl, "_retry_visible_audio_downloads"),
            patch.object(
                ctrl,
                "_get_timeline_view_signature",
                side_effect=[signature, signature],
            ),
            patch("src.pyJianYingDraft.jianying_controller.pyautogui.click") as click,
            patch("src.pyJianYingDraft.jianying_controller.time.sleep"),
        ):
            ctrl.retry_failed_audio_downloads(draft_dir=str(tmp_path))

        assert click.call_count == 1
        assert scroll_timeline.call_count == 3

    def test_scrolls_all_timeline_pages_and_restores_top(self, tmp_path) -> None:
        (tmp_path / "draft_content.json").write_text(
            json.dumps({"materials": {"audios": [{"effect_id": "sound-1"}]}}),
            encoding="utf-8",
        )
        ctrl = JianyingController.__new__(JianyingController)
        signature_a = bytes([10]) * (64 * 24)
        signature_b = bytes([40]) * (64 * 24)

        with (
            patch.object(ctrl, "_scroll_timeline") as scroll_timeline,
            patch.object(
                ctrl,
                "_find_visual_timeline_clip_points_on_screen",
                return_value=[(450, 600)],
            ),
            patch.object(ctrl, "_retry_visible_audio_downloads") as retry_visible,
            patch.object(
                ctrl,
                "_get_timeline_view_signature",
                side_effect=[signature_a, signature_b, signature_b, signature_b],
            ),
            patch(
                "src.pyJianYingDraft.jianying_controller.pyautogui.click"
            ) as click,
            patch("src.pyJianYingDraft.jianying_controller.time.sleep"),
        ):
            ctrl.retry_failed_audio_downloads(draft_dir=str(tmp_path))

        assert click.call_count == 2
        assert retry_visible.call_count == 2
        assert [call.args[0] for call in scroll_timeline.call_args_list] == [
            120,
            -6,
            -6,
            120,
        ]

    def test_retries_each_failed_audio_until_all_recover(self) -> None:
        ctrl = JianyingController.__new__(JianyingController)
        ctrl.app_status = "edit"
        failed_audio = MagicMock()

        with (
            patch.object(
                ctrl,
                "_find_audio_download_retry_control",
                side_effect=[failed_audio, failed_audio, None],
            ),
            patch.object(
                ctrl,
                "_find_visual_audio_retry_points_on_screen",
                return_value=[],
            ),
            patch.object(ctrl, "_safe_click") as safe_click,
            patch.object(ctrl, "get_window"),
            patch("src.pyJianYingDraft.jianying_controller.time.sleep"),
        ):
            ctrl.retry_failed_audio_downloads()

        assert safe_click.call_count == 2

    def test_blocks_export_when_audio_download_never_recovers(self) -> None:
        ctrl = JianyingController.__new__(JianyingController)
        ctrl.app_status = "edit"
        failed_audio = MagicMock()

        with (
            patch.object(
                ctrl,
                "_find_audio_download_retry_control",
                return_value=failed_audio,
            ),
            patch.object(ctrl, "_safe_click"),
            patch.object(ctrl, "get_window"),
            patch("src.pyJianYingDraft.jianying_controller.time.sleep"),
            pytest.raises(Exception, match="音频下载持续失败"),
        ):
            ctrl.retry_failed_audio_downloads()
