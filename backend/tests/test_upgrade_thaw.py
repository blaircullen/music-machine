"""
P3 lazy-thaw tests — paused gate, bounded thaw, sibling-skip, album rollup, poll.

No real Lidarr: run_usenet_upgrade_batch / poll take injected request_fn / check_fn, so these
tests never import upgrade_usenet (and never pull numpy/scipy).
"""

import pytest

import upgrade_thaw
from database import get_db, upgrade_paused


def _set_setting(key, value):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


@pytest.fixture(autouse=True)
def _clean(init_test_db):
    with get_db() as db:
        for t in ("album_upgrades", "upgrade_queue", "tracks"):
            db.execute(f"DELETE FROM {t}")
    _set_setting("upgrade_paused", "true")
    yield


def _track(*, artist, album, title, fmt="mp3", duration=200.0, status="active"):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO tracks (file_path, format, artist, album, title, duration, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (f"/m/{artist}-{title}.{fmt}", fmt, artist, album, title, duration, status),
        )
        return cur.lastrowid


def _queue(track_id, status="frozen"):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO upgrade_queue (track_id, status) VALUES (?, ?)", (track_id, status)
        )
        return cur.lastrowid


def _q_status(queue_id):
    with get_db() as db:
        return db.execute("SELECT status FROM upgrade_queue WHERE id=?", (queue_id,)).fetchone()[0]


# ---------------------------------------------------------------------------
# 1. default-paused gate
# ---------------------------------------------------------------------------

def test_default_paused_and_run_refuses():
    assert upgrade_paused() is True
    out = upgrade_thaw.run_usenet_upgrade_batch(request_fn=lambda *a, **k: {})
    assert out["ok"] is False and out["reason"] == "upgrade_paused"


# ---------------------------------------------------------------------------
# 2. thaw_next bounded
# ---------------------------------------------------------------------------

def test_thaw_next_flips_exactly_n():
    ids = [_queue(_track(artist="A", album="Alb", title=f"T{i}")) for i in range(5)]
    assert upgrade_thaw.thaw_next(3) == 3
    statuses = [_q_status(i) for i in ids]
    assert statuses.count("pending") == 3
    assert statuses.count("frozen") == 2
    # Idempotent tail: asking for more than remain flips only what's left.
    assert upgrade_thaw.thaw_next(10) == 2


# ---------------------------------------------------------------------------
# 3. lossless-sibling detection
# ---------------------------------------------------------------------------

def test_active_lossless_sibling_detected():
    _track(artist="Q", album="Alb", title="Hit", fmt="flac")
    with get_db() as db:
        index = upgrade_thaw._build_lossless_index(db)
    assert upgrade_thaw._has_active_lossless_sibling(
        {"artist": "Q", "title": "Hit", "duration": 201.0}, index) is True
    assert upgrade_thaw._has_active_lossless_sibling(
        {"artist": "Q", "title": "Other", "duration": 201.0}, index) is False


# ---------------------------------------------------------------------------
# 4. runner rollup + status mapping (injected request_fn)
# ---------------------------------------------------------------------------

def test_run_rolls_up_albums_and_sets_status():
    _set_setting("upgrade_paused", "false")

    # Two lossy tracks on the same album → ONE album request.
    t1 = _track(artist="Band", album="Record", title="One")
    t2 = _track(artist="Band", album="Record", title="Two")
    q1, q2 = _queue(t1, "pending"), _queue(t2, "pending")

    # A track that already has an active lossless sibling → skipped, no request.
    _track(artist="Band", album="Record", title="Three", fmt="flac")  # the sibling
    t3 = _track(artist="Band", album="Record", title="Three", fmt="mp3")
    q3 = _queue(t3, "pending")

    # A no-album track → skipped (album-level path).
    t4 = _track(artist="Solo", album="", title="Loose")
    q4 = _queue(t4, "pending")

    calls = []

    def fake_request(artist, album, *, dry_run=False):
        calls.append((artist, album, dry_run))
        return {"status": "searching", "lidarr_album_id": 42, "reason": "ok"}

    out = upgrade_thaw.run_usenet_upgrade_batch(request_fn=fake_request)

    assert out["ok"] is True
    assert calls == [("Band", "Record", False)]          # exactly one album request
    assert out["skipped_lossless_sibling"] == 1
    assert out["skipped_no_album"] == 1
    assert out["albums_requested"] == 1
    # Dedicated in-flight status (NOT 'searching') so the startup reset can't re-run it.
    assert _q_status(q1) == "usenet_inflight" and _q_status(q2) == "usenet_inflight"
    assert _q_status(q3) == "skipped"                    # sibling
    assert _q_status(q4) == "skipped"                    # no album


