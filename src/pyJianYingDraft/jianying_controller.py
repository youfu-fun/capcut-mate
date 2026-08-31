# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Modified by Hommy <taohongmin@sina.cn> on 2026-06-12
"""剪映自动化控制，主要与自动导出有关"""

import _ctypes
import json
import os
import time
import shutil
import sys

# 平台检查和依赖导入
if sys.platform != "win32":
    raise ImportError("JianyingController is only available on Windows platform")

try:
    import uiautomation as uia
except ImportError as e:
    raise ImportError(f"Missing required Windows dependencies: {e}. Please install with: pip install capcut-mate[windows]")

try:
    import pyautogui  # pyright: ignore[reportMissingModuleSource]
except ImportError as e:
    raise ImportError(f"Missing required Windows dependencies: {e}. Please install with: pip install pyautogui[windows]")

from enum import Enum
from typing import Optional, Literal, Callable

from . import exceptions
from .exceptions import AutomationError

# 添加logger导入
from src.utils.logger import logger

# Windows UI Automation COM 错误（EVENT_E_ALL_SUBSCRIBERS_FAILED）
COM_UIA_ERROR_HRESULT = -2147220991
COM_UIA_ERROR_MARKER = "事件无法调用任何订户"
# UIA 遍历 UI 树时偶发（E_FAIL / 未指定的错误）
COM_E_FAIL_HRESULT = -2147467259
COM_E_FAIL_MARKER = "未指定的错误"
UIA_CLICK_MAX_RETRIES = 4
UIA_CLICK_RETRY_INTERVAL = 1.0
AUDIO_DOWNLOAD_MAX_RETRIES = 4
AUDIO_DOWNLOAD_RETRY_INTERVAL = 5.0
AUDIO_TIMELINE_MAX_PAGES = 12
AUDIO_TIMELINE_SCROLL_CLICKS = 6
AUDIO_TIMELINE_RESET_CLICKS = 120
AUDIO_TIMELINE_SCROLL_INTERVAL = 0.8
AUDIO_TIMELINE_ACTIVATION_INTERVAL = 1.5


def is_com_uia_error(exc: BaseException) -> bool:
    if isinstance(exc, _ctypes.COMError):
        args = getattr(exc, "args", ())
        if args and args[0] in (COM_UIA_ERROR_HRESULT, COM_E_FAIL_HRESULT):
            return True
        if len(args) >= 2:
            msg = str(args[1])
            if COM_UIA_ERROR_MARKER in msg or COM_E_FAIL_MARKER in msg:
                return True
    text = str(exc)
    return (
        str(COM_UIA_ERROR_HRESULT) in text
        or str(COM_E_FAIL_HRESULT) in text
        or COM_UIA_ERROR_MARKER in text
        or COM_E_FAIL_MARKER in text
    )


class ExportResolution(Enum):
    """导出分辨率"""
    RES_8K = "8K"
    RES_4K = "4K"
    RES_2K = "2K"
    RES_1080P = "1080P"
    RES_720P = "720P"
    RES_480P = "480P"

class ExportFramerate(Enum):
    """导出帧率"""
    FR_24 = "24fps"
    FR_25 = "25fps"
    FR_30 = "30fps"
    FR_50 = "50fps"
    FR_60 = "60fps"

class ControlFinder:
    """控件查找器，封装部分与控件查找相关的逻辑"""

    @staticmethod
    def desc_matcher(target_desc: str, depth: int = 2, exact: bool = False) -> Callable[[uia.Control, int], bool]:
        """根据full_description查找控件的匹配器"""
        target_desc = target_desc.lower()
        def matcher(control: uia.Control, _depth: int) -> bool:
            if _depth != depth:
                return False
            full_desc: str = control.GetPropertyValue(30159).lower()
            return (target_desc == full_desc) if exact else (target_desc in full_desc)
        return matcher

    @staticmethod
    def class_name_matcher(class_name: str, depth: int = 1, exact: bool = False) -> Callable[[uia.Control, int], bool]:
        """根据ClassName查找控件的匹配器"""
        class_name = class_name.lower()
        def matcher(control: uia.Control, _depth: int) -> bool:
            if _depth != depth:
                return False
            curr_class_name: str = control.ClassName.lower()
            return (class_name == curr_class_name) if exact else (class_name in curr_class_name)
        return matcher

