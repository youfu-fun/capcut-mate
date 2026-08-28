import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from exceptions import CustomException
from src.pyJianYingDraft.local_materials import CapCutAudioResourceMaterial
from src.schemas.add_audios import AddAudiosRequest
from src.service.add_audios import (
    _prepare_audios_local_files,
    add_audio_to_draft,
    parse_audio_data,
)


def _request(item):
    return AddAudiosRequest(draft_url="http://localhost/get_draft?draft_id=test", audio_infos=json.dumps([item]))


def test_schema_keeps_http_audio_compatible():
    request = _request({"audio_url": "https://example.com/bgm.mp3", "start": 0, "end": 1_000_000})
    assert "audio_url" in request.audio_infos


def test_schema_accepts_music_id_without_audio_url():
    request = _request({"music_id": "music-123", "duration": 5_000_000, "start": 0, "end": 2_000_000})
    assert "music-123" in request.audio_infos


def test_schema_rejects_audio_without_url_or_resource_id():
    with pytest.raises(ValidationError):
        _request({"duration": 5_000_000, "start": 0, "end": 2_000_000})


def test_parser_normalizes_music_and_sound_effect_aliases():
    parsed = parse_audio_data(json.dumps([
        {"music_id": "music-123", "duration": 5_000_000, "start": 0, "end": 2_000_000},
        {"effect_id": "sound-456", "duration": 1_000_000, "start": 2_000_000, "end": 3_000_000},
    ]))

    assert parsed[0]["source_type"] == "capcut_resource"
    assert parsed[0]["resource_id"] == "music-123"
    assert parsed[0]["resource_kind"] == "music"
    assert parsed[1]["resource_id"] == "sound-456"
    assert parsed[1]["resource_kind"] == "sound_effect"


def test_parser_requires_resource_duration():
    with pytest.raises(CustomException):
        parse_audio_data(json.dumps([
            {"resource_id": "music-123", "resource_kind": "music", "start": 0, "end": 2_000_000},
        ]))


def test_prepare_skips_download_for_capcut_resource():
    info = json.dumps([
        {"resource_id": "music-123", "resource_kind": "music", "duration": 5_000_000, "start": 0, "end": 2_000_000},
    ])
    with (
        patch("src.service.add_audios.validate_and_get_draft_id", return_value="draft-1"),
        patch("src.service.add_audios.create_audio_directory", return_value="/tmp/audios"),
        patch("src.service.add_audios.download_audio_file") as download_audio,
    ):
        result = _prepare_audios_local_files("http://localhost/get_draft?draft_id=draft-1", info)

    download_audio.assert_not_called()
    assert "local_audio_path" not in result[0]


def test_music_material_exports_native_id_and_preserves_metadata():
    material = CapCutAudioResourceMaterial(
        "music-123",
        5_000_000,
        resource_kind="music",
        material_name="测试 BGM",
        resource_metadata={"category_id": "favorites", "custom_version_field": "kept"},
    )

    payload = material.export_json()
    assert payload["type"] == "music"
    assert payload["music_id"] == "music-123"
    assert payload["effect_id"] == ""
    assert payload["id"] == material.material_id
    assert payload["id"] != payload["music_id"]
    assert payload["duration"] == 5_000_000
    assert payload["custom_version_field"] == "kept"


def test_sound_effect_material_uses_effect_id():
    material = CapCutAudioResourceMaterial("sound-456", 1_000_000, resource_kind="sound_effect")
    payload = material.export_json()

    assert payload["type"] == "sound"
    assert payload["effect_id"] == "sound-456"
    assert payload["music_id"] == ""


def test_add_audio_to_draft_uses_resource_material_without_local_file():
    script = MagicMock()
    audio = parse_audio_data(json.dumps([
        {
            "resource_id": "music-123",
            "resource_kind": "music",
            "resource_name": "测试 BGM",
            "duration": 5_000_000,
            "start": 0,
            "end": 2_000_000,
            "volume": 0.5,
        },
    ]))[0]

    with (
        patch("src.service.add_audios.download_audio_file") as download_audio,
        patch("src.service.add_audios.get_audio_actual_duration") as get_duration,
    ):
        material_id = add_audio_to_draft(script, "audio-track", "/tmp/audios", audio)

    download_audio.assert_not_called()
    get_duration.assert_not_called()
    segment = script.add_segment.call_args.args[0]
    assert isinstance(segment.material_instance, CapCutAudioResourceMaterial)
    assert segment.material_instance.resource_id == "music-123"
    assert segment.target_timerange.duration == 2_000_000
    assert material_id == segment.material_instance.material_id
