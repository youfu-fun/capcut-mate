from unittest.mock import MagicMock, patch

import src.pyJianYingDraft as draft
from src.service.add_videos import _add_videos_internal


def test_add_videos_reuses_existing_track_for_followup_batch() -> None:
    script = MagicMock()
    track = MagicMock()
    track.track_id = "video-track-1"
    track.track_type = draft.TrackType.video
    track.name = "existing-video-track"
    script.tracks = {track.name: track}
    script.materials.videos = []
    prepared = [
        {
            "video_url": "https://example.com/clip.mp4",
            "local_video_path": "C:/tmp/clip.mp4",
            "start": 1_000_000,
            "end": 2_000_000,
            "original_start": 1_000_000,
            "original_end": 2_000_000,
        }
    ]

    with (
        patch("src.service.add_videos.helper.get_url_param", return_value="draft-1"),
        patch("src.service.add_videos.DRAFT_CACHE", {"draft-1": script}),
        patch("src.service.add_videos.os.makedirs"),
        patch(
            "src.service.add_videos.add_video_to_draft",
            return_value=("segment-1", MagicMock(), 1_000_000),
        ) as add_video,
    ):
        result = _add_videos_internal(
            draft_url="http://localhost/get_draft?draft_id=draft-1",
            video_infos="[]",
            track_id="video-track-1",
            prepared_videos=prepared,
        )

    script.add_track_ordered.assert_not_called()
    assert add_video.call_args.args[1] == "existing-video-track"
    assert result[1] == "video-track-1"
