"""
U7 dedup-pass tests — identity gate + fail-closed apply.

Covers:
1. _migrate_dedup_actions creates the table; dedup_act_enabled defaults false.
2. find_dedup_candidates: fingerprint lossless-keeps-lossy group is auto_eligible.
3. Gate: a 'conflict' member skips the group; divergent recordings skip the group.
4. Metadata-only match is a candidate but NOT auto_eligible.
5. apply_dedup is FAIL-CLOSED: PermissionError unless BOTH flags true; dry_run never trashes.
6. apply_dedup with both flags on trashes via file_txn and writes a dedup_actions row.
"""

import pytest

import database
import dedup_pass
from database import get_db, dedup_act_enabled

# A valid base64 fingerprint; identical on both members → similarity 1.0 → 'fingerprint' match.
FP = "AAAABBBBCCCC"


def _set_setting(key, value):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


@pytest.fixture(autouse=True)
def _clean(init_test_db):
    """Isolate each test: wipe library + identity + action rows and reset the gate flags."""
    with get_db() as db:
        for t in ("dedup_actions", "track_identity", "upgrade_queue", "tracks"):
            db.execute(f"DELETE FROM {t}")
    _set_setting("identity_act_enabled", "false")
    _set_setting("dedup_act_enabled", "false")
    yield


def _insert_track(*, file_path, fmt, artist, title, duration=200.0, fingerprint=None,
                  bit_depth=None, sample_rate=None, bitrate=None, status="active"):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO tracks
                   (file_path, format, artist, title, duration, fingerprint,
                    bit_depth, sample_rate, bitrate, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (file_path, fmt, artist, title, duration, fingerprint,
             bit_depth, sample_rate, bitrate, status),
        )
        return cur.lastrowid


def _set_identity(track_id, state, mb_recording_id="rec-x"):
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO track_identity
                   (track_id, state, mb_recording_id, evidence)
               VALUES (?, ?, ?, '{}')""",
            (track_id, state, mb_recording_id),
        )


# ---------------------------------------------------------------------------
# 1. migration + default flag
# ---------------------------------------------------------------------------

def test_dedup_actions_table_and_default_flag():
    with get_db() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(dedup_actions)").fetchall()}
    assert {"keep_id", "trashed_id", "match_type", "confidence", "sha_before",
            "rolled_back", "rolled_back_at"} <= cols
    assert dedup_act_enabled() is False


# ---------------------------------------------------------------------------
# 2. auto-eligible group (the Oasis pattern)
# ---------------------------------------------------------------------------

def test_auto_eligible_fingerprint_lossless_over_lossy():
    flac = _insert_track(file_path="/m/a.flac", fmt="flac", artist="Oasis",
                         title="Don't Go Away", fingerprint=FP, bit_depth=16,
                         sample_rate=44100, bitrate=900)
    mp3 = _insert_track(file_path="/m/a.mp3", fmt="mp3", artist="Oasis",
                        title="Don't Go Away", fingerprint=FP, bitrate=221)

    cands = dedup_pass.find_dedup_candidates()
    assert len(cands) == 1
    c = cands[0]
    assert c["match_type"] == "fingerprint"
    assert c["auto_eligible"] is True
    assert c["keep_id"] == flac
    assert c["trash_ids"] == [mp3]


# ---------------------------------------------------------------------------
# 3. identity gate — conflict + divergent recordings both skip
# ---------------------------------------------------------------------------

def test_conflict_member_skips_group():
    flac = _insert_track(file_path="/m/b.flac", fmt="flac", artist="X", title="Song",
                         fingerprint=FP, bit_depth=16, sample_rate=44100)
    _insert_track(file_path="/m/b.mp3", fmt="mp3", artist="X", title="Song",
                  fingerprint=FP, bitrate=200)
    _set_identity(flac, "conflict")

    assert dedup_pass.find_dedup_candidates() == []
    skipped = dedup_pass.find_dedup_candidates(include_skipped=True)
    assert len(skipped) == 1 and skipped[0]["skip_reason"] == "conflict"


def test_divergent_recordings_skip_group():
    a = _insert_track(file_path="/m/c.flac", fmt="flac", artist="Y", title="Tune",
                      fingerprint=FP, bit_depth=16, sample_rate=44100)
    b = _insert_track(file_path="/m/c.mp3", fmt="mp3", artist="Y", title="Tune",
                      fingerprint=FP, bitrate=200)
    _set_identity(a, "confirmed", mb_recording_id="rec-1")
    _set_identity(b, "confirmed", mb_recording_id="rec-2")

    skipped = dedup_pass.find_dedup_candidates(include_skipped=True)
    assert len(skipped) == 1 and skipped[0]["skip_reason"] == "divergent_recordings"
    assert dedup_pass.find_dedup_candidates() == []


# ---------------------------------------------------------------------------
# 4. metadata-only match — candidate but NOT auto_eligible
# ---------------------------------------------------------------------------

def test_metadata_match_not_auto_eligible():
    # No fingerprints → dedup falls back to 'metadata' match_type.
    _insert_track(file_path="/m/d.flac", fmt="flac", artist="Z", title="Wave",
                  bit_depth=16, sample_rate=44100)
    _insert_track(file_path="/m/d.mp3", fmt="mp3", artist="Z", title="Wave", bitrate=200)

    cands = dedup_pass.find_dedup_candidates()
    assert len(cands) == 1
    assert cands[0]["match_type"] == "metadata"
    assert cands[0]["auto_eligible"] is False


# ---------------------------------------------------------------------------
# 5. apply_dedup fail-closed + dry-run
# ---------------------------------------------------------------------------

def test_apply_blocked_when_flags_off(tmp_path):
    f = tmp_path / "keep.flac"
    f.write_bytes(b"flac")
    g = tmp_path / "drop.mp3"
    g.write_bytes(b"mp3")
    keep = _insert_track(file_path=str(f), fmt="flac", artist="A", title="T",
                         fingerprint=FP, bit_depth=16, sample_rate=44100)
    drop = _insert_track(file_path=str(g), fmt="mp3", artist="A", title="T",
                         fingerprint=FP, bitrate=200)

    with pytest.raises(PermissionError):
        dedup_pass.apply_dedup({"keep_id": keep, "trash_ids": [drop]})

    # File untouched, still active.
    assert g.exists()
    with get_db() as db:
        assert db.execute("SELECT status FROM tracks WHERE id=?", (drop,)).fetchone()[0] == "active"


def test_apply_refuses_wrong_keeper(tmp_path):
    """Server re-verifies: you cannot trash the lossless keeper by passing keep_id=mp3."""
    f = tmp_path / "keep.flac"; f.write_bytes(b"flac")
    g = tmp_path / "drop.mp3"; g.write_bytes(b"mp3")
    flac = _insert_track(file_path=str(f), fmt="flac", artist="A", title="T",
                         fingerprint=FP, bit_depth=16, sample_rate=44100)
    mp3 = _insert_track(file_path=str(g), fmt="mp3", artist="A", title="T",
                        fingerprint=FP, bitrate=200)
    # Inverted request: keep the mp3, trash the flac.
    with pytest.raises(ValueError, match="not the computed keeper"):
        dedup_pass.apply_dedup({"keep_id": mp3, "trash_ids": [flac]}, dry_run=True)


def test_apply_refuses_non_duplicate(tmp_path):
    """A trash id that isn't actually a duplicate of the keeper is refused."""
    keep = _insert_track(file_path=str(tmp_path / "k.flac"), fmt="flac", artist="A", title="T",
                         bit_depth=16, sample_rate=44100)
    other = _insert_track(file_path=str(tmp_path / "o.mp3"), fmt="mp3", artist="Different",
                          title="Unrelated", bitrate=200)
    with pytest.raises(ValueError, match="do not form a single duplicate group"):
        dedup_pass.apply_dedup({"keep_id": keep, "trash_ids": [other]}, dry_run=True)


