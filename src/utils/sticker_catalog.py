"""CapCut sticker catalog loading and draft material generation.

Sticker search results are not enough by themselves to create a usable draft
material.  Jianying expects the material entry to contain its display metadata
and, when the resource has already been downloaded, its local cache path.
This module is the single source of truth for both searching and draft export.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import config
from src.utils.logger import logger


def load_sticker_catalog() -> List[Dict[str, Any]]:
    """Load the public catalog and merge verified local overrides by ID."""
    with open(config.STICKER_CONFIG_PATH, "r", encoding="utf-8") as file:
        public_items = json.load(file)

    items_by_id = {
        str(item.get("sticker_id", "")): item
        for item in public_items
        if item.get("sticker_id")
    }
    override_path = getattr(config, "STICKER_OVERRIDE_CONFIG_PATH", "")
    if override_path:
        try:
            with open(override_path, "r", encoding="utf-8") as file:
                override_items = json.load(file)
        except FileNotFoundError:
            override_items = []
        for item in override_items:
            sticker_id = str(item.get("sticker_id", ""))
            if sticker_id:
                items_by_id[sticker_id] = item
    return list(items_by_id.values())


def find_sticker(sticker_id: str) -> Optional[Dict[str, Any]]:
    wanted = str(sticker_id)
    try:
        return next(
            (item for item in load_sticker_catalog() if str(item.get("sticker_id")) == wanted),
            None,
        )
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.error(f"Failed to load sticker catalog: {exc}")
        return None


def _first_nonempty(values: Iterable[Any]) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def resolve_sticker_cache_path(
    sticker_id: str,
    *,
    local_app_data: Optional[str] = None,
) -> str:
    """Return Jianying's existing cache entry for a sticker, if available."""
    app_data = local_app_data if local_app_data is not None else os.getenv("LOCALAPPDATA", "")
    if not app_data:
        return ""
    resource_dir = (
        Path(app_data)
        / "JianyingPro"
        / "User Data"
        / "Cache"
        / "artistEffect"
        / str(sticker_id)
    )
    if not resource_dir.exists():
        return ""
    children = sorted(resource_dir.iterdir(), key=lambda path: path.name)
    selected = children[0] if children else resource_dir
    return selected.as_posix()


def build_sticker_material(sticker_id: str) -> Dict[str, Any]:
    """Build a Jianying-compatible material entry from a catalog asset."""
    resource_id = str(sticker_id)
    item = find_sticker(resource_id) or {}
    sticker = item.get("sticker") if isinstance(item.get("sticker"), dict) else {}
    large_image = sticker.get("large_image") if isinstance(sticker.get("large_image"), dict) else {}
    catalog = item.get("catalog") if isinstance(item.get("catalog"), dict) else {}
    icon_url = _first_nonempty(
        (
            sticker.get("track_thumbnail"),
            sticker.get("preview_cover"),
            large_image.get("image_url"),
        )
    )
    preview_url = _first_nonempty(
        (
            sticker.get("preview_cover"),
            large_image.get("image_url"),
            sticker.get("track_thumbnail"),
        )
    )

    return {
        "aigc_type": "none",
        "background_alpha": 1.0,
        "background_color": "",
        "border_color": "",
        "border_line_style": 0,
        "border_width": 0.0,
        "category_id": str(item.get("category_id") or "heycan_search_sticker"),
        "category_name": str(item.get("category_name") or "heycan_search_sticker"),
        "check_flag": 1,
        "combo_info": {"text_templates": []},
        "cycle_setting": bool(catalog.get("cycle_setting", True)),
        "formula_id": "",
        "global_alpha": 1.0,
        "has_shadow": False,
        "icon_url": icon_url,
        "multi_language_current": "none",
        "name": str(item.get("title") or resource_id),
        "original_size": [],
        "path": resolve_sticker_cache_path(resource_id),
        "platform": "all",
        "preview_cover_url": preview_url,
        "radius": {
            "bottom_left": 0.0,
            "bottom_right": 0.0,
            "top_left": 0.0,
            "top_right": 0.0,
        },
        "request_id": "",
        "resource_id": resource_id,
        "sequence_type": False,
        "shadow_alpha": 0.8,
        "shadow_angle": 0.0,
        "shadow_color": "",
        "shadow_distance": 0.0,
        "shadow_point": {"x": 0.0, "y": 0.0},
        "shadow_smoothing": 0.0,
        "shape_param": {
            "custom_points": [],
            "roundness": [],
            "shape_size": [],
            "shape_type": 0,
        },
        "source_platform": 1,
        "sticker_id": resource_id,
        "sub_type": 0,
        "team_id": "",
        "type": "sticker",
        "unicode": "",
    }
