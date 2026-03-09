"""Tests for Last.fm API client."""
import pytest
from unittest.mock import patch, MagicMock


def make_similar_response(artists: list[dict]) -> dict:
    return {"similarartists": {"artist": artists, "@attr": {"artist": "Test Artist"}}}


def make_info_response(listeners: int) -> dict:
    return {"artist": {"name": "Test", "stats": {"listeners": str(listeners), "playcount": "1000"}}}


class TestGetSimilarArtists:
    def test_returns_name_match_listeners(self):
        from lastfm_client import get_similar_artists
        mock_similar = make_similar_response([
            {"name": "Artist B", "match": "0.9", "mbid": ""},
            {"name": "Artist C", "match": "0.7", "mbid": ""},
        ])
        mock_info_b = make_info_response(2_000_000)
        mock_info_c = make_info_response(300_000)

        def fake_get(url, params, timeout):
            m = MagicMock()
            m.raise_for_status = MagicMock()
            if params.get("method") == "artist.getsimilar":
                m.json.return_value = mock_similar
            elif params.get("artist") == "Artist B":
                m.json.return_value = mock_info_b
            else:
                m.json.return_value = mock_info_c
            return m

        with patch("lastfm_client.requests.get", side_effect=fake_get):
            results = get_similar_artists("Seed Artist", api_key="testkey", limit=50)

        assert len(results) == 2
        names = [r["name"] for r in results]
        assert "Artist B" in names
        assert "Artist C" in names
        b = next(r for r in results if r["name"] == "Artist B")
        assert abs(b["match"] - 0.9) < 0.01
        assert b["listeners"] == 2_000_000

    def test_returns_empty_on_api_error(self):
        from lastfm_client import get_similar_artists
        with patch("lastfm_client.requests.get", side_effect=Exception("network error")):
            results = get_similar_artists("Seed Artist", api_key="testkey", limit=50)
        assert results == []

    def test_handles_missing_listeners_field(self):
        from lastfm_client import get_similar_artists
        mock_similar = make_similar_response([{"name": "Artist X", "match": "0.5", "mbid": ""}])
        mock_info = {"artist": {"name": "Artist X"}}

        def fake_get(url, params, timeout):
            m = MagicMock()
            m.raise_for_status = MagicMock()
            if params.get("method") == "artist.getsimilar":
                m.json.return_value = mock_similar
            else:
                m.json.return_value = mock_info
            return m

        with patch("lastfm_client.requests.get", side_effect=fake_get):
            results = get_similar_artists("Seed", api_key="testkey", limit=50)

        assert len(results) == 1
        assert results[0]["listeners"] == 0
