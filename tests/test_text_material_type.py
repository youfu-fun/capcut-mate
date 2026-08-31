from src.pyJianYingDraft import ScriptFile, TrackType
from src.service.add_captions import add_caption_to_draft


def _material(text_type: str, background_color: str | None = None):
    script = ScriptFile(1080, 1920, 30, maintrack_adsorb=False)
    script.add_track(TrackType.text, "text")
    _, material_id, _ = add_caption_to_draft(
        script,
        "text",
        caption={"start": 0, "end": 1_000_000, "text": "测试花字"},
        text_type=text_type,
        background_color=background_color,
        background_round_radius=0.2,
    )
    return next(item for item in script.materials.texts if item["id"] == material_id)


def test_decoration_text_is_not_exported_as_subtitle():
    material = _material("text", "#FFA500")
    assert material["type"] == "text"
    assert material["background_color"] == "#FFA500"
    assert material["background_round_radius"] == 0.2


def test_dialogue_text_remains_subtitle():
    assert _material("subtitle")["type"] == "subtitle"
