"""
Feedback Service — Plex history polling and passive EMA updates.

Runs nightly at 2 AM. Polls Plex play history for tracks that appeared in
any station's last generated playlist, then applies EMA updates to the
station's preference vector based on whether tracks were played or skipped.

Explicit thumbs up/down feedback from the web player is handled immediately
via sonic_service.apply_feedback() — this module handles only the passive
Plex history signal.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# How far back to look in Plex history (one full day to cover overnight gaps)
_HISTORY_LOOKBACK_HOURS = 26
_PLAY_THRESHOLD = 0.80  # fraction of track duration → counts as "played"


# ---------------------------------------------------------------------------
# Plex history helpers
# ---------------------------------------------------------------------------

def _plex_get(path: str, params: Optional[dict] = None) -> Optional[requests.Response]:
    import os
    plex_url = os.environ.get("PLEX_URL", "http://10.0.0.13:32400")
    plex_token = os.environ.get("PLEX_TOKEN", "mxrEzLiMjZ1FftGMZaiq")
    try:
        resp = requests.get(
            f"{plex_url}{path}",
            params={**(params or {}), "X-Plex-Token": plex_token},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp
    except Exception as e:
        logger.warning(f"Plex API error {path}: {e}")
        return None


def _get_recent_plex_plays() -> dict[str, float]:
    """
    Query Plex history for tracks played in the last _HISTORY_LOOKBACK_HOURS.
    Returns {ratingKey: play_fraction} where play_fraction is viewOffset/duration.
    Only includes tracks with play_fraction > 0 (actually started).
    """
    cutoff = int((datetime.now() - timedelta(hours=_HISTORY_LOOKBACK_HOURS)).timestamp())

    resp = _plex_get(
        "/status/sessions/history/all",
        params={
            "librarySectionID": "5",  # music section
            "sort": "viewedAt:desc",
        },
    )
    if resp is None:
        return {}

    try:
        data = resp.json()
    except Exception:
        return {}

    plays: dict[str, float] = {}
    for item in data.get("MediaContainer", {}).get("Metadata", []):
        viewed_at = item.get("viewedAt", 0)
        if viewed_at < cutoff:
            continue
        rating_key = str(item.get("ratingKey", ""))
        if not rating_key:
            continue
        view_offset = item.get("viewOffset", 0)
        duration = item.get("duration", 0)
        if duration > 0:
            plays[rating_key] = view_offset / duration
        else:
            plays[rating_key] = 1.0  # assume complete if no duration

    return plays


def _rating_key_to_track_id(rating_key: str) -> Optional[int]:
    """
    Resolve a Plex ratingKey to a local tracks.id by fetching the file path
    from Plex and matching against the tracks table.
    """
    resp = _plex_get(f"/library/metadata/{rating_key}")
    if resp is None:
        return None

    try:
        data = resp.json()
        items = data.get("MediaContainer", {}).get("Metadata", [])
        if not items:
            return None
        # Get file path from first media part
        media = items[0].get("Media", [])
        if not media:
            return None
        parts = media[0].get("Part", [])
        if not parts:
            return None
        plex_file = parts[0].get("file", "")
    except Exception:
        return None

    if not plex_file:
        return None

    from database import get_db
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM tracks WHERE file_path = ?", (plex_file,)
        ).fetchone()
        if row:
            return row["id"]

        # Try suffix match (container path may differ from host path)
        # Strip leading /music/ prefix variants
        for prefix in ("/music/", "/mnt/nas/music/", "/mnt/music/"):
            if plex_file.startswith(prefix):
                suffix = plex_file[len(prefix):]
                row = db.execute(
                    "SELECT id FROM tracks WHERE file_path LIKE ?",
                    (f"%{suffix}",),
                ).fetchone()
                if row:
                    return row["id"]

    return None


# ---------------------------------------------------------------------------
# Nightly polling job
# ---------------------------------------------------------------------------

def run_nightly_plex_feedback():
    """
    Poll Plex history and apply passive EMA updates to station preference vectors.
    Called by the scheduler at 2 AM (shares the slot with playlist sync; runs after).
    """
    logger.info("Nightly Plex feedback poll starting")

    from database import get_db
    from sonic_service import apply_feedback

    # 1. Get all active stations
    with get_db() as db:
        stations = [dict(r) for r in db.execute("SELECT id FROM stations").fetchall()]

    if not stations:
        logger.info("No stations — skipping feedback poll")
        return

    # 2. Collect track_ids that appeared in any station's last playlist
    #    (tracks generated in the last 7 days, to match recency window)
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    station_playlists: dict[int, set[int]] = {}  # station_id → set of track_ids

    with get_db() as db:
        for st in stations:
            sid = st["id"]
            rows = db.execute(
                "SELECT DISTINCT track_id FROM station_track_history "
                "WHERE station_id = ? AND generated_at >= ?",
                (sid, cutoff),
            ).fetchall()
            station_playlists[sid] = {r["track_id"] for r in rows}

    all_playlist_track_ids = set().union(*station_playlists.values())
    if not all_playlist_track_ids:
        logger.info("No recently generated playlists — skipping feedback poll")
        return

    # 3. Get Plex recent plays (ratingKey → play_fraction)
    recent_plays = _get_recent_plex_plays()
    if not recent_plays:
        logger.info("No recent Plex plays found")
        return

    logger.info(f"Found {len(recent_plays)} recent Plex plays to cross-reference")

    # 4. Resolve rating keys to local track_ids (with in-memory cache)
    rk_to_tid: dict[str, Optional[int]] = {}
    played_track_ids: dict[int, float] = {}  # track_id → play_fraction

    for rk, fraction in recent_plays.items():
        if rk not in rk_to_tid:
            rk_to_tid[rk] = _rating_key_to_track_id(rk)
        tid = rk_to_tid[rk]
        if tid is not None:
            played_track_ids[tid] = fraction

    # 5. Apply feedback per station
    for sid, playlist_tids in station_playlists.items():
        if not playlist_tids:
            continue

        played_in_station = playlist_tids & set(played_track_ids.keys())
        unplayed_in_station = playlist_tids - set(played_track_ids.keys())

        applied = 0
        for tid in played_in_station:
            fraction = played_track_ids[tid]
            signal = "played" if fraction >= _PLAY_THRESHOLD else "skipped"
            try:
                apply_feedback(sid, tid, signal, source="plex_history")
                applied += 1
            except Exception as e:
                logger.warning(f"Station {sid} track {tid} feedback failed: {e}")

        # Weak negative for in-playlist tracks that were never started
        for tid in list(unplayed_in_station)[:20]:  # cap to avoid spam
            try:
                apply_feedback(sid, tid, "skipped", source="plex_history")
            except Exception as e:
                logger.warning(f"Station {sid} track {tid} skip-signal failed: {e}")

        logger.info(
            f"Station {sid}: {applied} played signals, "
            f"{min(len(unplayed_in_station), 20)} skipped signals applied"
        )

    logger.info("Nightly Plex feedback poll complete")
