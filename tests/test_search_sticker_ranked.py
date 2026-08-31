from __future__ import annotations

import json

import config
from src.schemas.search_sticker import SearchStickerResponse
from src.service.search_sticker import search_sticker


def test_combined_terms_search_entire_catalog_without_random_fallback(tmp_path, monkeypatch):
    catalog = [
        {"sticker_id": "1", "title": "猫咪休息"},
        {"sticker_id": "2", "title": "白猫开心跳舞"},
        {"sticker_id": "3", "title": "小狗跳舞"},
    ]
    path = tmp_path / "sticker.json"
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "STICKER_CONFIG_PATH", str(path))
    monkeypatch.setattr(
        config,
        "STICKER_OVERRIDE_CONFIG_PATH",
        str(tmp_path / "missing-overrides.json"),
    )

    combined = search_sticker(keywords=["猫", "跳舞"], match_mode="all", limit=20)
    missing = search_sticker("根本不存在")

    assert [item["sticker_id"] for item in combined] == ["2"]
    assert missing == []


def test_verified_draft_override_is_searchable_by_alias(tmp_path, monkeypatch):
    base = tmp_path / "sticker.json"
    base.write_text("[]", encoding="utf-8")
    overrides = tmp_path / "sticker_overrides.json"
    overrides.write_text(
        json.dumps(
            [
                {
                    "sticker": {
                        "large_image": {"image_url": "https://example.com/cat.png"},
                        "preview_cover": "https://example.com/cat.png",
                        "sticker_package": {
                            "height_per_frame": 0,
                            "size": 0,
                            "width_per_frame": 0,
                        },
                        "sticker_type": 1,
                        "track_thumbnail": "https://example.com/cat.png",
                    },
                    "sticker_id": "7616307736162143550",
                    "title": "跳舞奶牛猫",
                    "search_aliases": ["站立摆臂猫", "动态猫咪"],
                    "catalog": {
                        "source_provider": "capcut_draft_verified",
                        "cycle_setting": True,
                        "verified_in_capcut": True,
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "STICKER_CONFIG_PATH", str(base))
    monkeypatch.setattr(config, "STICKER_OVERRIDE_CONFIG_PATH", str(overrides))

    result = search_sticker(keywords=["站立", "摆臂", "猫"], match_mode="all")

    assert [item["sticker_id"] for item in result] == ["7616307736162143550"]
    response = SearchStickerResponse(data=result)
    assert response.data[0].sticker.sticker_package.size == 0
    assert response.data[0].catalog["verified_in_capcut"] is True
