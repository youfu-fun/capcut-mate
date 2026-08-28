import json
import os
import tempfile
from unittest.mock import patch

import src.utils.draft_downloader as dd


def test_local_generated_draft_is_copied_directly_into_jianying_root():
    draft_id = "20260828150000abcdef12"
    with tempfile.TemporaryDirectory() as project_output, tempfile.TemporaryDirectory() as jianying_root:
        source_dir = os.path.join(project_output, draft_id)
        asset_dir = os.path.join(source_dir, "assets", "videos")
        os.makedirs(asset_dir)
        source_asset = os.path.join(asset_dir, "clip.mp4")
        with open(source_asset, "wb") as f:
            f.write(b"video")

        with open(os.path.join(source_dir, "draft_content.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "duration": 5_000_000,
                    "materials": {
                        "audios": [],
                        "videos": [{"id": "v1", "path": source_asset}],
                    },
                },
                f,
            )
        with open(os.path.join(source_dir, "draft_info.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        with open(os.path.join(source_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "draft_name": "old-name",
                    "draft_fold_path": "C:/old",
                    "draft_root_path": "C:/old",
                },
                f,
            )

        with (
            patch.object(dd.config, "DRAFT_DIR", project_output),
            patch.object(dd.config, "DRAFT_SAVE_PATH", jianying_root),
            patch.object(dd, "trigger_directory_scan_with_robocopy"),
            patch.object(dd, "_get_draft_files_list") as get_files,
        ):
            result = dd.download_draft_with_result(
                f"http://127.0.0.1:30000/get_draft?draft_id={draft_id}"
            )

        assert result.ok is True
        get_files.assert_not_called()

        target_dir = os.path.join(jianying_root, draft_id)
        with open(os.path.join(target_dir, "draft_content.json"), encoding="utf-8") as f:
            content = json.load(f)
        with open(os.path.join(target_dir, "draft_meta_info.json"), encoding="utf-8") as f:
            meta = json.load(f)

        expected_asset = os.path.join(target_dir, "assets", "videos", "clip.mp4")
        assert content["materials"]["videos"][0]["path"] == expected_asset
        assert os.path.isfile(expected_asset)
        assert meta["draft_name"] == draft_id
        assert os.path.normpath(meta["draft_fold_path"]) == os.path.normpath(target_dir)
        assert os.path.normpath(meta["draft_root_path"]) == os.path.normpath(jianying_root)
        assert os.path.isfile(os.path.join(target_dir, "draft_info.json"))


def test_existing_complete_jianying_draft_is_reused_without_recopy():
    draft_id = "20260828153000abcdef12"
    with tempfile.TemporaryDirectory() as project_output, tempfile.TemporaryDirectory() as jianying_root:
        source_dir = os.path.join(project_output, draft_id)
        target_dir = os.path.join(jianying_root, draft_id)
        os.makedirs(source_dir)
        os.makedirs(target_dir)

        for directory, marker in ((source_dir, "source"), (target_dir, "target")):
            with open(os.path.join(directory, "draft_content.json"), "w", encoding="utf-8") as f:
                json.dump({"duration": 5_000_000, "marker": marker}, f)
            with open(os.path.join(directory, "draft_meta_info.json"), "w", encoding="utf-8") as f:
                json.dump({"draft_name": draft_id}, f)

        with (
            patch.object(dd.config, "DRAFT_DIR", project_output),
            patch.object(dd.config, "DRAFT_SAVE_PATH", jianying_root),
            patch.object(dd, "trigger_directory_scan_with_robocopy") as scan,
            patch.object(dd.shutil, "rmtree") as rmtree,
            patch.object(dd.shutil, "copytree") as copytree,
        ):
            result = dd.download_draft_with_result(
                f"http://127.0.0.1:30000/get_draft?draft_id={draft_id}"
            )

        assert result.ok is True
        scan.assert_called_once_with(target_dir)
        rmtree.assert_not_called()
        copytree.assert_not_called()
        with open(os.path.join(target_dir, "draft_content.json"), encoding="utf-8") as f:
            assert json.load(f)["marker"] == "target"
