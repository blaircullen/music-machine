"""
upgrade_thaw.py — lazy thaw of the frozen upgrade_queue onto the usenet-primary path.

Background: on 2026-06-11 the identity-resolver safety freeze parked ~14,700 upgrade_queue rows
at status='frozen'. This module thaws them DELIBERATELY and in SMALL batches onto the proven
usenet route (upgrade_usenet.request_album_upgrade → Lidarr → NZBgeek → SABnzbd), NOT the broken
MusicGrabber/monochrome path.

Safety posture:
  - database.upgrade_paused() defaults TRUE. A real run REFUSES while paused (dry-run previews
    are allowed). Un-pause is a deliberate settings flip.
  - thaw_next(n) flips ONLY the next N frozen rows → pending. Nothing thaws itself.
  - The Lidarr queue is already deep (~895 in flight + ~30k missing-monitored) — NEVER mass-
    trigger. The runner caps albums per invocation (max_albums) and rolls per-track rows up to
    ONE album request via the album_upgrades table.
  - Idempotent: a track that already has an active lossless sibling is skipped (it needed dedup,
    not a download).
  - NO file operations here — Lidarr imports/places its own files. The superseded lossy original
    is removed later by the U7 dedup pass (review-only).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import database
from dedup import normalize_text

logger = logging.getLogger(__name__)

LOSSLESS_FORMATS = {"flac", "alac", "wav"}
_DURATION_GATE_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Lazy production wiring (injectable for tests so we never pull numpy/scipy)
# ---------------------------------------------------------------------------

def _default_request_fn():
    import upgrade_usenet
    return upgrade_usenet.request_album_upgrade


def _default_check_fn():
    import upgrade_usenet
    return upgrade_usenet.check_upgrade_result


# ---------------------------------------------------------------------------
# Thaw control
# ---------------------------------------------------------------------------

def thaw_next(n: int) -> int:
    """Flip the next N 'frozen' upgrade_queue rows (lowest id first) to 'pending'.

    Returns the number actually flipped (< n if fewer remain frozen). This is the ONLY way rows
    leave the frozen state — there is no automatic thaw.
    """
    if n <= 0:
        return 0
    with database.get_db() as db:
        ids = [
            r["id"]
            for r in db.execute(
                "SELECT id FROM upgrade_queue WHERE status = 'frozen' ORDER BY id LIMIT ?",
                (n,),
            ).fetchall()
        ]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        db.execute(
            f"""UPDATE upgrade_queue
                SET status = 'pending', updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})""",
            ids,
        )
    logger.info("thaw_next: %d frozen rows → pending", len(ids))
    return len(ids)


# ---------------------------------------------------------------------------
# Lossless-sibling idempotency check
# ---------------------------------------------------------------------------

def _build_lossless_index(db) -> dict:
    """(norm_artist, norm_title) -> [durations] for every ACTIVE lossless track."""
    index: dict[tuple, list] = {}
    rows = db.execute(
        "SELECT artist, title, duration FROM tracks "
        "WHERE status = 'active' AND LOWER(format) IN ('flac','alac','wav')"
    ).fetchall()
    for r in rows:
        key = (normalize_text(r["artist"] or ""), normalize_text(r["title"] or ""))
        if not key[0] and not key[1]:
            continue
        index.setdefault(key, []).append(r["duration"])
    return index


def _has_active_lossless_sibling(track: dict, lossless_index: dict) -> bool:
    key = (normalize_text(track.get("artist") or ""), normalize_text(track.get("title") or ""))
    if not key[0] and not key[1]:
        return False
    siblings = lossless_index.get(key)
    if not siblings:
        return False
    td = track.get("duration") or 0
    for sd in siblings:
        sd = sd or 0
        if not td or not sd or abs(td - sd) <= _DURATION_GATE_SECONDS:
            return True
    return False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_usenet_upgrade_batch(
    *,
    dry_run: bool = False,
    max_albums: int = 5,
    pull_limit: int = 200,
    request_fn: Optional[Callable[..., dict]] = None,
) -> dict:
    """
    Drain a SMALL batch of 'pending' upgrade_queue rows onto the usenet path.

    Gating: a real run refuses while database.upgrade_paused() is true (dry-run previews allowed).
    Rows whose track already has an active lossless sibling are marked 'skipped' (idempotent).
    Remaining rows roll up to (artist, album); each distinct album (capped at max_albums) triggers
    ONE upgrade_usenet.request_album_upgrade. Rows with no album cannot use the album-level path
    and are marked 'skipped'. Returns a per-outcome report.
    """
    if not dry_run and database.upgrade_paused():
        return {"ok": False, "reason": "upgrade_paused",
                "detail": "set upgrade_paused=false to run (default-paused safety gate)"}

    req = request_fn or _default_request_fn()

    with database.get_db() as db:
        pending = db.execute(
            """SELECT uq.id AS queue_id, uq.track_id,
                      t.artist, t.album, t.title, t.duration, t.format
               FROM upgrade_queue uq
               JOIN tracks t ON uq.track_id = t.id
               WHERE uq.status = 'pending'
               ORDER BY t.artist, t.album, uq.id
               LIMIT ?""",
            (pull_limit,),
        ).fetchall()
        lossless_index = _build_lossless_index(db)

    pending = [dict(r) for r in pending]

    def _set_status(queue_id: int, status: str, error_msg: str | None = None):
        with database.get_db() as db:
            db.execute(
                """UPDATE upgrade_queue
                   SET status = ?, error_msg = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (status, error_msg, queue_id),
            )

    skipped_sibling, skipped_no_album = [], []
    albums: dict[tuple, list[dict]] = {}

    for row in pending:
        if _has_active_lossless_sibling(row, lossless_index):
            skipped_sibling.append(row["queue_id"])
            if not dry_run:
                _set_status(row["queue_id"], "skipped", "active lossless sibling exists")
            continue
        artist = (row.get("artist") or "").strip()
        album = (row.get("album") or "").strip()
        if not album:
            skipped_no_album.append(row["queue_id"])
            if not dry_run:
                _set_status(row["queue_id"], "skipped", "no album — usenet path is album-level")
            continue
        albums.setdefault((artist, album), []).append(row)

    album_keys = list(albums.keys())[:max_albums]
    requested = []
    for (artist, album) in album_keys:
        rows = albums[(artist, album)]
        try:
            result = req(artist, album, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — never crash the batch on one album
            logger.warning("request_album_upgrade crashed for %s - %s: %s", artist, album, exc)
            result = {"status": "error", "lidarr_album_id": None, "reason": str(exc)[:300]}

        status = result.get("status")
        reason = result.get("reason")
        # Map the album request outcome onto the per-track queue rows. Use a DEDICATED
        # 'usenet_inflight' status (NOT 'searching') so the MusicGrabber-oriented startup reset in
        # main.py — which flips 'searching'/'downloading' back to 'pending' — never re-runs an
        # already-submitted album request. poll_thawed_upgrades advances these rows.
        if status in ("searching", "inflight", "dry_run"):
            queue_status = "usenet_inflight"
            err = None
        elif status in ("flagged_no_artist", "flagged_no_album"):
            queue_status = "skipped"  # not in Lidarr; Phase-1 is flag-only (no auto-add)
            err = reason
        else:  # 'error' or anything unexpected
            queue_status = "failed"
            err = reason

        if not dry_run:
            for r in rows:
                _set_status(r["queue_id"], queue_status, err)

        requested.append({
            "artist": artist,
            "album": album,
            "result_status": status,
            "queue_status": queue_status,
            "lidarr_album_id": result.get("lidarr_album_id"),
            "track_count": len(rows),
            "reason": reason,
        })

    return {
        "ok": True,
        "dry_run": dry_run,
        "pending_pulled": len(pending),
        "skipped_lossless_sibling": len(skipped_sibling),
        "skipped_no_album": len(skipped_no_album),
        "albums_considered": len(albums),
        "albums_requested": len(requested),
        "albums_deferred": max(0, len(albums) - len(album_keys)),
        "requested": requested,
    }


def _check_track_placed(check: Callable[..., str], album_id: int, title: str | None) -> str:
    """Call check_upgrade_result with per-track targeting; tolerate fakes without the kwarg."""
    try:
        return check(album_id, want_title=title)
    except TypeError:
        return check(album_id)


def poll_thawed_upgrades(*, check_fn: Optional[Callable[..., str]] = None) -> dict:
    """
    Advance in-flight album_upgrades. For each in-flight album, check EACH linked 'usenet_inflight'
    track INDIVIDUALLY (upgrade_usenet.check_upgrade_result with want_title) so only the track whose
    own lossless file actually landed flips to 'found' — ready for the U7 dedup REVIEW that removes
    the superseded lossy original. The album row is marked 'placed' only once none of its tracks
    remain in flight. Never auto-trashes.
    """
    check = check_fn or _default_check_fn()

    with database.get_db() as db:
        inflight = [
            dict(r)
            for r in db.execute(
                "SELECT id, artist, album, lidarr_album_id FROM album_upgrades "
                "WHERE status IN ('searching','inflight') AND lidarr_album_id IS NOT NULL"
            ).fetchall()
        ]

    placed_tracks = 0
    placed_albums = []
    for au in inflight:
        album_id = int(au["lidarr_album_id"])
        with database.get_db() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    """SELECT uq.id AS queue_id, t.title
                       FROM upgrade_queue uq JOIN tracks t ON uq.track_id = t.id
                       WHERE uq.status = 'usenet_inflight' AND t.artist = ? AND t.album = ?""",
                    (au["artist"], au["album"]),
                ).fetchall()
            ]

        for r in rows:
            try:
                verdict = _check_track_placed(check, album_id, r["title"])
            except Exception as exc:  # noqa: BLE001
                logger.debug("poll check failed for album %s / %s: %s",
                             album_id, r["title"], exc)
                continue
            if verdict == "placed":
                with database.get_db() as db:
                    db.execute(
                        "UPDATE upgrade_queue SET status = 'found', updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?",
                        (r["queue_id"],),
                    )
                placed_tracks += 1

        # Album is 'placed' only when no linked track remains in flight.
        with database.get_db() as db:
            remaining = db.execute(
                """SELECT COUNT(*) FROM upgrade_queue uq JOIN tracks t ON uq.track_id = t.id
                   WHERE uq.status = 'usenet_inflight' AND t.artist = ? AND t.album = ?""",
                (au["artist"], au["album"]),
            ).fetchone()[0]
            if remaining == 0:
                db.execute(
                    "UPDATE album_upgrades SET status = 'placed', updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (au["id"],),
                )
                placed_albums.append(au["id"])

    return {
        "ok": True,
        "checked": len(inflight),
        "placed_tracks": placed_tracks,
        "placed_albums": len(placed_albums),
    }


def thaw_status() -> dict:
    """Counts for the thaw dashboard: upgrade_queue by status + album_upgrades by status."""
    with database.get_db() as db:
        q = {
            r["status"]: r["n"]
            for r in db.execute(
                "SELECT status, COUNT(*) AS n FROM upgrade_queue GROUP BY status"
            ).fetchall()
        }
        a = {
            r["status"]: r["n"]
            for r in db.execute(
                "SELECT status, COUNT(*) AS n FROM album_upgrades GROUP BY status"
            ).fetchall()
        }
    return {
        "upgrade_paused": database.upgrade_paused(),
        "upgrade_queue": q,
        "album_upgrades": a,
        "frozen": q.get("frozen", 0),
        "pending": q.get("pending", 0),
    }
