"""
Unit U1 kill switch tests — freeze hardening + identity_act_enabled gate.

Covers:
1. identity_act_enabled defaults to false after init_db
2. _migrate_freeze_upgrade_queue: parks non-terminal rows, leaves terminal rows,
   idempotent, sets freeze_migration_version = '1'
3. _auto_fix_track with setting false → marks flagged, no write attempted
4. write_metadata with setting false → raises ValueError before any write
5. Setting true restores: _auto_fix_track proceeds past the gate
6. identity_act_enabled reads fresh value mid-test
"""

import pytest
import database
from database import get_db, init_db, identity_act_enabled, _migrate_freeze_upgrade_queue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_setting(key, value):
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def _get_setting_raw(key):
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Test 1: default is false
# ---------------------------------------------------------------------------

def test_identity_act_enabled_default_false(init_test_db):
    """identity_act_enabled should be 'false' (returns False) right after init_db."""
    assert _get_setting_raw("identity_act_enabled") == "false"
    assert identity_act_enabled() is False


# ---------------------------------------------------------------------------
# Test 2: freeze migration
# ---------------------------------------------------------------------------

def test_migrate_freeze_upgrade_queue(init_test_db):
    """
    Non-terminal rows (pending/searching/found/approved/downloading) become 'frozen'.
    Terminal rows (completed, failed) are untouched.
    freeze_migration_version is set to '1'.
    Running again is idempotent.
    """
    non_terminal = ["pending", "searching", "found", "approved", "downloading"]
    terminal = ["completed", "failed"]

    # Reset version marker so migration runs fresh
    _set_setting("freeze_migration_version", "0")

    inserted_ids = {}
    with get_db() as db:
        # get_db() enables PRAGMA foreign_keys=ON — parent track row required.
        db.execute(
            "INSERT OR IGNORE INTO tracks (id, file_path) VALUES (999, '/fake/track.flac')"
        )
        for status in non_terminal + terminal:
            cur = db.execute(
                "INSERT INTO upgrade_queue (track_id, status) VALUES (999, ?)",
                (status,),
            )
            inserted_ids[status] = cur.lastrowid

    with get_db() as db:
        _migrate_freeze_upgrade_queue(db)

    # Verify non-terminal rows are now frozen
    with get_db() as db:
        for status in non_terminal:
            row = db.execute(
                "SELECT status FROM upgrade_queue WHERE id = ?",
                (inserted_ids[status],),
            ).fetchone()
            assert row[0] == "frozen", f"expected 'frozen' for originally-'{status}' row"

        # Terminal rows untouched
        for status in terminal:
            row = db.execute(
                "SELECT status FROM upgrade_queue WHERE id = ?",
                (inserted_ids[status],),
            ).fetchone()
            assert row[0] == status, f"expected '{status}' to be unchanged"

    assert _get_setting_raw("freeze_migration_version") == "1"

    # Idempotency: run again — frozen rows stay frozen, terminal stay terminal
    with get_db() as db:
        _migrate_freeze_upgrade_queue(db)

    with get_db() as db:
        for status in non_terminal:
            row = db.execute(
                "SELECT status FROM upgrade_queue WHERE id = ?",
                (inserted_ids[status],),
            ).fetchone()
            assert row[0] == "frozen"
        for status in terminal:
            row = db.execute(
                "SELECT status FROM upgrade_queue WHERE id = ?",
                (inserted_ids[status],),
            ).fetchone()
            assert row[0] == status


# ---------------------------------------------------------------------------
# Test 3: _auto_fix_track with kill switch false → no write, result flagged
# ---------------------------------------------------------------------------

