import json
import os
import tempfile
from unittest.mock import patch

import src.utils.draft_downloader as dd


def test_local_generated_draft_uses_original_http_download_pipeline():
    draft_id = "20260828150000abcdef12"
    draft_url = f"http://127.0.0.1:30000/get_draft?draft_id={draft_id}"
    files = [f"http://127.0.0.1:30000/output/draft/{draft_id}/draft_content.json"]

    with tempfile.TemporaryDirectory() as project_output, tempfile.TemporaryDirectory() as jianying_root:
        # 即使本机 output 中已有同 ID 草稿，也必须沿用作者原版下载链路，
        # 由逐文件落盘和 robocopy 负责触发剪映扫描。
        os.makedirs(os.path.join(project_output, draft_id))
        target_dir = os.path.join(jianying_root, draft_id)

        with (
            patch.object(dd.config, "DRAFT_DIR", project_output),
            patch.object(dd.config, "DRAFT_SAVE_PATH", jianying_root),
            patch.object(dd, "_get_draft_files_list", return_value=files) as get_files,
            patch.object(dd, "_download_all_files") as download_files,
        ):
            result = dd.download_draft_with_result(draft_url)

        assert result.ok is True
        get_files.assert_called_once_with(draft_url)
        download_files.assert_called_once_with(files, target_dir, draft_id)


def test_original_download_pipeline_rewrites_local_source_material_paths():
    draft_id = "20260828153000abcdef12"
    with tempfile.TemporaryDirectory() as project_output, tempfile.TemporaryDirectory() as jianying_root:
        source_dir = os.path.join(project_output, draft_id)
        source_asset_dir = os.path.join(source_dir, "assets", "videos")
        os.makedirs(source_asset_dir)
        source_asset = os.path.join(source_asset_dir, "clip.mp4")
        with open(source_asset, "wb") as f:
            f.write(b"source")

        target_dir = os.path.join(jianying_root, draft_id)
        target_asset_dir = os.path.join(target_dir, "assets", "videos")
        os.makedirs(target_asset_dir)
        target_asset = os.path.join(target_asset_dir, "clip.mp4")
        with open(target_asset, "wb") as f:
            f.write(b"target")

        content_path = os.path.join(target_dir, "draft_content.json")
        with open(content_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "materials": {
                        "audios": [],
                        "videos": [{"id": "v1", "path": source_asset}],
                    }
                },
                f,
            )

        with (
            patch.object(dd.config, "DRAFT_DIR", project_output),
            patch.object(dd.config, "DRAFT_SAVE_PATH", jianying_root),
        ):
            dd._update_json_file_paths(content_path, target_dir, draft_id)

        with open(content_path, encoding="utf-8") as f:
            content = json.load(f)
        assert content["materials"]["videos"][0]["path"] == target_asset