def test_apply_dry_run_reports_without_trashing(tmp_path):
    g = tmp_path / "drop.mp3"
    g.write_bytes(b"mp3")
    keep = _insert_track(file_path=str(tmp_path / "keep.flac"), fmt="flac", artist="A",
                         title="T", bit_depth=16, sample_rate=44100)
    drop = _insert_track(file_path=str(g), fmt="mp3", artist="A", title="T", bitrate=200)

    out = dedup_pass.apply_dedup({"keep_id": keep, "trash_ids": [drop]}, dry_run=True)
    assert out["dry_run"] is True
    assert out["applied"][0]["trashed_id"] == drop
    assert g.exists()  # nothing moved
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM dedup_actions").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 6. apply_dedup with BOTH flags → real reversible trash via file_txn
# ---------------------------------------------------------------------------

def test_apply_trashes_when_both_flags_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_TXN_JOURNAL_DIR", str(tmp_path / "jrnl"))
    monkeypatch.setattr(dedup_pass, "MUSIC_ROOT", tmp_path)
    _set_setting("identity_act_enabled", "true")
    _set_setting("dedup_act_enabled", "true")

    keep_f = tmp_path / "Artist" / "keep.flac"
    keep_f.parent.mkdir(parents=True)
    keep_f.write_bytes(b"flac-data")
    drop_f = tmp_path / "Artist" / "drop.mp3"
    drop_f.write_bytes(b"mp3-data")

    keep = _insert_track(file_path=str(keep_f), fmt="flac", artist="Artist", title="Song",
                         fingerprint=FP, bit_depth=16, sample_rate=44100, bitrate=900)
    drop = _insert_track(file_path=str(drop_f), fmt="mp3", artist="Artist", title="Song",
                         fingerprint=FP, bitrate=221)

    out = dedup_pass.apply_dedup(
        {"keep_id": keep, "trash_ids": [drop], "match_type": "fingerprint", "confidence": 0.99}
    )

    assert out["errors"] == []
    assert len(out["applied"]) == 1
    assert not drop_f.exists()                 # moved out of the library
    assert keep_f.exists()                     # keeper untouched
    with get_db() as db:
        assert db.execute("SELECT status FROM tracks WHERE id=?", (drop,)).fetchone()[0] == "trashed"
        row = db.execute(
            "SELECT keep_id, trashed_id, match_type FROM dedup_actions WHERE trashed_id=?", (drop,)
        ).fetchone()
    assert row["keep_id"] == keep and row["match_type"] == "fingerprint"
    # Reversible: the file now lives under the journaled trash root.
    assert (tmp_path / ".m2-trash").exists()
