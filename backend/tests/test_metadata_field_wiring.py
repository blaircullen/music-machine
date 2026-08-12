"""
Regression tests for P1 fixes #3 and #4 (2026-08-12 review of commit
48ec988): isrc/label/composer were captured on write but dropped on both
the manual-approve and rollback round trips.
"""

import shutil
from pathlib import Path

from database import get_db

FIXTURES = Path(__file__).parent / "fixtures"


def _copy_fixture(tmp_path, name="test_16_44.flac"):
    dest = tmp_path / name
    shutil.copy(FIXTURES / name, dest)
    return str(dest)


# ---------------------------------------------------------------------------
# Fix #3 — POST /review/{result_id}/approve must carry isrc/label/composer
# ---------------------------------------------------------------------------


def test_approve_result_carries_isrc_label_composer(monkeypatch):
    from routes import fingerprint as fingerprint_routes

    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO tracks (id, file_path) VALUES (5001, '/fake/approve.flac')"
        )
        cur = db.execute(
            """INSERT INTO fingerprint_results
               (track_id, status, matched_artist, matched_title, matched_album,
                matched_isrc, matched_label, matched_composer)
               VALUES (5001, 'flagged', 'Artist', 'Title', 'Album',
                       'USRC17607839', 'Test Records', 'J. Composer')"""
        )
        result_id = cur.lastrowid

    captured = {}

    def fake_auto_fix(track_id, file_path, fp_result_id, metadata, genre, recording_id):
        captured["metadata"] = metadata

    # approve_result does `from fingerprint_engine import _auto_fix_track` inline,
    # so patching must happen on the fingerprint_engine module (not the route module).
    import fingerprint_engine
    monkeypatch.setattr(fingerprint_engine, "_auto_fix_track", fake_auto_fix)

    result = fingerprint_routes.approve_result(result_id)

    assert result["ok"] is True
    assert captured["metadata"]["isrc"] == "USRC17607839"
    assert captured["metadata"]["label"] == "Test Records"
    assert captured["metadata"]["composer"] == "J. Composer"


# ---------------------------------------------------------------------------
# Fix #4 — rollback_tags() must restore isrc/label/composer
# ---------------------------------------------------------------------------


def test_rollback_tags_restores_isrc_label_composer(tmp_path, monkeypatch):
    import tag_backup

    file_path = _copy_fixture(tmp_path)

    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO tracks (id, file_path) VALUES (5002, ?)",
            (file_path,),
        )
        cur = db.execute(
            """INSERT INTO tag_snapshots
               (track_id, original_artist, original_title, original_album,
                original_isrc, original_label, original_composer)
               VALUES (5002, 'Original Artist', 'Original Title', 'Original Album',
                       'GBUM71029601', 'Original Label', 'Original Composer')"""
        )
        snapshot_id = cur.lastrowid

    captured = {}
    # rollback_tags does `from tagger import write_metadata` inline, so
    # patching must happen on the tagger module (not tag_backup).
    import tagger
    monkeypatch.setattr(
        tagger,
        "write_metadata",
        lambda *a, **kw: captured.setdefault("metadata", a[1]) or ("h1", "h2"),
    )

    ok = tag_backup.rollback_tags(snapshot_id)

    assert ok is True
    assert captured["metadata"]["isrc"] == "GBUM71029601"
    assert captured["metadata"]["label"] == "Original Label"
    assert captured["metadata"]["composer"] == "Original Composer"
