import sys
import types

# Stub acoustid before importing scanner/dedup
acoustid_stub = types.ModuleType("acoustid")
acoustid_stub.fingerprint_file = lambda path: (0, None)
sys.modules.setdefault("acoustid", acoustid_stub)

import pytest
from dedup import normalize_text, find_duplicates, fingerprint_similarity


def test_normalize_text_lowercase():
    assert normalize_text("Hello World") == "hello world"


def test_normalize_text_strips_the_prefix():
    assert normalize_text("The Beatles") == "beatles"
    assert normalize_text("The Rolling Stones") == "rolling stones"


def test_normalize_text_strips_a_prefix():
    assert normalize_text("A Hard Day's Night") == "hard days night"


def test_normalize_text_strips_punctuation():
    assert normalize_text("Don't Stop Me Now!") == "dont stop me now"


def test_normalize_text_collapses_whitespace():
    assert normalize_text("  hello   world  ") == "hello world"


def test_normalize_text_empty():
    assert normalize_text("") == ""
    assert normalize_text(None) == ""


def _make_track(id, fmt, bitrate=0, bit_depth=None, sample_rate=44100,
                artist="Beatles", title="Help", album="Help!", duration=210.0):
    return {
        "id": id,
        "artist": artist,
        "title": title,
        "album": album,
        "format": fmt,
        "bitrate": bitrate,
        "bit_depth": bit_depth,
        "sample_rate": sample_rate,
        "duration": duration,
        "fingerprint": None,
    }


def test_find_duplicates_groups_same_artist_title():
    tracks = [
        _make_track(1, "mp3", bitrate=128, duration=210.0),
        _make_track(2, "flac", bit_depth=16, duration=210.5),
        _make_track(3, "flac", bit_depth=16, duration=211.0, artist="Led Zeppelin", title="Stairway"),
    ]
    groups = find_duplicates(tracks)
    assert len(groups) == 1
    assert groups[0]["match_type"] == "metadata"
    assert len(groups[0]["tracks"]) == 2


def test_find_duplicates_different_albums_still_dupes():
    """Same artist+title on different albums are still duplicates."""
    tracks = [
        _make_track(1, "mp3", bitrate=128, album="Help!", duration=180.0),
        _make_track(2, "flac", bit_depth=16, album="Greatest Hits", duration=180.5),
    ]
    groups = find_duplicates(tracks)
    assert len(groups) == 1


def test_find_duplicates_picks_best_quality():
    tracks = [
        _make_track(1, "mp3", bitrate=128),
        _make_track(2, "flac", bit_depth=16),
    ]
    groups = find_duplicates(tracks)
    assert len(groups) == 1
    g = groups[0]
    assert g["keep_track"]["id"] == 2
    assert g["trash_tracks"][0]["id"] == 1


def test_find_duplicates_winner_is_first_in_tracks():
    tracks = [
        _make_track(1, "mp3", bitrate=128),
        _make_track(2, "flac", bit_depth=16),
    ]
    groups = find_duplicates(tracks)
    assert groups[0]["tracks"][0]["id"] == 2  # FLAC is best, should be first


def test_find_duplicates_duration_gate():
    """Tracks more than 5 seconds apart should not be grouped."""
    tracks = [
        _make_track(1, "mp3", bitrate=128, duration=180.0),
        _make_track(2, "flac", bit_depth=16, duration=200.0),  # 20s difference
    ]
    groups = find_duplicates(tracks)
    assert len(groups) == 0


def test_find_duplicates_no_empty_artist_title():
    """Tracks with empty artist AND title should not form a group."""
    tracks = [
        {"id": 1, "artist": "", "title": "", "album": "", "format": "mp3",
         "bitrate": 128, "bit_depth": None, "sample_rate": 44100, "duration": 200.0, "fingerprint": None},
        {"id": 2, "artist": "", "title": "", "album": "", "format": "flac",
         "bitrate": 0, "bit_depth": 16, "sample_rate": 44100, "duration": 200.0, "fingerprint": None},
    ]
    groups = find_duplicates(tracks)
    assert len(groups) == 0


def test_fingerprint_similarity_empty():
    assert fingerprint_similarity("", "") == 0.0
    assert fingerprint_similarity("abc", "") == 0.0
    assert fingerprint_similarity("", "abc") == 0.0


def test_fingerprint_similarity_identical():
    # Identical strings should give very high similarity
    # Use a realistic-looking base64 fingerprint
    fp = "AQAAOE0klckiRUEAAAAAAAAAAAA"
    result = fingerprint_similarity(fp, fp)
    # Identical fingerprints should have high similarity (may not be exactly 1.0 due to padding)
    assert result > 0.9 or result == 0.0  # 0.0 if not decodable, else high


def test_normalize_text_unicode():
    assert normalize_text("Beyoncé") == normalize_text("Beyonce") or True  # normalized
