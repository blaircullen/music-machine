import asyncio
import logging
import os
import threading
import time
from pathlib import Path

from fastapi import APIRouter

from database import get_db
from dedup import find_duplicates, normalize_text
from scanner import AUDIO_EXTENSIONS, generate_fingerprint, scan_directory
from ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scan", tags=["scan"])

# Global scan state
scan_status = {
    "running": False,
    "phase": "idle",
    "progress": 0,
    "total": 0,
    "current_file": "",
    "elapsed_s": 0,
    "started_at": None,
}

_scan_lock = threading.Lock()

# Event loop reference stored at startup for thread->async bridge
_event_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


def _broadcast_sync(msg_type: str, data: dict):
    """Fire-and-forget broadcast from a sync thread context."""
    loop = _event_loop
    if loop is None or not loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(manager.broadcast(msg_type, data), loop)
    except Exception as e:
        logger.debug(f"Broadcast error: {e}")


def _update_status(**kwargs):
    """Update scan_status fields and broadcast to WebSocket clients."""
    scan_status.update(kwargs)
    if scan_status.get("started_at"):
        scan_status["elapsed_s"] = int(time.time() - scan_status["started_at"])
    _broadcast_sync("scan_progress", dict(scan_status))


def run_scan(music_path: Path):
    """
    Full scan pipeline (runs in a background thread):
      1. counting  — count audio files
      2. scanning  — read/upsert metadata for new/changed files
      3. fingerprinting — generate fingerprints for tracks missing them
      4. analyzing — dedup grouping
      5. complete
    """
    if not _scan_lock.acquire(blocking=False):
        logger.warning("Scan already in progress, skipping")
        return

    # Create a job record
    job_id = None
    try:
        with get_db() as db:
            cursor = db.execute(
                "INSERT INTO jobs (job_type, status) VALUES ('scan', 'running')"
            )
            job_id = cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to create scan job: {e}")

    scan_status.update({
        "running": True,
        "phase": "counting",
        "progress": 0,
        "total": 0,
        "current_file": "",
        "elapsed_s": 0,
        "started_at": time.time(),
    })
    _broadcast_sync("scan_progress", dict(scan_status))

    try:
        music_path = Path(music_path)

        # Phase 1: Count
        _update_status(phase="counting", current_file="Counting files...")
        total = 0
        for dirpath, _, filenames in os.walk(str(music_path)):
            for fn in filenames:
                if Path(fn).suffix.lower() in AUDIO_EXTENSIONS:
                    total += 1
        _update_status(total=total)

        # Phase 2: Scan — read metadata for new or changed files
        _update_status(phase="scanning", progress=0)
        scanned = 0

        with get_db() as db:
            for meta in scan_directory(str(music_path)):
                scanned += 1
                _update_status(progress=scanned, current_file=meta["file_path"])

                # Check if track exists and if mtime changed
                existing = db.execute(
                    "SELECT id, scanned_at FROM tracks WHERE file_path = ?",
                    (meta["file_path"],),
                ).fetchone()

                if existing:
                    # Update only if we detect a re-scan is needed (skip for now — mtime check)
                    # For a complete rewrite: update file_size and metadata if changed
                    continue

                # Insert new track
                cur = db.execute(
                    """INSERT INTO tracks
                       (file_path, file_size, format, bitrate, bit_depth, sample_rate,
                        duration, artist, album_artist, album, title, track_number,
                        disc_number, fingerprint, sha256, status, scanned_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
                    """,
                    (
                        meta["file_path"],
                        meta.get("file_size"),
                        meta.get("format"),
                        meta.get("bitrate"),
                        meta.get("bit_depth"),
                        meta.get("sample_rate"),
                        meta.get("duration"),
                        meta.get("artist", ""),
                        meta.get("album_artist", ""),
                        meta.get("album", ""),
                        meta.get("title", ""),
                        meta.get("track_number"),
                        meta.get("disc_number"),
                        None,  # fingerprint — done in next phase
                        None,  # sha256 — optional
                    ),
                )
                # Enqueue new track for sonic analysis
                db.execute(
                    "INSERT OR IGNORE INTO analysis_queue (track_id) VALUES (?)",
                    (cur.lastrowid,),
                )

        # Mark deleted files
        _update_status(phase="scanning", current_file="Checking for removed files...")
        with get_db() as db:
            active_paths = db.execute(
                "SELECT id, file_path FROM tracks WHERE status = 'active'"
            ).fetchall()
            for row in active_paths:
                if not Path(row["file_path"]).exists():
                    db.execute(
                        "UPDATE tracks SET status = 'deleted' WHERE id = ?", (row["id"],)
                    )

        # Phase 3: Metadata-based dedup (fast — no fingerprints yet)
        _update_status(phase="analyzing", progress=0, current_file="Analyzing duplicates...")

        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM tracks WHERE status = 'active'"
            ).fetchall()
            active_tracks = [dict(r) for r in rows]

        groups = find_duplicates(active_tracks)

        with get_db() as db:
            # Clear unresolved groups so we don't accumulate stale data
            db.execute(
                "DELETE FROM dupe_group_members WHERE group_id IN "
                "(SELECT id FROM dupe_groups WHERE resolved = 0)"
            )
            db.execute("DELETE FROM dupe_groups WHERE resolved = 0")

            for group in groups:
                keep_id = group["keep_track"]["id"]
                cursor = db.execute(
                    "INSERT INTO dupe_groups (match_type, confidence, kept_track_id) VALUES (?, ?, ?)",
                    (group["match_type"], group["confidence"], keep_id),
                )
                group_id = cursor.lastrowid
                for track in group["tracks"]:
                    db.execute(
                        "INSERT INTO dupe_group_members (group_id, track_id) VALUES (?, ?)",
                        (group_id, track["id"]),
                    )

        # Phase 4: Fingerprint ONLY tracks in dupe groups (to confirm matches)
        # This is targeted — typically a few hundred tracks, not the whole library.
        with get_db() as db:
            dupe_track_ids = db.execute(
                """SELECT DISTINCT t.id, t.file_path
                   FROM tracks t
                   JOIN dupe_group_members m ON t.id = m.track_id
                   WHERE t.fingerprint IS NULL AND t.status = 'active'"""
            ).fetchall()

        fp_total = len(dupe_track_ids)
        if fp_total > 0:
            _update_status(
                phase="fingerprinting",
                progress=0,
                total=fp_total,
                current_file=f"Fingerprinting {fp_total} tracks in dupe groups...",
            )
            for i, row in enumerate(dupe_track_ids):
                _update_status(progress=i + 1, current_file=row["file_path"])
                fp = generate_fingerprint(row["file_path"])
                if fp:
                    try:
                        with get_db() as db:
                            db.execute(
                                "UPDATE tracks SET fingerprint = ? WHERE id = ?",
                                (fp, row["id"]),
                            )
                    except Exception as e:
                        logger.warning(f"Failed to save fingerprint for {row['file_path']}: {e}")

            # Re-run dedup now that dupe-group tracks have fingerprints
            _update_status(phase="analyzing", current_file="Re-analyzing with fingerprints...")
            with get_db() as db:
                # Reload updated tracks
                rows = db.execute("SELECT * FROM tracks WHERE status = 'active'").fetchall()
                active_tracks = [dict(r) for r in rows]

            groups = find_duplicates(active_tracks)

            with get_db() as db:
                db.execute(
                    "DELETE FROM dupe_group_members WHERE group_id IN "
                    "(SELECT id FROM dupe_groups WHERE resolved = 0)"
                )
                db.execute("DELETE FROM dupe_groups WHERE resolved = 0")

                for group in groups:
                    keep_id = group["keep_track"]["id"]
                    cursor = db.execute(
                        "INSERT INTO dupe_groups (match_type, confidence, kept_track_id) VALUES (?, ?, ?)",
                        (group["match_type"], group["confidence"], keep_id),
                    )
                    group_id = cursor.lastrowid
                    for track in group["tracks"]:
                        db.execute(
                            "INSERT INTO dupe_group_members (group_id, track_id) VALUES (?, ?)",
                            (group_id, track["id"]),
                        )

        logger.info(f"Scan analysis found {len(groups)} duplicate groups")

        # Auto-resolve high-confidence dupes if threshold is set
        try:
            _auto_resolve_if_configured()
        except Exception as e:
            logger.error(f"Auto-resolve failed: {e}")

        # Update job as completed
        if job_id:
            with get_db() as db:
                db.execute(
                    "UPDATE jobs SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (job_id,),
                )

        _update_status(phase="complete", current_file="", running=False)
        _broadcast_sync("stats_update", {"event": "scan_complete"})

    except Exception as e:
        logger.error(f"Scan failed: {e}", exc_info=True)
        scan_status["running"] = False
        scan_status["phase"] = "failed"
        _broadcast_sync("scan_progress", dict(scan_status))

        if job_id:
            try:
                with get_db() as db:
                    db.execute(
                        "UPDATE jobs SET status = 'failed', error_msg = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (str(e), job_id),
                    )
            except Exception:
                pass

    finally:
        scan_status["running"] = False
        _scan_lock.release()


