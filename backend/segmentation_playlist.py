"""
Live & Holiday segmentation — playlist protection (docs/live-holiday-split-spec.md §9).

When a track's file changes library section Plex assigns it a NEW ratingKey, and any playlist
referencing the OLD ratingKey silently drops it. This module snapshots affected playlists
before a run's moves, auto-repairs membership after the target-section rescan, and writes a
manual-repair report for anything that couldn't be auto-repaired — so nothing breaks silently.

All Plex helpers are reused from plex_playlist_sync (verified). Every Plex call is wrapped;
failures are recorded (repair_error / report), never raised — a playlist API hiccup must not
abort a move run.

[VERIFY AT BUILD] §4/§9:
  - The Live/Holiday Plex SECTION IDS (SEG_LIVE_SECTION_ID / SEG_HOLIDAY_SECTION_ID) are
    unknown from this repo — set them at deploy once the sections exist on Beast.
  - Plex's Media/Part/@file uses the PLEX-HOST mount root (e.g. /mnt/nas/...) while this
    container writes dest paths under /live_performances etc. Path matching therefore compares
    a normalized TAIL (Artist/Album/filename) rather than absolute paths — confirm this tail
    is stable against the real library layout on Beast before trusting auto-repair.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from database import get_db
import plex_playlist_sync as pps

logger = logging.getLogger(__name__)

LIVE_SECTION_ID = os.environ.get("SEG_LIVE_SECTION_ID", "")      # [VERIFY AT BUILD]
HOLIDAY_SECTION_ID = os.environ.get("SEG_HOLIDAY_SECTION_ID", "")  # [VERIFY AT BUILD]

REPAIR_REPORT = Path(os.environ.get("SEG_REPAIR_REPORT", "/data/segmentation_playlist_repair.json"))


def _section_for(target_library: str) -> str:
    return LIVE_SECTION_ID if target_library == "live" else HOLIDAY_SECTION_ID


def _path_tail(path: Optional[str], n: int = 3) -> str:
    """Normalized last-n path components (Artist/Album/filename), for cross-mount matching."""
    if not path:
        return ""
    parts = Path(path).parts[-n:]
    return "/".join(p.lower() for p in parts)


# ---------------------------------------------------------------------------
# Snapshot (before the first move of a run)
# ---------------------------------------------------------------------------

def _part_file(rating_key: str) -> Optional[str]:
    """Resolve a Plex ratingKey to its on-disk Media/Part/@file, or None."""
    try:
        resp = pps._plex_get(f"/library/metadata/{rating_key}")
        meta = resp.json().get("MediaContainer", {}).get("Metadata", [])
        if not meta:
            return None
        for media in meta[0].get("Media", []):
            for part in media.get("Part", []):
                if part.get("file"):
                    return part["file"]
    except Exception as e:
        logger.warning(f"failed to resolve Part/@file for ratingKey {rating_key}: {e}")
    return None


def snapshot_playlists(run_id: str, move_tracks: list[dict]) -> dict:
    """Snapshot playlists whose members intersect this run's move-set (§9 steps 1-3).

    `move_tracks` is a list of {track_id, file_path} the run will move. Writes one
    segmentation_playlist_snapshot row per (playlist, track) pair, and stashes each
    old_rating_key into the matching segmentation_moves row.
    """
    stats = {"playlists": 0, "pairs": 0}
    # Index the move-set by normalized path tail.
    by_tail = {}
    for mt in move_tracks:
        by_tail.setdefault(_path_tail(mt.get("file_path")), mt)

    try:
        resp = pps._plex_get("/playlists")
        playlists = resp.json().get("MediaContainer", {}).get("Metadata", [])
    except Exception as e:
        logger.warning(f"snapshot: failed to list playlists: {e}")
        return stats

    for pl in playlists:
        if pl.get("smart") in (1, "1", True):
            continue  # smart/dynamic playlists are unaffected by ratingKey changes
        pl_key = pl.get("ratingKey")
        pl_title = pl.get("title")
        try:
            member_keys = pps._get_playlist_track_keys(pl_key)
        except Exception as e:
            logger.warning(f"snapshot: failed to read items for playlist {pl_title}: {e}")
            continue

        matched_any = False
        for rk in member_keys:
            f = _part_file(rk)
            mt = by_tail.get(_path_tail(f))
            if mt is None:
                continue
            matched_any = True
            stats["pairs"] += 1
            with get_db() as db:
                db.execute(
                    """INSERT INTO segmentation_playlist_snapshot
                           (run_id, playlist_rating_key, playlist_title, track_id,
                            old_rating_key, file_path)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (run_id, str(pl_key), pl_title, mt["track_id"], str(rk), mt.get("file_path")),
                )
                # Stash old_rating_key into the run's (pending/done) move row for this track.
                db.execute(
                    "UPDATE segmentation_moves SET old_rating_key=? "
                    "WHERE run_id=? AND track_id=? AND (old_rating_key IS NULL OR old_rating_key='')",
                    (str(rk), run_id, mt["track_id"]),
                )
        if matched_any:
            stats["playlists"] += 1

    logger.info(f"segmentation playlist snapshot run={run_id}: {stats}")
    return stats


