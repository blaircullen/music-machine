"""
Regression tests for the P1 collision-detection fixes (2026-08-12 review of
commit 48ec988):

1. tagger.tag_file() now routes through the same AcoustID-collision +
   artist-reattribution guard as fingerprint_engine.py, instead of blindly
   trusting the top AcoustID candidate (disambiguator.resolve_match_candidates).
2. disambiguator.text_differs() (formerly fingerprint_engine._artist_differs)
   no longer has the blind substring-containment shortcut, and its
   similarity floor is raised from 0.6 to 0.88 to catch confirmed false
   negatives, while still not tripping on legitimate formatting variants.
"""

import shutil
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from disambiguator import resolve_match_candidates, text_differs

FIXTURES = Path(__file__).parent / "fixtures"


def _copy_fixture(tmp_path, name="test_16_44.flac"):
    dest = tmp_path / name
    shutil.copy(FIXTURES / name, dest)
    return str(dest)


# ---------------------------------------------------------------------------
# Fix #2 — text_differs() floor + shortcut removal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("existing,new", [
    ("The Beat", "The Beatles"),
    ("Muse", "Museum"),
    ("David Bowie", "David Byrne"),
    ("America", "American Authors"),
])
def test_text_differs_catches_confirmed_false_negatives(existing, new):
    """These 4 pairs scored below the old 0.6 floor (i.e. read as 'not
    different') and, for the first two, were also masked by the old blind
    substring-containment shortcut. All 4 must now read as different."""
    assert text_differs(existing, new) is True


@pytest.mark.parametrize("existing,new", [
    ("The Beatles", "Beatles, The"),
    ("Beyoncé", "Beyonce"),
])
def test_text_differs_does_not_false_positive_on_formatting_variants(existing, new):
    """Legitimate formatting variants (word order, diacritics) must still
    read as the same artist/title at the raised floor."""
    assert text_differs(existing, new) is False


def test_text_differs_no_longer_has_blind_substring_shortcut():
    """The dropped 'a in b or b in a' shortcut used to make a genuine
    subset-name collision (a different, larger act) read as 'not
    different'. Confirm it's gone."""
    assert text_differs("Chicago", "Chicago Symphony Orchestra") is True
    assert text_differs("Now", "Now United") is True


def test_text_differs_identical_and_empty():
    assert text_differs("The Beatles", "The Beatles") is False
    assert text_differs("", "The Beatles") is False
    assert text_differs("The Beatles", "") is False


# ---------------------------------------------------------------------------
# Fix #1 — resolve_match_candidates() shared collision resolution
# ---------------------------------------------------------------------------


def test_resolve_match_candidates_unambiguous_picks_top_score():
    tier_matches = [{"recording_id": "rec-a", "score": 0.95}]
    fetch = {"rec-a": {"artist": "A", "title": "Song", "release_id": "rel-a"}}.get

    metadata, recording_id, ambiguous = resolve_match_candidates(tier_matches, fetch)

    assert ambiguous is False
    assert recording_id == "rec-a"
    assert metadata["artist"] == "A"


def test_resolve_match_candidates_collision_is_flagged_ambiguous():
    """Two candidates within score_margin of each other must be reported as
    ambiguous, even though the disambiguator still returns its best pick —
    callers (fingerprint_engine, tagger.tag_file) are responsible for
    routing an ambiguous result to human review rather than auto-writing."""
    tier_matches = [
        {"recording_id": "rec-a", "score": 0.95},
        {"recording_id": "rec-b", "score": 0.94},
    ]
    candidates = {
        "rec-a": {"artist": "Artist A", "title": "Song A", "album": "Album A",
                  "release_id": "rel-a", "release_group_id": "rg-a"},
        "rec-b": {"artist": "Artist B", "title": "Song B", "album": "Album B",
                  "release_id": "rel-b", "release_group_id": "rg-b"},
    }

    metadata, recording_id, ambiguous = resolve_match_candidates(
        tier_matches, candidates.get, score_margin=0.02,
    )

    assert ambiguous is True
    assert metadata is not None
    assert recording_id in ("rec-a", "rec-b")


def test_resolve_match_candidates_not_ambiguous_when_score_gap_exceeds_margin():
    tier_matches = [
        {"recording_id": "rec-a", "score": 0.95},
        {"recording_id": "rec-b", "score": 0.50},
    ]
    fetch = {"rec-a": {"artist": "A", "title": "Song", "release_id": "rel-a"}}.get

    metadata, recording_id, ambiguous = resolve_match_candidates(
        tier_matches, fetch, score_margin=0.02,
    )

    assert ambiguous is False
    assert recording_id == "rec-a"


