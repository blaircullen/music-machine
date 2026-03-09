"""Tests for Pandora station recommendation engine."""
import json
import pytest
from unittest.mock import patch, MagicMock


def _make_station(
    seed_artists=None,
    bpm_min=None, bpm_max=None,
    decade_min=None, decade_max=None,
    lastfm_min_listeners=500_000,
):
    return {
        "id": 1,
        "name": "Test Station",
        "seed_artists": json.dumps(seed_artists or ["Tom Petty"]),
        "bpm_min": bpm_min,
        "bpm_max": bpm_max,
        "decade_min": decade_min,
        "decade_max": decade_max,
        "plex_playlist_name": "Test Station",
        "lastfm_min_listeners": lastfm_min_listeners,
    }


def _make_similar(name, match, listeners):
    return {"name": name, "match": match, "listeners": listeners}


def _make_track(rating_key, bpm=None, year=None):
    return {"ratingKey": rating_key, "title": "Track", "bpm": bpm, "year": year}


class TestBuildCandidates:
    def test_filters_artists_below_listener_threshold(self):
        from stations_service import _build_candidates

        similar = [
            _make_similar("Popular Artist", 0.9, 2_000_000),
            _make_similar("Obscure Band", 0.8, 100_000),   # below 500k
        ]
        plex_tracks = {
            "Popular Artist": [_make_track("rk1")],
            "Obscure Band": [_make_track("rk2")],
        }
        candidates = _build_candidates(similar, plex_tracks, _make_station())
        keys = [c["ratingKey"] for c in candidates]
        assert "rk1" in keys
        assert "rk2" not in keys

    def test_bpm_filter_excludes_out_of_range(self):
        from stations_service import _build_candidates

        similar = [_make_similar("Artist", 0.9, 1_000_000)]
        plex_tracks = {
            "Artist": [
                _make_track("low_bpm", bpm=90),    # excluded
                _make_track("ok_bpm", bpm=130),    # included
                _make_track("no_bpm", bpm=None),   # included (fail-open)
            ]
        }
        station = _make_station(bpm_min=120, bpm_max=160)
        candidates = _build_candidates(similar, plex_tracks, station)
        keys = [c["ratingKey"] for c in candidates]
        assert "ok_bpm" in keys
        assert "no_bpm" in keys   # untagged passes through
        assert "low_bpm" not in keys

    def test_decade_filter(self):
        from stations_service import _build_candidates

        similar = [_make_similar("Artist", 0.9, 1_000_000)]
        plex_tracks = {
            "Artist": [
                _make_track("r80s", year=1985),   # excluded
                _make_track("r90s", year=1994),   # included
                _make_track("no_yr", year=None),  # included (fail-open)
            ]
        }
        station = _make_station(decade_min=1990, decade_max=1999)
        candidates = _build_candidates(similar, plex_tracks, station)
        keys = [c["ratingKey"] for c in candidates]
        assert "r90s" in keys
        assert "no_yr" in keys
        assert "r80s" not in keys


class TestRecencyWeight:
    def test_recent_tracks_get_reduced_weight(self):
        from stations_service import _apply_recency_weights

        candidates = [
            {"ratingKey": "recent", "weight": 1.0},
            {"ratingKey": "old", "weight": 1.0},
        ]
        recent_keys = {"recent"}
        result = _apply_recency_weights(candidates, recent_keys)
        recent_c = next(c for c in result if c["ratingKey"] == "recent")
        old_c = next(c for c in result if c["ratingKey"] == "old")
        assert abs(recent_c["weight"] - 0.3) < 0.01
        assert abs(old_c["weight"] - 1.0) < 0.01


class TestWeightedSample:
    def test_returns_requested_count(self):
        from stations_service import _weighted_sample

        candidates = [{"ratingKey": str(i), "weight": 1.0} for i in range(100)]
        result = _weighted_sample(candidates, n=40)
        assert len(result) == 40

    def test_returns_all_when_fewer_candidates(self):
        from stations_service import _weighted_sample

        candidates = [{"ratingKey": str(i), "weight": 1.0} for i in range(10)]
        result = _weighted_sample(candidates, n=40)
        assert len(result) == 10

    def test_no_duplicates(self):
        from stations_service import _weighted_sample

        candidates = [{"ratingKey": str(i), "weight": float(i + 1)} for i in range(50)]
        result = _weighted_sample(candidates, n=40)
        assert len(result) == len(set(result))