class JianyingController:
    """剪映控制器"""

    # 窗口查找重试：剪映启动较慢、RDP 刚连上、或 UI 树尚未就绪时，瞬时 Exists(0) 易失败
    WINDOW_FIND_MAX_RETRIES = 12
    WINDOW_FIND_RETRY_INTERVAL = 1.0

    app: uia.WindowControl
    """剪映窗口"""
    app_status: Literal["home", "edit", "pre_export"]
    """当app_status为pre_export时，app_sub_status表示导出过程中的子状态"""
    app_sub_status: Literal["none", "export_start", "exporting", "export_succeed"]

    def __init__(self):
        """初始化剪映控制器, 此时剪映应该处于目录页"""
        self.get_window()

    def _safe_click(
        self,
        get_control: Callable[[], uia.Control],
        operation: str,
        *,
        exists_timeout: float = 1.0,
        max_retries: int = UIA_CLICK_MAX_RETRIES,
        retry_interval: float = UIA_CLICK_RETRY_INTERVAL,
    ) -> None:
        """带 COM 重试的控件点击；每次尝试重新查找控件，失效时刷新窗口。"""
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_retries + 1):
            try:
                control = get_control()
                if not control.Exists(exists_timeout, 0.5):
                    raise AutomationError(f"{operation}: control not found")
                control.Click(simulateMove=False)
                return
            except Exception as exc:
                last_exc = exc
                if not is_com_uia_error(exc) or attempt >= max_retries:
                    logger.error(
                        "UIA click failed: operation=%s attempt=%d/%d error=%r",
                        operation,
                        attempt,
                        max_retries,
                        exc,
                        exc_info=not is_com_uia_error(exc),
                    )
                    raise
                logger.warning(
                    "UIA COM error on click, retrying: operation=%s attempt=%d/%d",
                    operation,
                    attempt,
                    max_retries,
                )
                time.sleep(retry_interval)
                self.get_window()
        if last_exc is not None:
            raise last_exc

    def _exists_with_com_retry(
        self,
        control: uia.Control,
        operation: str,
        *,
        timeout: float = 0,
        max_retries: int = UIA_CLICK_MAX_RETRIES,
        retry_interval: float = UIA_CLICK_RETRY_INTERVAL,
        raise_on_exhausted: bool = True,
    ) -> bool:
        """对单个控件的 Exists 调用做 COM 重试；遍历 UI 树时偶发失效元素可由此消化。"""
        search_interval = 0.5 if timeout > 0 else 0
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_retries + 1):
            try:
                return control.Exists(timeout, search_interval)
            except Exception as exc:
                last_exc = exc
                if not is_com_uia_error(exc) or attempt >= max_retries:
                    logger.error(
                        "UIA Exists failed: operation=%s attempt=%d/%d error=%r",
                        operation,
                        attempt,
                        max_retries,
                        exc,
                        exc_info=not is_com_uia_error(exc),
                    )
                    if raise_on_exhausted:
                        raise
                    return False
                logger.warning(
                    "UIA COM error on Exists, retrying: operation=%s attempt=%d/%d",
                    operation,
                    attempt,
                    max_retries,
                )
                time.sleep(retry_interval)
        if last_exc is not None:
            if raise_on_exhausted:
                raise last_exc
            return False
        return False

    def _safe_exists(
        self,
        get_control: Callable[[], uia.Control],
        operation: str,
        *,
        timeout: float = 0.5,
        max_retries: int = UIA_CLICK_MAX_RETRIES,
        retry_interval: float = UIA_CLICK_RETRY_INTERVAL,
    ) -> bool:
        """带 COM 重试的控件 Exists 检测；每次尝试重新查找控件，失效时刷新窗口。"""
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_retries + 1):
            try:
                return get_control().Exists(timeout, 0.5)
            except Exception as exc:
                last_exc = exc
                if not is_com_uia_error(exc) or attempt >= max_retries:
                    logger.error(
                        "UIA Exists failed: operation=%s attempt=%d/%d error=%r",
                        operation,
                        attempt,
                        max_retries,
                        exc,
                        exc_info=not is_com_uia_error(exc),
                    )
                    raise
                logger.warning(
                    "UIA COM error on Exists, retrying: operation=%s attempt=%d/%d",
                    operation,
                    attempt,
                    max_retries,
                )
                time.sleep(retry_interval)
                self.get_window()
        if last_exc is not None:
            raise last_exc
        return False

    @staticmethod
    def _audio_download_retry_cmp(control: uia.Control, depth: int) -> bool:
        """匹配剪映 5.9 时间线上的内置音频下载失败/重试控件。"""
        if depth < 1:
            return False
        try:
            name = str(control.Name or "")
            full_desc = str(control.GetPropertyValue(30159) or "")
        except Exception as exc:
            if is_com_uia_error(exc):
                return False
            raise
        text = f"{name} {full_desc}".strip()
        return (
            (
                ("音频下载失败" in text or "音乐下载失败" in text)
                and "重试" in text
            )
            or text in ("重试", "点击重试", "重新下载")
        )

    def _make_audio_download_retry_control(self) -> uia.Control:
        return self.app.Control(
            searchDepth=12,
            Compare=self._audio_download_retry_cmp,
        )

    def _find_audio_download_retry_control(self) -> Optional[uia.Control]:
        control = self._make_audio_download_retry_control()
        if self._exists_with_com_retry(
            control,
            "find_audio_download_retry_control",
            timeout=0.5,
            raise_on_exhausted=False,
        ):
            return control
        return None

    def _require_audio_download_retry_control(self) -> uia.Control:
        control = self._find_audio_download_retry_control()
        if control is None:
            raise AutomationError("audio download retry control not found")
        return control

    @staticmethod
    def _find_visual_audio_retry_points(screenshot) -> list[tuple[int, int]]:
        """从剪映 5.9 时间轴截图中定位红色的音频下载重试图标。

        5.9 的时间轴由 Qt/QML 自绘，失败文案不会进入 UIA 控件树。重试图标
        是稳定的红色圆环，且位于深色轨道内；这里按颜色、尺寸、形状和上下
        背景联合过滤，避免把视频缩略图里的红色内容当成按钮。
        """
        image = screenshot.convert("RGB")
        original_width, original_height = image.size
        if original_width <= 0 or original_height <= 0:
            return []

        # 限制扫描宽度，兼顾 2K/4K 屏幕上的速度和图标识别精度。
        scale = min(1.0, 1600.0 / original_width)
        if scale < 1.0:
            image = image.resize(
                (
                    max(1, int(original_width * scale)),
                    max(1, int(original_height * scale)),
                )
            )

        width, height = image.size
        pixels = image.load()
        red_pixels: set[tuple[int, int]] = set()

        # 时间轴位于编辑窗口下半部分；排除顶部工具栏和窗口边缘。
        for y in range(int(height * 0.35), max(int(height * 0.35), height - 8)):
            for x in range(int(width * 0.05), max(int(width * 0.05), width - 8)):
                red, green, blue = pixels[x, y]
                if (
                    red >= 145
                    and green <= 115
                    and blue <= 115
                    and red - green >= 55
                    and red - blue >= 45
                ):
                    red_pixels.add((x, y))

        candidates: list[tuple[int, int]] = []
        while red_pixels:
            seed = red_pixels.pop()
            queue = [seed]
            xs = [seed[0]]
            ys = [seed[1]]
            for x, y in queue:
                for next_x in (x - 1, x, x + 1):
                    for next_y in (y - 1, y, y + 1):
                        point = (next_x, next_y)
                        if point in red_pixels:
                            red_pixels.remove(point)
                            queue.append(point)
                            xs.append(next_x)
                            ys.append(next_y)

            left, right = min(xs), max(xs)
            top, bottom = min(ys), max(ys)
            component_width = right - left + 1
            component_height = bottom - top + 1
            area = len(xs)
            aspect_ratio = component_width / component_height
            if not (
                6 <= component_width <= 40
                and 6 <= component_height <= 40
                and 0.65 <= aspect_ratio <= 1.45
                and area >= 12
                and area >= component_width * component_height * 0.18
            ):
                continue

            center_x = (left + right) // 2
            center_y = (top + bottom) // 2
            outside_offset = component_height // 2 + 4
            above_y = max(0, center_y - outside_offset)
            below_y = min(height - 1, center_y + outside_offset)
            above = pixels[center_x, above_y]
            below = pixels[center_x, below_y]
            if max(above) > 140 or max(below) > 140:
                continue

            candidates.append(
                (
                    int(round(center_x / scale)),
                    int(round(center_y / scale)),
                )
            )

        return sorted(candidates, key=lambda point: (point[1], point[0]))

    def _find_visual_audio_retry_points_on_screen(self) -> list[tuple[int, int]]:
        try:
            return self._find_visual_audio_retry_points(pyautogui.screenshot())
        except Exception as exc:
            logger.warning(
                "Unable to inspect Jianying timeline screenshot for audio retry: %r",
                exc,
            )
            return []

    @staticmethod
    def _count_native_audio_resources(draft_dir: Optional[str]) -> int:
        """统计草稿中需要剪映联网加载的内置 BGM/音效数量。"""
        if not draft_dir:
            return 0

        content_path = os.path.join(draft_dir, "draft_content.json")
        try:
            with open(content_path, "r", encoding="utf-8") as content_file:
                content = json.load(content_file)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                "Unable to inspect draft native audio resources: path=%s error=%r",
                content_path,
                exc,
            )
            return 0

        audios = content.get("materials", {}).get("audios", [])
        return sum(
            1
            for audio in audios
            if isinstance(audio, dict)
            and any(
                str(audio.get(field) or "").strip()
                for field in ("music_id", "effect_id", "resource_id")
            )
        )

    @staticmethod
    def _count_native_sticker_resources(draft_dir: Optional[str]) -> int:
        """统计草稿中需要剪映联网加载的内置贴纸数量。"""
        if not draft_dir:
            return 0

        content_path = os.path.join(draft_dir, "draft_content.json")
        try:
            with open(content_path, "r", encoding="utf-8") as content_file:
                content = json.load(content_file)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                "Unable to inspect draft native sticker resources: path=%s error=%r",
                content_path,
                exc,
            )
            return 0

        stickers = content.get("materials", {}).get("stickers", [])
        return sum(
            1
            for sticker in stickers
            if isinstance(sticker, dict)
            and any(
                str(sticker.get(field) or "").strip()
                for field in ("sticker_id", "resource_id")
            )
        )

    @staticmethod
    def _is_timeline_clip_pixel(red: int, green: int, blue: int) -> bool:
        """判断像素是否更像时间线片段，而不是深色空白背景。"""
        brightest = max(red, green, blue)
        darkest = min(red, green, blue)
        return 32 <= brightest <= 210 and brightest - darkest >= 12

    @classmethod
    def _find_visual_timeline_clip_points(
        cls, screenshot
    ) -> list[tuple[int, int]]:
        """从当前可见时间线中找出片段中心点。

        剪映 5.9 的时间线由 Qt/QML 自绘，音频片段通常不进入 UIA 控件树。
        这里按时间线中的有色横向矩形寻找片段；点击视频或字幕片段是无害的，
        但能确保可见的内置音频片段被激活并触发首次下载。
        """
        image = screenshot.convert("RGB")
        original_width, original_height = image.size
        if original_width <= 0 or original_height <= 0:
            return []

        scale = min(1.0, 1600.0 / original_width)
        if scale < 1.0:
            image = image.resize(
                (
                    max(1, int(original_width * scale)),
                    max(1, int(original_height * scale)),
                )
            )

        width, height = image.size
        pixels = image.load()
        left = int(width * 0.10)
        right = int(width * 0.96)
        top = int(height * 0.52)
        bottom = int(height * 0.94)
        min_row_pixels = max(24, int((right - left) * 0.025))

        active_rows: list[int] = []
        for y in range(top, bottom):
            count = 0
            for x in range(left, right):
                if cls._is_timeline_clip_pixel(*pixels[x, y]):
                    count += 1
            if count >= min_row_pixels:
                active_rows.append(y)

        row_bands: list[tuple[int, int]] = []
        for y in active_rows:
            if not row_bands or y > row_bands[-1][1] + 2:
                row_bands.append((y, y))
            else:
                row_bands[-1] = (row_bands[-1][0], y)

        candidates: list[tuple[int, int]] = []
        for band_top, band_bottom in row_bands:
            band_height = band_bottom - band_top + 1
            if band_height < 6 or band_height > int(height * 0.14):
                continue

            center_y = (band_top + band_bottom) // 2
            active_columns: list[int] = []
            required_band_pixels = max(2, int(band_height * 0.18))
            for x in range(left, right):
                count = 0
                for y in range(band_top, band_bottom + 1):
                    if cls._is_timeline_clip_pixel(*pixels[x, y]):
                        count += 1
                if count >= required_band_pixels:
                    active_columns.append(x)

            column_bands: list[tuple[int, int]] = []
            for x in active_columns:
                if not column_bands or x > column_bands[-1][1] + 5:
                    column_bands.append((x, x))
                else:
                    column_bands[-1] = (column_bands[-1][0], x)

            for band_left, band_right in column_bands:
                if band_right - band_left + 1 < 24:
                    continue
                candidates.append(
                    (
                        int(round(((band_left + band_right) // 2) / scale)),
                        int(round(center_y / scale)),
                    )
                )

        return candidates

    def _find_visual_timeline_clip_points_on_screen(
        self,
    ) -> list[tuple[int, int]]:
        try:
            return self._find_visual_timeline_clip_points(pyautogui.screenshot())
        except Exception as exc:
            logger.warning(
                "Unable to inspect Jianying timeline clips: %r",
                exc,
            )
            return []

    @staticmethod
    def _timeline_view_signature(screenshot) -> bytes:
        """生成时间线视口的低分辨率签名，用于判断是否已经滚到底。"""
        image = screenshot.convert("L")
        width, height = image.size
        viewport = image.crop(
            (
                int(width * 0.10),
                int(height * 0.52),
                int(width * 0.96),
                int(height * 0.94),
            )
        )
        return viewport.resize((64, 24)).tobytes()

    @staticmethod
    def _timeline_signatures_similar(first: bytes, second: bytes) -> bool:
        if not first or len(first) != len(second):
            return False
        average_difference = sum(
            abs(left - right) for left, right in zip(first, second)
        ) / len(first)
        return average_difference <= 1.5

    def _get_timeline_view_signature(self) -> bytes:
        try:
            return self._timeline_view_signature(pyautogui.screenshot())
        except Exception as exc:
            logger.warning("Unable to snapshot Jianying timeline: %r", exc)
            return b""

    def _scroll_timeline(self, clicks: int) -> None:
        screenshot = pyautogui.screenshot()
        width, height = screenshot.size
        pyautogui.moveTo(int(width * 0.72), int(height * 0.82))
        pyautogui.scroll(clicks)

    def _retry_visible_audio_downloads(self) -> bool:
        """重试当前可见页面上的音频下载；返回是否观察到过失败。"""
        saw_failure = False
        for attempt in range(1, AUDIO_DOWNLOAD_MAX_RETRIES + 1):
            retry_control = self._find_audio_download_retry_control()
            visual_points = (
                []
                if retry_control is not None
                else self._find_visual_audio_retry_points_on_screen()
            )
            if retry_control is None and not visual_points:
                if saw_failure:
                    logger.info("Jianying audio resource download recovered")
                    time.sleep(2)
                return saw_failure

            saw_failure = True
            logger.warning(
                "Jianying audio resource download failed; clicking retry (%d/%d)",
                attempt,
                AUDIO_DOWNLOAD_MAX_RETRIES,
            )
            if retry_control is not None:
                self._safe_click(
                    self._require_audio_download_retry_control,
                    f"retry_visible_audio_downloads[{attempt}]",
                )
            else:
                logger.info(
                    "Found %d Jianying audio retry icon(s) visually",
                    len(visual_points),
                )
                for point in visual_points:
                    pyautogui.click(*point)
                    time.sleep(0.5)
            time.sleep(AUDIO_DOWNLOAD_RETRY_INTERVAL)
            self.get_window()
            if self.app_status != "edit":
                raise AutomationError("重试音频下载后剪映离开了编辑页面")

        if (
            self._find_audio_download_retry_control() is not None
            or self._find_visual_audio_retry_points_on_screen()
        ):
            raise AutomationError(
                "剪映内置音频下载持续失败，已停止导出，请检查网络或音频资源 ID"
            )
        return saw_failure

    def retry_failed_audio_downloads(
        self, draft_dir: Optional[str] = None
    ) -> None:
        """剪映 5.9 打开草稿后，自动重试内置 BGM/音效的首次下载。

        音频轨可能隐藏在时间线下方，因此必须逐屏向下滚动：激活当前可见片段、
        重试下载失败资源，再继续下一屏。持续失败则阻止导出，避免静音成片。
        """
        # 保留独立调用的兼容行为；真实导出会传入 draft_dir 并启用全轨扫描。
        if not draft_dir:
            self._retry_visible_audio_downloads()
            return

        native_audio_count = self._count_native_audio_resources(draft_dir)
        native_sticker_count = self._count_native_sticker_resources(draft_dir)
        if native_audio_count + native_sticker_count == 0:
            return

        logger.info(
            "Scanning Jianying timeline for native resources: audio=%d stickers=%d",
            native_audio_count,
            native_sticker_count,
        )

        # 从轨道顶部开始，避免上一次手动滚动位置影响本次扫描。
        self._scroll_timeline(AUDIO_TIMELINE_RESET_CLICKS)
        time.sleep(AUDIO_TIMELINE_SCROLL_INTERVAL)
        try:
            for page in range(AUDIO_TIMELINE_MAX_PAGES):
                clip_points = self._find_visual_timeline_clip_points_on_screen()
                logger.info(
                    "Activating visible Jianying timeline clips: page=%d points=%d",
                    page + 1,
                    len(clip_points),
                )
                for point in clip_points:
                    pyautogui.click(*point)
                    time.sleep(0.15)

                if clip_points:
                    time.sleep(AUDIO_TIMELINE_ACTIVATION_INTERVAL)
                self._retry_visible_audio_downloads()

                before_scroll = self._get_timeline_view_signature()
                self._scroll_timeline(-AUDIO_TIMELINE_SCROLL_CLICKS)
                time.sleep(AUDIO_TIMELINE_SCROLL_INTERVAL)
                after_scroll = self._get_timeline_view_signature()
                if (
                    before_scroll
                    and after_scroll
                    and self._timeline_signatures_similar(
                        before_scroll, after_scroll
                    )
                ):
                    logger.info(
                        "Reached bottom of Jianying timeline after %d page(s)",
                        page + 1,
                    )
                    break
        finally:
            # 导出前恢复轨道顶部，保持后续界面识别的稳定性。
            self._scroll_timeline(AUDIO_TIMELINE_RESET_CLICKS)
            time.sleep(AUDIO_TIMELINE_SCROLL_INTERVAL)

    def _make_export_succeed_close_btn(self, *, from_export_window: bool = False) -> uia.Control:
        root = self.app
        if from_export_window:
            root = self.app.WindowControl(searchDepth=2, Name="导出")
        return root.TextControl(
            searchDepth=2 if from_export_window else 3,
            Compare=ControlFinder.desc_matcher("ExportSucceedCloseBtn"),
        )

    def _find_export_succeed_close_btn(self) -> Optional[uia.Control]:
        """在当前窗口或「导出」子窗口中查找导出成功关闭按钮。"""
        if self._safe_exists(
            lambda: self._make_export_succeed_close_btn(from_export_window=False),
            "find_export_succeed_close_btn.main",
        ):
            return self._make_export_succeed_close_btn(from_export_window=False)

        if self._safe_exists(
            lambda: self.app.WindowControl(searchDepth=2, Name="导出"),
            "find_export_succeed_close_btn.export_window",
        ):
            if self._safe_exists(
                lambda: self._make_export_succeed_close_btn(from_export_window=True),
                "find_export_succeed_close_btn.in_export_window",
            ):
                return self._make_export_succeed_close_btn(from_export_window=True)
        return None

    def _require_export_succeed_close_btn(self) -> uia.Control:
        btn = self._find_export_succeed_close_btn()
        if btn is None:
            raise AutomationError("export succeed close button not found")
        return btn

    def _dismiss_export_success_dialog(self) -> bool:
        """关闭导出成功弹窗；返回是否找到并点击了关闭按钮。"""
        try:
            close_btn = self._find_export_succeed_close_btn()
        except Exception as exc:
            if is_com_uia_error(exc):
                logger.warning(
                    "COM error while locating export success close button: %r",
                    exc,
                )
                self.get_window()
                return False
            raise
        if close_btn is None:
            return False
        logger.info("Dismissing export success dialog")
        self._safe_click(
            self._require_export_succeed_close_btn,
            "dismiss_export_success_dialog",
        )
        time.sleep(2)
        self.get_window()
        return True

    def find_and_click_draft(
        self,
        draft_name: str,
        max_retries: int = 6,
        retry_interval: float = 5.0,
        draft_dir: Optional[str] = None,
    ) -> None:
        """查找并点击指定名称的草稿
        
        Args:
            draft_name (str): 要查找的草稿名称
            max_retries (int): 最大重试次数，默认6次
            retry_interval (float): 重试间隔时间(秒)，默认5秒
            draft_dir (str, optional): 剪映本地草稿目录；未找到时会触发 robocopy 扫描以刷新列表
            
        Raises:
            DraftNotFound: 未找到指定名称的剪映草稿
        """
        last_exception = None
        for attempt in range(max_retries):
            try:
                # 点击对应草稿
                draft_name_text = self.app.TextControl(
                    searchDepth=2,
                    Compare=ControlFinder.desc_matcher(f"HomePageDraftTitle:{draft_name}", exact=True)
                )
                if not draft_name_text.Exists(0):
                    raise exceptions.DraftNotFound(f"未找到名为{draft_name}的剪映草稿")
                draft_btn = draft_name_text.GetParentControl()
                assert draft_btn is not None
                draft_btn.Click(simulateMove=False)
                time.sleep(10)
                self.get_window()
                return  # 成功则返回
            except exceptions.DraftNotFound as e:
                last_exception = e
                if attempt < max_retries - 1:
                    logger.info(
                        "Draft not found (name=%s), retry %d/%d",
                        draft_name,
                        attempt + 1,
                        max_retries,
                    )
                    if draft_dir and os.path.isdir(draft_dir):
                        from src.utils.draft_downloader import trigger_directory_scan_with_robocopy
                        logger.info(
                            "Triggering robocopy directory scan before retry: %s",
                            draft_dir,
                        )
                        trigger_directory_scan_with_robocopy(draft_dir)
                    time.sleep(retry_interval)
        
        # 所有重试都失败，抛出异常
        raise last_exception

    def click_export_button(self) -> None:
        """点击编辑页面的导出按钮
        
        Raises:
            AutomationError: 未找到导出按钮
        """
        export_btn = self.app.TextControl(searchDepth=2, Compare=ControlFinder.desc_matcher("MainWindowTitleBarExportBtn"))
        if not export_btn.Exists(0):
            raise AutomationError("未在编辑窗口中找到导出按钮")
        export_btn.Click(simulateMove=False)
        time.sleep(10)
        self.get_window()

    def get_original_export_path(self) -> str:
        """获取原始导出路径
        
        Returns:
            str: 原始导出路径
            
        Raises:
            AutomationError: 未找到导出路径框
        """
        # 获取原始导出路径（带后缀名）
        export_path_sib = self.app.TextControl(searchDepth=2, Compare=ControlFinder.desc_matcher("ExportPath"))
        if not export_path_sib.Exists(0):
            raise AutomationError("未找到导出路径框")
        export_path_text = export_path_sib.GetSiblingControl(lambda ctrl: True)
        assert export_path_text is not None
        export_path = export_path_text.GetPropertyValue(30159)
        return export_path

    def set_export_resolution(self, resolution: Optional[ExportResolution]) -> None:
        """设置导出分辨率
        
        Args:
            resolution (Optional[ExportResolution]): 导出分辨率，如果为None则不设置
            
        Raises:
            AutomationError: 未找到相关控件
        """
        if resolution is not None:
            setting_group = self.app.GroupControl(searchDepth=1,
                                          Compare=ControlFinder.class_name_matcher("PanelSettingsGroup_QMLTYPE"))
            if not setting_group.Exists(0):
                raise AutomationError("未找到导出设置组")
            resolution_btn = setting_group.TextControl(searchDepth=2, Compare=ControlFinder.desc_matcher("ExportSharpnessInput"))
            if not resolution_btn.Exists(0.5):
                raise AutomationError("未找到导出分辨率下拉框")
            resolution_btn.Click(simulateMove=False)
            time.sleep(0.5)
            resolution_item = self.app.TextControl(
                searchDepth=2, Compare=ControlFinder.desc_matcher(resolution.value)
            )
            if not resolution_item.Exists(0.5):
                raise AutomationError(f"未找到{resolution.value}分辨率选项")
            resolution_item.Click(simulateMove=False)
            time.sleep(0.5)

    def set_export_framerate(self, framerate: Optional[ExportFramerate]) -> None:
        """设置导出帧率
        
        Args:
            framerate (Optional[ExportFramerate]): 导出帧率，如果为None则不设置
            
        Raises:
            AutomationError: 未找到相关控件
        """
        if framerate is not None:
            setting_group = self.app.GroupControl(searchDepth=1,
                                          Compare=ControlFinder.class_name_matcher("PanelSettingsGroup_QMLTYPE"))
            if not setting_group.Exists(0):
                raise AutomationError("未找到导出设置组")
            framerate_btn = setting_group.TextControl(searchDepth=2, Compare=ControlFinder.desc_matcher("FrameRateInput"))
            if not framerate_btn.Exists(0.5):
                raise AutomationError("未找到导出帧率下拉框")
            framerate_btn.Click(simulateMove=False)
            time.sleep(0.5)
            framerate_item = self.app.TextControl(
                searchDepth=2, Compare=ControlFinder.desc_matcher(framerate.value)
            )
            if not framerate_item.Exists(0.5):
                raise AutomationError(f"未找到{framerate.value}帧率选项")
            framerate_item.Click(simulateMove=False)
            time.sleep(0.5)

    def click_final_export_button(self) -> None:
        """点击导出窗口的最终导出按钮
        
        Raises:
            AutomationError: 未找到导出按钮
        """
        export_btn = self.app.TextControl(searchDepth=2, Compare=ControlFinder.desc_matcher("ExportOkBtn", exact=True))
        if not export_btn.Exists(0):
            raise AutomationError("未在导出窗口中找到导出按钮")
        export_btn.Click(simulateMove=False)
        time.sleep(5)

    def __ensure_window_focus(self) -> None:
        """在点击前确保窗口有焦点"""
        # 1. 确保窗口激活
        self.app.SetActive()
        time.sleep(1)
        
        # 2. 确保窗口置顶
        self.app.SetTopmost()
        time.sleep(1)
        
        # 3. 强制获取焦点
        try:
            self.app.SetFocus()
        except:
            pass  # 某些情况下可能失败，但继续执行
        time.sleep(1)

    def wait_for_export_completion(self, timeout: float) -> bool:
        """等待导出完成
        
        Args:
            timeout (float): 超时时间（秒）
            
        Returns:
            bool: 是否已关闭导出成功弹窗（表示导出已完成）
            
        Raises:
            AutomationError: 导出超时
        """
        # 点击继续导出按钮次数
        continue_export_click_count = 0
        export_succeeded = False

        # 等待导出完成
        st = time.time()
        while True:
            self.get_window()
            if self.app_status != "pre_export":
                break

            if self._find_export_succeed_close_btn() is not None:
                logger.info("Export finished, closing success dialog")
                self._safe_click(
                    self._require_export_succeed_close_btn,
                    "wait_for_export_completion.close_success",
                )
                time.sleep(2)
                export_succeeded = True
                break

            if time.time() - st > timeout:
                raise AutomationError("导出超时, 时限为%d秒" % timeout)

            # 导出过程中，如果出现异常弹窗，则点击继续导出按钮
            if continue_export_click_count < 20:
                print("pyautogui.size(): ", pyautogui.size(), ", click index: ", continue_export_click_count)
                pyautogui.click(x=996, y=597, button="left")
                continue_export_click_count += 1

            time.sleep(1)
        time.sleep(2)
        return export_succeeded

    def return_to_home(self) -> None:
        """回到目录页并稍作延迟"""
        self.get_window()
        self._dismiss_export_success_dialog()
        self.switch_to_home()
        time.sleep(2)

    def move_exported_file(self, original_path: str, output_path: Optional[str]) -> None:
        """移动导出的文件到指定位置
        
        Args:
            original_path (str): 原始导出路径
            output_path (Optional[str]): 目标输出路径，如果为None则不移动
        """
        logger.info(f"move {original_path} to {output_path}")
        if output_path is not None:
            shutil.move(original_path, output_path)

    def export_draft(self, draft_name: str, output_path: Optional[str] = None, *,
                     resolution: Optional[ExportResolution] = None,
                     framerate: Optional[ExportFramerate] = None,
                     timeout: float = 300,
                     draft_dir: Optional[str] = None) -> None:
        """导出指定的剪映草稿, **目前仅支持剪映6及以下版本**

        **注意: 需要确认有导出草稿的权限(不使用VIP功能或已开通VIP), 否则可能陷入死循环**

        Args:
            draft_name (`str`): 要导出的剪映草稿名称
            output_path (`str`, optional): 导出路径, 支持指向文件夹或直接指向文件, 不指定则使用剪映默认路径.
            resolution (`Export_resolution`, optional): 导出分辨率, 默认不改变剪映导出窗口中的设置.
            framerate (`Export_framerate`, optional): 导出帧率, 默认不改变剪映导出窗口中的设置.
            timeout (`float`, optional): 导出超时时间(秒), 默认为5分钟.
            draft_dir (`str`, optional): 剪映本地草稿目录；未在首页找到草稿时会 robocopy 触发扫描后重试.

        Raises:
            `DraftNotFound`: 未找到指定名称的剪映草稿
            `AutomationError`: 剪映操作失败
        """
        logger.info(f"start export {draft_name} to {output_path}")

        # 初始化准备
        self.get_window()
        self.switch_to_home()

        original_path = None
        export_completed = False

        for i in range(16):
            # 确保窗口有焦点
            self.__ensure_window_focus()
            if self.app_status == "home":
                logger.info("[%d]app is already in home page", i)
                self.find_and_click_draft(draft_name, draft_dir=draft_dir)
                self.retry_failed_audio_downloads(draft_dir=draft_dir)
            elif self.app_status == "edit":
                if export_completed or (
                    original_path and os.path.isfile(original_path)
                ):
                    logger.info(
                        "[%d]export already finished, skip re-export and return home",
                        i,
                    )
                    self.return_to_home()
                    break
                logger.info("[%d]app is already in edit page", i)
                # 点击导出按钮进入导出界面
                self.click_export_button()
            elif self.app_status == "pre_export":                
                if self.app_sub_status == "export_start":
                    logger.info("[%d]app is already in pre_export[export_start] page", i)
                    # 获取原始导出路径
                    original_path = self.get_original_export_path()
                    # 设置分辨率（如果指定）
                    self.set_export_resolution(resolution)                    
                    # 设置帧率（如果指定）
                    self.set_export_framerate(framerate)                    
                    # 点击最终导出按钮
                    self.click_final_export_button()
                    # 获取窗口状态
                    self.get_window()
                elif self.app_sub_status == "exporting":
                    logger.info("[%d]app is already in pre_export[exporting] page", i)
                    if self.wait_for_export_completion(timeout):
                        export_completed = True
                        self.return_to_home()
                        break
                    self.get_window()
                    if original_path and os.path.isfile(original_path):
                        logger.info(
                            "[%d]export output file exists after wait, treating as success",
                            i,
                        )
                        export_completed = True
                        self.return_to_home()
                        break
                elif self.app_sub_status == "export_succeed":
                    logger.info("[%d]app is already in pre_export[export_succeed] page", i)
                    export_completed = True
                    self.return_to_home()
                    break
                else:
                    raise AutomationError("[%d]app is in unknown sub-status: %s" % (i, self.app_sub_status))
            else:
                raise AutomationError("[%d]app is in unknown status: %s" % (i, self.app_status))
        
        # 移动导出文件到指定路径（如果指定）
        self.move_exported_file(original_path, output_path)
        
        logger.info(f"export {draft_name} to {output_path} completed")

    def switch_to_home(self) -> None:
        """切换到剪映主页"""
        for i in range(8):
            self.get_window()
            if self.app_status == "home":
                return

            if self._dismiss_export_success_dialog():
                continue

            if self.app_status == "pre_export":
                # 导出弹窗未识别为 export_succeed 时，仍尝试关闭成功页或按 ESC 退出
                if self.app_sub_status in ("export_succeed", "exporting", "export_start"):
                    if self._find_export_succeed_close_btn() is not None:
                        self._safe_click(
                            self._require_export_succeed_close_btn,
                            f"switch_to_home.pre_export_close[{i}]",
                        )
                        time.sleep(2)
                        continue
                logger.warning(
                    "switch_to_home: stuck in pre_export sub_status=%s, attempt=%d",
                    self.app_sub_status,
                    i,
                )
                time.sleep(1)
                continue

            if self.app_status == "edit":
                close_btn = self.app.GroupControl(
                    searchDepth=1,
                    ClassName="TitleBarButton",
                    foundIndex=3,
                )
                if not close_btn.Exists(1, 0.5):
                    logger.warning(
                        "switch_to_home: edit close button missing, attempt=%d",
                        i,
                    )
                    time.sleep(1)
                    continue
                self._safe_click(
                    lambda: self.app.GroupControl(
                        searchDepth=1,
                        ClassName="TitleBarButton",
                        foundIndex=3,
                    ),
                    f"switch_to_home.edit_close[{i}]",
                )
                time.sleep(2)
                continue

            raise AutomationError("invalid app status: %s" % self.app_status)

        logger.warning("Cannot switch to home page after %d attempts", 8)

    def get_window(
        self,
        max_retries: Optional[int] = None,
        retry_interval: Optional[float] = None,
    ) -> None:
        """寻找剪映窗口并置顶；未找到时按间隔重试以提高容错。"""
        if max_retries is None:
            max_retries = self.WINDOW_FIND_MAX_RETRIES
        if retry_interval is None:
            retry_interval = self.WINDOW_FIND_RETRY_INTERVAL

        if hasattr(self, "app"):
            try:
                if self._exists_with_com_retry(
                    self.app,
                    "get_window.clear_topmost",
                    timeout=0,
                    raise_on_exhausted=False,
                ):
                    self.app.SetTopmost(False)
            except Exception as exc:
                if not is_com_uia_error(exc):
                    raise
                logger.warning(
                    "Stale Jianying window handle when clearing topmost: %r",
                    exc,
                )

        for attempt in range(max_retries):
            self.app = uia.WindowControl(searchDepth=1, Compare=self.__jianying_window_cmp)
            if self._exists_with_com_retry(
                self.app,
                "get_window.find_main",
                timeout=0,
                raise_on_exhausted=False,
            ):
                if attempt > 0:
                    logger.info(
                        "Jianying main window matched on attempt %d/%d",
                        attempt + 1,
                        max_retries,
                    )
                break
            if attempt < max_retries - 1:
                logger.warning(
                    "Jianying main window not found, retrying in %.1fs (%d/%d)",
                    retry_interval,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(retry_interval)
        else:
            raise AutomationError(
                "Jianying window not found after %d attempts (%.1fs interval); "
                "ensure Jianying Pro is open on the home or edit screen."
                % (max_retries, retry_interval)
            )

        # 寻找可能存在的导出窗口
        export_window = self.app.WindowControl(searchDepth=1, Name="导出")
        if self._exists_with_com_retry(
            export_window,
            "get_window.find_export",
            timeout=0,
            raise_on_exhausted=False,
        ):
            self.app = export_window
            self.app_status = "pre_export"

        # 初始化导出子状态
        self.init_export_sub_status()

        logger.info("app_status: %s, app_sub_status: %s", self.app_status, self.app_sub_status)

        self.app.SetActive()
        self.app.SetTopmost()

    # 初始化导出子状态
    def init_export_sub_status(self) -> None:
        if self.app_status == "pre_export":
            # 0. 初始化默认值为导出中
            self.app_sub_status = "exporting"
            
            # 1. 检查窗口是否停留在导出开始页面
            export_ok_btn = self.app.TextControl(searchDepth=2, Compare=ControlFinder.desc_matcher("ExportOkBtn", exact=True))
            if export_ok_btn.Exists(0):
                self.app_sub_status = "export_start"
                return

            # 2. 检查窗口是否停留在导出完成页面
            if self._safe_exists(
                lambda: self._make_export_succeed_close_btn(from_export_window=False),
                "init_export_sub_status.export_succeed",
                timeout=0,
            ):
                self.app_sub_status = "export_succeed"
                return
        else:
            self.app_sub_status = "none"

    def __jianying_window_cmp(self, control: uia.WindowControl, depth: int) -> bool:
        try:
            name = control.Name
        except Exception as exc:
            if is_com_uia_error(exc):
                return False
            raise
        if name != "剪映专业版":
            return False
        try:
            class_name = control.ClassName
        except Exception as exc:
            if is_com_uia_error(exc):
                return False
            raise
        class_name_lower = class_name.lower()
        if "homepage" in class_name_lower:
            self.app_status = "home"
            return True
        if "mainwindow" in class_name_lower:
            self.app_status = "edit"
            return True

        logger.info("ClassName: %s, Name: %s", class_name_lower, name.lower())
        return False