# ---------------------------------------------------------------------------
# Repair (after the run's moves + target-section rescan)
# ---------------------------------------------------------------------------

def _resolve_new_rating_key(dest_path: str, target_library: str) -> Optional[str]:
    """Find the new ratingKey for a moved file by tail-matching Part/@file in the target section.

    Parameterized by section — the spec notes search_plex_track() hardcodes MUSIC_SECTION_ID=5
    (plex_playlist_sync.py:254, the /all query), so this uses a direct section-scoped scan
    instead of that helper.
    """
    section = _section_for(target_library)
    if not section:
        return None
    want = _path_tail(dest_path)
    try:
        resp = pps._plex_get(f"/library/sections/{section}/all", {"type": "10"})
        tracks = resp.json().get("MediaContainer", {}).get("Metadata", [])
    except Exception as e:
        logger.warning(f"repair: section {section} scan failed: {e}")
        return None
    for t in tracks:
        for media in t.get("Media", []):
            for part in media.get("Part", []):
                if _path_tail(part.get("file")) == want:
                    return t.get("ratingKey")
    return None


def repair_playlists(run_id: str) -> dict:
    """Re-add moved tracks to their playlists under the new ratingKey (§9 steps 4-6).

    Writes repair_error for any pair that can't be auto-repaired and emits a manual-repair
    report at REPAIR_REPORT. Returns {repaired, failed}.
    """
    stats = {"repaired": 0, "failed": 0}
    with get_db() as db:
        snaps = db.execute(
            "SELECT s.id, s.playlist_rating_key, s.playlist_title, s.track_id, s.file_path, "
            "       m.dest_path, m.target_library, m.new_rating_key "
            "FROM segmentation_playlist_snapshot s "
            "LEFT JOIN segmentation_moves m "
            "  ON m.run_id=s.run_id AND m.track_id=s.track_id AND m.state='done' AND m.rolled_back=0 "
            "WHERE s.run_id=? AND s.repaired=0",
            (run_id,),
        ).fetchall()

    machine_id = None
    try:
        machine_id = pps.get_machine_id()
    except Exception as e:
        logger.warning(f"repair: get_machine_id failed: {e}")

    failures = []
    for s in snaps:
        snap_id = s["id"]
        dest = s["dest_path"]
        target = s["target_library"]
        if not dest or not target:
            _record_repair_error(snap_id, "no matching done move row for this track/run")
            stats["failed"] += 1
            failures.append(_fail_entry(s, "no matching done move row"))
            continue

        new_key = s["new_rating_key"] or _resolve_new_rating_key(dest, target)
        if not new_key:
            _record_repair_error(snap_id, "new ratingKey not resolvable in target section")
            stats["failed"] += 1
            failures.append(_fail_entry(s, "new ratingKey not resolvable"))
            continue

        if machine_id is None:
            _record_repair_error(snap_id, "machine id unavailable")
            stats["failed"] += 1
            failures.append(_fail_entry(s, "machine id unavailable"))
            continue

        try:
            uri = pps._build_uri(machine_id, [str(new_key)])
            pps._plex_put(f"/playlists/{s['playlist_rating_key']}/items", {"uri": uri})
            with get_db() as db:
                db.execute(
                    "UPDATE segmentation_playlist_snapshot SET repaired=1, repair_error=NULL WHERE id=?",
                    (snap_id,),
                )
                db.execute(
                    "UPDATE segmentation_moves SET new_rating_key=? "
                    "WHERE run_id=? AND track_id=? AND state='done'",
                    (str(new_key), run_id, s["track_id"]),
                )
            stats["repaired"] += 1
        except Exception as e:
            _record_repair_error(snap_id, f"PUT failed: {e}")
            stats["failed"] += 1
            failures.append(_fail_entry(s, f"PUT failed: {e}"))

    _write_repair_report(run_id, failures)
    logger.info(f"segmentation playlist repair run={run_id}: {stats}")
    return stats


def _fail_entry(snap_row, reason: str) -> dict:
    return {
        "playlist_title": snap_row["playlist_title"],
        "playlist_rating_key": snap_row["playlist_rating_key"],
        "track_id": snap_row["track_id"],
        "file_path": snap_row["file_path"],
        "reason": reason,
    }


def _record_repair_error(snap_id: int, reason: str) -> None:
    try:
        with get_db() as db:
            db.execute(
                "UPDATE segmentation_playlist_snapshot SET repair_error=? WHERE id=?",
                (reason, snap_id),
            )
    except Exception as e:
        logger.warning(f"failed to record repair_error for snapshot {snap_id}: {e}")


def _write_repair_report(run_id: str, failures: list[dict]) -> None:
    try:
        REPAIR_REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPAIR_REPORT.write_text(json.dumps({
            "run_id": run_id,
            "needs_manual_repair": failures,
            "count": len(failures),
        }, indent=2))
    except Exception as e:
        logger.warning(f"failed to write repair report: {e}")