def test_auto_fix_track_blocked_when_disabled(init_test_db, monkeypatch):
    """When identity_act_enabled is false, _auto_fix_track must not attempt any
    tag write — snapshot_tags and write_metadata must never be called."""
    _set_setting("identity_act_enabled", "false")

    import fingerprint_engine

    write_called = []
    snapshot_called = []

    monkeypatch.setattr(fingerprint_engine, "snapshot_tags", lambda *a, **kw: snapshot_called.append(a) or 99)
    monkeypatch.setattr(fingerprint_engine, "write_metadata", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("write_metadata must not be called")))

    # Track the status update
    status_updates = []
    original_update = fingerprint_engine._update_fp_status
    monkeypatch.setattr(
        fingerprint_engine,
        "_update_fp_status",
        lambda fp_id, status, **kw: status_updates.append(status),
    )

    # Insert a parent tracks row (FK) + dummy fingerprint_results row
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO tracks (id, file_path) VALUES (999, '/fake/track.flac')"
        )
        cur = db.execute(
            """INSERT INTO fingerprint_results (track_id, status)
               VALUES (999, 'pending')"""
        )
        fp_result_id = cur.lastrowid

    fingerprint_engine._auto_fix_track(
        track_id=999,
        file_path="/fake/track.flac",
        fp_result_id=fp_result_id,
        metadata={},
        genre="Rock",
        recording_id="fake-uuid",
    )

    assert snapshot_called == [], "snapshot_tags must not be called when kill switch is off"
    assert "flagged" in status_updates, "result must be marked 'flagged'"


# ---------------------------------------------------------------------------
# Test 4: write_metadata with kill switch false → raises before any write
# ---------------------------------------------------------------------------

def test_write_metadata_blocked_when_disabled(init_test_db, monkeypatch):
    """write_metadata must raise ValueError before touching mutagen when kill switch is false."""
    _set_setting("identity_act_enabled", "false")

    import tagger

    # Monkeypatch mutagen File to assert it is never called
    monkeypatch.setattr(
        tagger,
        "MutagenFile",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("MutagenFile must not be called")),
    )

    with pytest.raises(ValueError, match="identity_act_enabled is false"):
        tagger.write_metadata("/fake/track.flac", {"artist": "Test"})


# ---------------------------------------------------------------------------
# Test 5: setting true restores — _auto_fix_track proceeds past the gate
# ---------------------------------------------------------------------------

def test_auto_fix_track_allowed_when_enabled(init_test_db, monkeypatch):
    """When identity_act_enabled is true, _auto_fix_track passes the gate and calls snapshot_tags."""
    _set_setting("identity_act_enabled", "true")

    import fingerprint_engine

    snapshot_called = []

    # snapshot_tags returns None → triggers the existing "snapshot failed" early-exit path,
    # which is fine — we only need to confirm the kill-switch gate was passed.
    monkeypatch.setattr(
        fingerprint_engine, "snapshot_tags", lambda *a, **kw: snapshot_called.append(a) or None
    )

    status_updates = []
    monkeypatch.setattr(
        fingerprint_engine,
        "_update_fp_status",
        lambda fp_id, status, **kw: status_updates.append(status),
    )

    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO tracks (id, file_path) VALUES (998, '/fake/track2.flac')"
        )
        cur = db.execute(
            "INSERT INTO fingerprint_results (track_id, status) VALUES (998, 'pending')"
        )
        fp_result_id = cur.lastrowid

    fingerprint_engine._auto_fix_track(
        track_id=998,
        file_path="/fake/track2.flac",
        fp_result_id=fp_result_id,
        metadata={},
        genre="Rock",
        recording_id="fake-uuid",
    )

    assert len(snapshot_called) == 1, "snapshot_tags should be called when kill switch is on"
    # snapshot returned None → _auto_fix_track marks 'flagged' and returns (expected path)
    assert "flagged" in status_updates

    # Clean up for other tests
    _set_setting("identity_act_enabled", "false")


# ---------------------------------------------------------------------------
# Test 6: identity_act_enabled reads fresh value
# ---------------------------------------------------------------------------

def test_identity_act_enabled_reads_fresh(init_test_db):
    """Helper must reflect in-DB changes immediately with no caching."""
    _set_setting("identity_act_enabled", "false")
    assert identity_act_enabled() is False

    _set_setting("identity_act_enabled", "true")
    assert identity_act_enabled() is True

    _set_setting("identity_act_enabled", "false")
    assert identity_act_enabled() is False
