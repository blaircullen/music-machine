#!/usr/bin/env python3
"""
Usenet-primary FLAC upgrade via Lidarr.

Replaces the broken MusicGrabber/Monochrome download path (upgrade_service.py) as the
PRIMARY upgrade route. Drives Lidarr → NZBgeek (usenet) → SABnzbd → import, which is the
proven path (validated live 2026-06-17: Oasis "Be Here Now" grabbed via NZBgeek, proto=usenet).

Album-level (usenet ships albums). Per-track lossy rows roll up to one album request.
Phase-1 safety: flag-only when artist/album are not already in Lidarr (NO auto-add). Lidarr
performs its own import/placement, so no file_txn is used here — file moves happen only in the
U7 dedup pass (separate module), which removes the superseded lossy copy after review.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import database  # noqa: E402
import lidarr_client as lc  # noqa: E402
# NOTE: lossless_detect (and its scipy dep) is imported LAZILY inside check_upgrade_result —
# it is NOT installed in the music-machine container, only the sonic sidecar. The main upgrade
# flow (request_album_upgrade) must not depend on it.

logger = logging.getLogger(__name__)

# Match lidarr_recue.py: env first, fall back to the known Beast Lidarr.
BASE = os.environ.get("LIDARR_URL", "http://10.0.0.13:8787")
API_KEY = os.environ.get("LIDARR_API_KEY", "2cecee10715a4c1dbe8daa16226f7ed7")


def configure(base: str | None = None, api_key: str | None = None) -> None:
    global BASE, API_KEY
    if base:
        BASE = base
    if api_key:
        API_KEY = api_key


def request_album_upgrade(artist_name: str, album_title: str, *, dry_run: bool = False) -> dict:
    """
    Drive Lidarr (usenet) to grab a lossless copy of one album.

    Returns {status, lidarr_album_id, reason}. Statuses:
      flagged_no_artist / flagged_no_album  — not in Lidarr; Phase-1 does NOT auto-add.
      inflight                              — Lidarr already searching/downloading this album.
      searching                             — usenet album search triggered.
    Records the outcome in the album_upgrades table.
    """
    old_dry = lc.DRY_RUN
    lc.DRY_RUN = dry_run
    try:
        artist = lc.find_artist(artist_name or "", BASE, API_KEY)
        if not artist:
            return _record(artist_name, album_title, None, "flagged_no_artist",
                           f"artist '{artist_name}' not in Lidarr (flag-only; no auto-add in Phase 1)")

        album = lc.find_album(int(artist["id"]), album_title or "", BASE, API_KEY)
        if not album:
            return _record(artist_name, album_title, None, "flagged_no_album",
                           f"album '{album_title}' not found under '{artist_name}' in Lidarr")

        album_id = int(album["id"])
        if lc.album_inflight(album_id, BASE, API_KEY):
            return _record(artist_name, album_title, album_id, "inflight",
                           "Lidarr already searching/downloading this album")

        lc.ensure_monitored_lossless(artist, album, BASE, API_KEY)  # forces qualityProfileId=2
        if not dry_run:
            lc.trigger_album_search(album_id, BASE, API_KEY)
        return _record(artist_name, album_title, album_id,
                       "dry_run" if dry_run else "searching",
                       "usenet album search triggered" if not dry_run else "dry-run: would trigger search")
    except Exception as exc:  # network / Lidarr error — flag, never crash the runner
        logger.warning("usenet upgrade failed for %s - %s: %s", artist_name, album_title, exc)
        return _record(artist_name, album_title, None, "error", str(exc)[:300])
    finally:
        lc.DRY_RUN = old_dry


def check_upgrade_result(album_id: int, *, want_title: str | None = None,
                         want_basename: str | None = None) -> str:
    """
    Return 'placed' if a real-lossless file for the wanted track is present in the album's
    Lidarr trackfiles, else 'pending'. Prefers lossless_detect.analyze_flac to confirm the import
    is genuinely lossless (not a fake-FLAC); if that module/scipy is unavailable in this container,
    falls back to trusting Lidarr's qualityProfileId=2 grab (a present .flac/.alac counts as placed).
    """
    try:
        analyze_flac = None
        try:
            from lossless_detect import analyze_flac as _af  # lazy: scipy dep, sidecar-only
            analyze_flac = _af
        except Exception:
            analyze_flac = None

        for path in lc.album_track_paths(int(album_id), BASE, API_KEY):
            if not os.path.exists(path):
                continue
            p = Path(path)
            # Per-track targeting. An exact basename wins; otherwise a title-in-stem match. When
            # ONLY want_title is supplied (the thaw poller's case — Lidarr's final basename isn't
            # known ahead of time) the title filter must STILL apply, so a single landed track can
            # never satisfy the whole album. With neither arg, any lossless file counts (album-level).
            if want_basename:
                if p.name != want_basename and not (
                    want_title and want_title.lower() in p.stem.lower()
                ):
                    continue
            elif want_title:
                if want_title.lower() not in p.stem.lower():
                    continue
            if analyze_flac is not None:
                if str(analyze_flac(path).get("verdict") or "") == "lossless":
                    return "placed"
            elif Path(path).suffix.lower() in (".flac", ".alac"):
                return "placed"  # fallback: Lidarr profile-2 ensures lossless
    except Exception as exc:
        logger.debug("check_upgrade_result error for album %s: %s", album_id, exc)
    return "pending"


def _record(artist: str, album: str, lidarr_album_id: int | None, status: str, reason: str) -> dict:
    """Upsert one row into album_upgrades keyed by (artist, album)."""
    try:
        with database.get_db() as db:
            db.execute(
                """
                INSERT INTO album_upgrades
                    (artist, album, lidarr_album_id, status, attempts, reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(artist, album) DO UPDATE SET
                    lidarr_album_id = excluded.lidarr_album_id,
                    status          = excluded.status,
                    attempts        = album_upgrades.attempts + 1,
                    reason          = excluded.reason,
                    updated_at      = CURRENT_TIMESTAMP
                """,
                (artist or "", album or "", lidarr_album_id, status, reason),
            )
    except Exception as exc:
        logger.warning("could not record album_upgrade (%s - %s): %s", artist, album, exc)
    return {"status": status, "lidarr_album_id": lidarr_album_id, "reason": reason}
