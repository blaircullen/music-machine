"""
Tests for the MusicGrabber-based upgrade_service module.
All HTTP calls are mocked via httpx mock transport.
"""
import asyncio

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
    orig = _patch_client(monkeypatch, {("GET", "/api/version"): (200, {"version": "2.2.4"})})
    assert run(us.check_connected()) is True
    monkeypatch.setattr(httpx, "AsyncClient", orig)


def test_check_connected_false(monkeypatch):
    orig = _patch_client(monkeypatch, {("GET", "/api/version"): (503, {})})
    assert run(us.check_connected()) is False
    monkeypatch.setattr(httpx, "AsyncClient", orig)


def test_classify_quality_hi_res():
    assert us._classify_quality("HI_RES_LOSSLESS") == "hi_res"
    assert us._classify_quality("HI_RES") == "hi_res"
    assert us._classify_quality(None, "FLAC 96kHz 24bit") == "hi_res"


def test_classify_quality_lossless():
    assert us._classify_quality("LOSSLESS") == "lossless"
    assert us._classify_quality(None, "FLAC 44.1kHz 16bit") == "lossless"


def test_score_search_result_exact_match():
    result = {
        "channel": "Pink Floyd",
        "title": "Time",
        "album": "The Dark Side of the Moon",
        "quality": "LOSSLESS",
        "quality_score": 200,
    }
    score = us._score_search_result(result, "Pink Floyd", "Time", "The Dark Side of the Moon")
    assert score > 500  # Should be high with exact artist + title match


def test_score_search_result_wrong_artist():
    correct = {
        "channel": "Pink Floyd",
        "title": "Time",
        "album": "The Dark Side of the Moon",
        "quality": "LOSSLESS",
        "quality_score": 200,
    }
    wrong = {
        "channel": "Lil Wayne",
        "title": "Time",
        "album": "Tha Carter V",
        "quality": "LOSSLESS",
        "quality_score": 200,
    }
    correct_score = us._score_search_result(correct, "Pink Floyd", "Time", "The Dark Side of the Moon")
    wrong_score = us._score_search_result(wrong, "Pink Floyd", "Time", "The Dark Side of the Moon")
    # Wrong artist should score significantly lower than correct artist
    assert wrong_score < correct_score


def test_normalize_text():
    assert us._normalize_text("  Hello, World!  ") == "hello world"
    assert us._normalize_text("AC/DC") == "acdc"
    assert us._normalize_text("") == ""


def test_search_for_flac_no_results(monkeypatch):
    orig = _patch_client(monkeypatch, {
        ("POST", "/api/search"): (200, {"results": []}),
    })
    result = run(us.search_for_flac("Unknown", "", "Unknown Track"))
    assert result is None
    monkeypatch.setattr(httpx, "AsyncClient", orig)


def test_search_for_flac_with_results(monkeypatch):
    orig = _patch_client(monkeypatch, {
        ("POST", "/api/search"): (200, {"results": [
            {
                "video_id": "12345",
                "title": "Time",
                "channel": "Pink Floyd",
                "album": "The Dark Side of the Moon",
                "quality": "LOSSLESS",
                "quality_score": 300,
                "source_url": "https://monochrome.tf/track/12345",
                "slskd_username": None,
                "slskd_filename": None,
            }
        ]}),
    })
    result = run(us.search_for_flac("Pink Floyd", "The Dark Side of the Moon", "Time"))
    assert result is not None
    assert result["mg_track_id"] == "12345"
    assert result["match_quality"] == "lossless"
    monkeypatch.setattr(httpx, "AsyncClient", orig)