def test_flagged_no_artist_maps_to_skipped():
    _set_setting("upgrade_paused", "false")
    t = _track(artist="Ghost", album="None", title="X")
    q = _queue(t, "pending")
    out = upgrade_thaw.run_usenet_upgrade_batch(
        request_fn=lambda *a, **k: {"status": "flagged_no_artist", "lidarr_album_id": None,
                                    "reason": "not in lidarr"}
    )
    assert out["ok"] is True
    assert _q_status(q) == "skipped"


# ---------------------------------------------------------------------------
# 5. dry-run preview allowed while paused
# ---------------------------------------------------------------------------

def test_dry_run_allowed_while_paused_and_no_writes():
    assert upgrade_paused() is True
    t = _track(artist="Band", album="Record", title="One")
    q = _queue(t, "pending")
    out = upgrade_thaw.run_usenet_upgrade_batch(
        dry_run=True, request_fn=lambda *a, **k: {"status": "dry_run", "lidarr_album_id": None,
                                                  "reason": "preview"})
    assert out["ok"] is True and out["dry_run"] is True
    assert out["albums_requested"] == 1
    assert _q_status(q) == "pending"  # dry-run wrote nothing


# ---------------------------------------------------------------------------
# 6. poll flips placed albums + linked rows
# ---------------------------------------------------------------------------

def test_poll_marks_placed_and_flips_rows():
    t = _track(artist="Band", album="Record", title="One")
    q = _queue(t, "usenet_inflight")
    with get_db() as db:
        db.execute(
            """INSERT INTO album_upgrades (artist, album, lidarr_album_id, status)
               VALUES ('Band', 'Record', 42, 'searching')"""
        )

    # check_fn must accept the per-track want_title kwarg.
    out = upgrade_thaw.poll_thawed_upgrades(check_fn=lambda album_id, **kw: "placed")
    assert out["placed_tracks"] == 1
    assert out["placed_albums"] == 1
    assert _q_status(q) == "found"
    with get_db() as db:
        st = db.execute("SELECT status FROM album_upgrades WHERE lidarr_album_id=42").fetchone()[0]
    assert st == "placed"


def test_check_upgrade_result_want_title_filters_standalone(tmp_path, monkeypatch):
    """Regression for the poll integration bug: want_title must filter even with no want_basename,
    so one landed track can't satisfy a sibling's poll. Forces the extension fallback (no scipy)."""
    import sys
    import upgrade_usenet

    one = tmp_path / "01 - One.flac"; one.write_bytes(b"x")
    two = tmp_path / "02 - Two.flac"; two.write_bytes(b"x")
    monkeypatch.setattr(upgrade_usenet.lc, "album_track_paths",
                        lambda *a, **k: [str(one), str(two)])
    # Make `from lossless_detect import analyze_flac` fail → extension fallback (.flac = placed).
    monkeypatch.setitem(sys.modules, "lossless_detect", None)

    assert upgrade_usenet.check_upgrade_result(42, want_title="Two") == "placed"
    assert upgrade_usenet.check_upgrade_result(42, want_title="Nonexistent Track") == "pending"


def test_poll_partial_album_not_marked_placed():
    """If one track lands but another is still in flight, the album stays 'searching'."""
    t1 = _track(artist="Band", album="Record", title="One")
    t2 = _track(artist="Band", album="Record", title="Two")
    q1 = _queue(t1, "usenet_inflight")
    q2 = _queue(t2, "usenet_inflight")
    with get_db() as db:
        db.execute(
            """INSERT INTO album_upgrades (artist, album, lidarr_album_id, status)
               VALUES ('Band', 'Record', 42, 'searching')"""
        )

    # Only "One" has landed.
    out = upgrade_thaw.poll_thawed_upgrades(
        check_fn=lambda album_id, want_title=None: "placed" if want_title == "One" else "pending"
    )
    assert out["placed_tracks"] == 1
    assert out["placed_albums"] == 0
    assert _q_status(q1) == "found" and _q_status(q2) == "usenet_inflight"
    with get_db() as db:
        st = db.execute("SELECT status FROM album_upgrades WHERE lidarr_album_id=42").fetchone()[0]
    assert st == "searching"