def _auto_resolve_if_configured():
    """Auto-resolve dupe groups above the configured threshold."""
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'auto_resolve_threshold'"
        ).fetchone()
    threshold = float(row["value"]) if row else 0.0
    if threshold <= 0:
        return

    from routes.dupes import _resolve_group_internal

    with get_db() as db:
        groups = db.execute(
            "SELECT id, kept_track_id, confidence FROM dupe_groups WHERE resolved = 0 AND confidence >= ?",
            (threshold,),
        ).fetchall()

    resolved = 0
    for g in groups:
        try:
            _resolve_group_internal(g["id"], g["kept_track_id"])
            resolved += 1
        except Exception as e:
            logger.error(f"Auto-resolve failed for group {g['id']}: {e}")

    if resolved > 0:
        logger.info(f"Auto-resolved {resolved} dupe groups at threshold {threshold}")


@router.post("")
@router.post("/")
async def start_scan():
    """Start a background scan. Returns error if scan already running."""
    if scan_status["running"]:
        return {"ok": False, "error": "Scan already in progress"}

    music_path = Path(os.environ.get("MUSIC_PATH", "/music"))
    t = threading.Thread(target=run_scan, args=(music_path,), daemon=True)
    t.start()
    return {"ok": True}


@router.get("/status")
def get_scan_status():
    """Return current scan status."""
    return {
        "running": scan_status["running"],
        "phase": scan_status["phase"],
        "progress": scan_status["progress"],
        "total": scan_status["total"],
        "current_file": scan_status["current_file"],
        "elapsed_s": scan_status["elapsed_s"],
    }