# ---------------------------------------------------------------------------
# Fix #1 — tagger.tag_file() end-to-end guard (legacy pipeline)
# ---------------------------------------------------------------------------


def test_tag_file_blocks_on_ambiguous_collision(tmp_path, monkeypatch):
    import tagger

    file_path = _copy_fixture(tmp_path)

    monkeypatch.setattr(tagger, "generate_fingerprint_with_duration", lambda p: ("fake-fp", 180.0))
    monkeypatch.setattr(tagger, "has_mb_recording_id", lambda p: False)
    monkeypatch.setattr(
        tagger, "lookup_acoustid",
        lambda fp, dur: [
            {"recording_id": "rec-a", "score": 0.95, "below_floor": False},
            {"recording_id": "rec-b", "score": 0.94, "below_floor": False},
        ],
    )

    def fake_mb(rec_id):
        if rec_id == "rec-a":
            return {"artist": "Artist A", "title": "Song A", "album": "Album A",
                    "release_id": "rel-a", "release_group_id": "rg-a"}
        return {"artist": "Artist B", "title": "Song B", "album": "Album B",
                "release_id": "rel-b", "release_group_id": "rg-b"}

    monkeypatch.setattr(tagger, "lookup_musicbrainz", fake_mb)

    write_called = []
    monkeypatch.setattr(tagger, "write_metadata", lambda *a, **kw: write_called.append(a) or ("h1", "h2"))

    result = tagger.tag_file(file_path)

    assert result["status"] == "failed"
    assert "ambiguous=True" in result["error_msg"]
    assert write_called == [], "must not blindly write tags on an unresolved AcoustID score collision"


def test_tag_file_blocks_on_artist_reattribution(tmp_path, monkeypatch):
    import tagger

    file_path = _copy_fixture(tmp_path)
    audio = FLAC(file_path)
    audio["artist"] = ["The Beat"]
    audio.save()

    monkeypatch.setattr(tagger, "generate_fingerprint_with_duration", lambda p: ("fake-fp", 180.0))
    monkeypatch.setattr(tagger, "has_mb_recording_id", lambda p: False)
    monkeypatch.setattr(
        tagger, "lookup_acoustid",
        lambda fp, dur: [{"recording_id": "rec-a", "score": 0.95, "below_floor": False}],
    )
    monkeypatch.setattr(
        tagger, "lookup_musicbrainz",
        lambda rec_id: {"artist": "The Beatles", "title": "Song", "album": "Album",
                         "release_id": "rel-a", "release_group_id": "rg-a"},
    )
    write_called = []
    monkeypatch.setattr(tagger, "write_metadata", lambda *a, **kw: write_called.append(a) or ("h1", "h2"))

    result = tagger.tag_file(file_path)

    assert result["status"] == "failed"
    assert "artist_changed=True" in result["error_msg"]
    assert write_called == [], "must not blindly overwrite a different artist without review"


def test_tag_file_proceeds_when_unambiguous_and_same_artist(tmp_path, monkeypatch):
    """Sanity check: the new guard should not block an ordinary, unambiguous,
    same-artist (formatting-variant) match."""
    import tagger

    file_path = _copy_fixture(tmp_path)
    audio = FLAC(file_path)
    audio["artist"] = ["The Beatles"]
    audio.save()

    monkeypatch.setattr(tagger, "generate_fingerprint_with_duration", lambda p: ("fake-fp", 180.0))
    monkeypatch.setattr(tagger, "has_mb_recording_id", lambda p: False)
    monkeypatch.setattr(
        tagger, "lookup_acoustid",
        lambda fp, dur: [{"recording_id": "rec-a", "score": 0.95, "below_floor": False}],
    )
    monkeypatch.setattr(
        tagger, "lookup_musicbrainz",
        lambda rec_id: {"artist": "Beatles, The", "title": "Song", "album": "Album",
                         "release_id": "rel-a", "release_group_id": "rg-a"},
    )
    monkeypatch.setattr(tagger, "fetch_cover_art", lambda rgid: None)
    write_called = []
    monkeypatch.setattr(tagger, "write_metadata", lambda *a, **kw: write_called.append(a) or ("h1", "h2"))

    result = tagger.tag_file(file_path)

    assert result["status"] == "tagged"
    assert len(write_called) == 1
