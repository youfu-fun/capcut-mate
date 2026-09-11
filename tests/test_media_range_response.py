from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles
import pytest

from src.middlewares.response import ResponseMiddleware
from src.utils.draft_downloader import (
    DraftDownloadAbort, _resume_request_headers, _write_http_body_to_file,
)


def test_static_binary_range_and_errors_are_preserved(tmp_path):
    payload = b"\x00\xff\xe1\x80" * 100
    (tmp_path / "test.mp4").write_bytes(payload)
    app = FastAPI()
    app.mount("/output", StaticFiles(directory=tmp_path))
    app.add_middleware(ResponseMiddleware)
    with TestClient(app) as client:
        full = client.get("/output/test.mp4")
        assert full.status_code == 200
        assert full.content == payload
        partial = client.get("/output/test.mp4", headers={"Range": "bytes=4-19"})
        assert partial.status_code == 206
        assert partial.content == payload[4:20]
        assert partial.headers["content-range"] == "bytes 4-19/400"
        assert client.get("/output/test.mp4", headers={"Range": "bytes=999-"}).status_code == 416
        assert client.get("/output/missing.mp4").status_code == 404
        assert client.head("/output/test.mp4").content == b""


def test_poisoned_media_cache_restarts_without_range(tmp_path):
    path = tmp_path / "test.mp4"
    path.write_bytes(b'{"code":9998,"message":"decode failed"}')
    assert _resume_request_headers(str(path)) == (None, 0)
    assert not path.exists()
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    assert _resume_request_headers(str(path))[1] == 12


@pytest.mark.parametrize("content_type", ["application/json", "text/html; charset=utf-8"])
def test_error_response_cannot_overwrite_media(tmp_path, content_type):
    class Response:
        headers = {"Content-Type": content_type}

    path = tmp_path / "test.wav"
    path.write_bytes(b"RIFF")
    with pytest.raises(DraftDownloadAbort):
        _write_http_body_to_file(Response(), str(path), append=False)
    assert path.read_bytes() == b"RIFF"
