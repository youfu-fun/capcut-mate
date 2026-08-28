import json
from unittest.mock import MagicMock, patch

from src.service.add_audios import create_audio_segment, parse_audio_data
from src.service.add_videos import add_video_to_draft, parse_video_data


def test_video_parser_preserves_source_trim_speed_and_per_clip_transform():
    result = parse_video_data(
        json.dumps(
            [
                {
                    "video_url": "https://example.com/video.mp4",
                    "start": 2_000_000,
                    "end": 4_000_000,
                    "source_start": 5_000_000,
                    "source_end": 8_000_000,
                    "playback_rate": 1.5,
                    "alpha": 0.8,
                    "scale_x": 1.2,
                    "scale_y": 1.1,
                    "transform_x": 120,
                    "transform_y": -80,
                    "rotation_degrees": 12,
                }
            ]
        )
    )[0]

    assert result["source_start"] == 5_000_000
    assert result["source_end"] == 8_000_000
    assert result["playback_rate"] == 1.5
    assert result["transform_x"] == 120
    assert result["rotation_degrees"] == 12


def test_video_writer_uses_physical_source_range_and_exact_timeline_duration():
    script = MagicMock(width=1080, height=1920)
    material = MagicMock(duration=10_000_000)
    segment = MagicMock(segment_id="segment-1")
    video = {
        "video_url": "https://example.com/video.mp4",
        "local_video_path": "/tmp/video.mp4",
        "start": 2_000_000,
        "end": 4_000_000,
        "duration": 2_000_000,
        "source_start": 5_000_000,
        "source_end": 8_000_000,
        "playback_rate": 1.5,
        "volume": 0,
        "alpha": 0.8,
        "scale_x": 1.2,
        "scale_y": 1.1,
        "transform_x": 108,
        "transform_y": -192,
        "rotation_degrees": 12,
        "transition": None,
    }

    with (
        patch("src.service.add_videos.os.path.isfile", return_value=True),
        patch("src.service.add_videos.draft.VideoMaterial", return_value=material),
        patch("src.service.add_videos.draft.VideoSegment", return_value=segment) as factory,
    ):
        _, info, duration = add_video_to_draft(
            script,
            "main",
            "/tmp",
            video,
        )

    kwargs = factory.call_args.kwargs
    assert kwargs["target_timerange"].start == 2_000_000
    assert kwargs["target_timerange"].duration == 2_000_000
    assert kwargs["source_timerange"].start == 5_000_000
    assert kwargs["source_timerange"].duration == 3_000_000
    assert kwargs["speed"] == 1.5
    assert kwargs["clip_settings"].rotation == 12
    assert kwargs["clip_settings"].transform_x == 0.1
    assert duration == 2_000_000
    assert info.end == 4_000_000


def test_audio_parser_and_writer_preserve_trim_and_fades():
    audio = parse_audio_data(
        json.dumps(
            [
                {
                    "audio_url": "https://example.com/audio.wav",
                    "start": 1_000_000,
                    "end": 3_000_000,
                    "source_start": 4_000_000,
                    "source_end": 6_000_000,
                    "fade_in_duration": 100_000,
                    "fade_out_duration": 200_000,
                    "volume": 0.75,
                }
            ]
        )
    )[0]
    segment = MagicMock()

    with patch("src.service.add_audios.draft.AudioSegment", return_value=segment) as factory:
        result = create_audio_segment(
            "/tmp/audio.wav",
            audio["start"],
            audio["end"] - audio["start"],
            audio,
            source_start=audio["source_start"],
            source_end=audio["source_end"],
        )

    kwargs = factory.call_args.kwargs
    assert kwargs["target_timerange"].start == 1_000_000
    assert kwargs["target_timerange"].duration == 2_000_000
    assert kwargs["source_timerange"].start == 4_000_000
    assert kwargs["source_timerange"].duration == 2_000_000
    assert kwargs["volume"] == 0.75
    segment.add_fade.assert_called_once_with(100_000, 200_000)
    assert result is segment
