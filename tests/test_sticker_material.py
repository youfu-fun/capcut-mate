import json
from pathlib import Path

import config
from src.pyJianYingDraft import StickerSegment, trange
from src.utils.sticker_catalog import build_sticker_material, resolve_sticker_cache_path


def test_build_sticker_material_uses_catalog_metadata(monkeypatch, tmp_path):
    base = tmp_path / "stickers.json"
    overrides = tmp_path / "overrides.json"
    base.write_text("[]", encoding="utf-8")
    overrides.write_text(
        json.dumps(
            [
                {
                    "sticker_id": "123",
                    "title": "跳舞猫",
                    "sticker": {
                        "track_thumbnail": "https://example.test/icon.png",
                        "preview_cover": "https://example.test/preview.png",
                    },
                    "catalog": {"cycle_setting": True},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "STICKER_CONFIG_PATH", str(base))
    monkeypatch.setattr(config, "STICKER_OVERRIDE_CONFIG_PATH", str(overrides))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    material = build_sticker_material("123")

    assert material["name"] == "跳舞猫"
    assert material["icon_url"] == "https://example.test/icon.png"
    assert material["preview_cover_url"] == "https://example.test/preview.png"
    assert material["category_id"] == "heycan_search_sticker"
    assert material["cycle_setting"] is True
    assert material["resource_id"] == "123"

    exported = StickerSegment(
        "123", trange(0, 1_000_000), material_payload=material
    ).export_material()
    assert exported["name"] == "跳舞猫"
    assert exported["resource_id"] == "123"
    assert exported["id"]


def test_resolve_sticker_cache_path_selects_existing_entry(tmp_path):
    cache = (
        tmp_path
        / "JianyingPro"
        / "User Data"
        / "Cache"
        / "artistEffect"
        / "123"
        / "resource-hash"
    )
    cache.mkdir(parents=True)

    assert resolve_sticker_cache_path("123", local_app_data=str(tmp_path)) == cache.as_posix()
