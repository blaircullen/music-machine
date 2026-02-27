"""
Tests for the slskd-based upgrade_service module.
All HTTP calls are mocked via httpx mock transport.
"""
import asyncio
import sys

import pytest
import httpx

import upgrade_service as us


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patch_client(monkeypatch, routes: dict):
    """
    Returns a context that patches httpx.AsyncClient to use a mock transport.
    routes: dict of {(METHOD, path_prefix): (status_code, json_body)}
    """
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            for (method, path), (status, body) in routes.items():
                if request.method == method and request.url.path.startswith(path):
                    return httpx.Response(status, json=body)
            return httpx.Response(404, json={"detail": "not found"})

    original_client = httpx.AsyncClient

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(transport=MockTransport(), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", PatchedClient)
    return original_client


def test_check_connected_true(monkeypatch):
    orig = _patch_client(monkeypatch, {("GET", "/api/v0/application"): (200, {"status": "ok"})})
    assert run(us.check_connected()) is True
    monkeypatch.setattr(httpx, "AsyncClient", orig)


def test_check_connected_false(monkeypatch):
    orig = _patch_client(monkeypatch, {("GET", "/api/v0/application"): (503, {})})
    assert run(us.check_connected()) is False
    monkeypatch.setattr(httpx, "AsyncClient", orig)


def test_download_file_success(monkeypatch):
    orig = _patch_client(monkeypatch, {("POST", "/api/v0/transfers/downloads/"): (201, {})})
    result = run(us.download_file("testuser", "/music/Artist/song.flac", 12345678))
    assert result is True
    monkeypatch.setattr(httpx, "AsyncClient", orig)


def test_download_file_failure(monkeypatch):
    orig = _patch_client(monkeypatch, {("POST", "/api/v0/transfers/downloads/"): (400, {"error": "bad"})})
    result = run(us.download_file("testuser", "/music/Artist/song.flac"))
    assert result is False
    monkeypatch.setattr(httpx, "AsyncClient", orig)


def test_score_slskd_result_hi_res_marker():
    entry = {"filename": "/music/Artist/Album [24bit]/song.flac", "size": 90_000_000, "uploadSpeed": 0}
    score = us._score_slskd_result(entry)
    assert score >= 1000


def test_score_slskd_result_no_markers():
    entry = {"filename": "/music/Artist/Album/song.flac", "size": 5_000_000, "uploadSpeed": 0}
    score = us._score_slskd_result(entry)
    assert score == 0


def test_score_slskd_result_large_file_bonus():
    entry = {"filename": "/music/Artist/Album/song.flac", "size": 60_000_000, "uploadSpeed": 0}
    score = us._score_slskd_result(entry)
    assert score >= 100


def test_score_slskd_result_medium_file_bonus():
    entry = {"filename": "/music/Artist/Album/song.flac", "size": 25_000_000, "uploadSpeed": 0}
    score = us._score_slskd_result(entry)
    assert score >= 50


def test_classify_match_quality_hi_res_24bit():
    entry = {"filename": "/music/Album [24bit]/song.flac", "size": 30_000_000}
    assert us._classify_match_quality(entry) == "hi_res"


def test_classify_match_quality_lossless():
    entry = {"filename": "/music/Album/song.flac", "size": 25_000_000}
    assert us._classify_match_quality(entry) == "lossless"


def test_classify_match_quality_very_large_file():
    entry = {"filename": "/music/Album/song.flac", "size": 100_000_000}
    assert us._classify_match_quality(entry) == "hi_res"


def test_get_download_status_completed(monkeypatch):
    transfers = [
        {
            "files": [
                {
                    "filename": "/music/Artist/song.flac",
                    "state": "Completed",
                    "bytesTransferred": 12345678,
                    "size": 12345678,
                    "localFilename": "/downloads/Artist/song.flac",
                }
            ]
        }
    ]
    orig = _patch_client(monkeypatch, {("GET", "/api/v0/transfers/downloads/"): (200, transfers)})
    result = run(us.get_download_status("testuser", "/music/Artist/song.flac"))
    assert result is not None
    assert result["state"] == "Completed"
    assert result["local_filename"] == "/downloads/Artist/song.flac"
    monkeypatch.setattr(httpx, "AsyncClient", orig)


def test_get_download_status_not_found(monkeypatch):
    orig = _patch_client(monkeypatch, {("GET", "/api/v0/transfers/downloads/"): (200, [])})
    result = run(us.get_download_status("testuser", "missing.flac"))
    assert result is None
    monkeypatch.setattr(httpx, "AsyncClient", orig)


def test_get_download_status_matches_by_basename(monkeypatch):
    """Should find a file by matching just the basename."""
    transfers = [
        {
            "files": [
                {
                    "filename": "/long/path/on/slskd/song.flac",
                    "state": "InProgress",
                    "bytesTransferred": 5000000,
                    "size": 12345678,
                    "localFilename": "/downloads/path/song.flac",
                }
            ]
        }
    ]
    orig = _patch_client(monkeypatch, {("GET", "/api/v0/transfers/downloads/"): (200, transfers)})
    result = run(us.get_download_status("testuser", "/long/path/on/slskd/song.flac"))
    assert result is not None
    assert result["state"] == "InProgress"
    monkeypatch.setattr(httpx, "AsyncClient", orig)
