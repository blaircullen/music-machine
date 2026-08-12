"""
API routes for the fingerprint verification engine.
"""

import asyncio
import json
import logging
import threading

from fastapi import APIRouter

from database import get_db
from ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/fingerprint", tags=["fingerprint"])

_event_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _event_loop
    _event_loop = loop


# -----------------------------------------------------------------------
# Dashboard / Stats
# -----------------------------------------------------------------------


@router.get("/stats")
def get_stats():
    """Dashboard summary stats."""
    with get_db() as db:
        total_tracks = db.execute(
            "SELECT COUNT(*) FROM tracks WHERE status='active'"
        ).fetchone()[0]

        processed = db.execute(
            "SELECT COUNT(*) FROM fingerprint_results"
        ).fetchone()[0]

        by_status = db.execute("""
            SELECT status, COUNT(*) as count
            FROM fingerprint_results
            GROUP BY status
        """).fetchall()

        status_counts = {row["status"]: row["count"] for row in by_status}

        # AudD usage
        from audd_client import get_usage_stats
        audd_stats = get_usage_stats()

        # Genre distribution
        from genre_normalizer import get_genre_stats
        genre_stats = get_genre_stats()

        # Match source distribution
        source_counts = db.execute("""
            SELECT match_source, COUNT(*) as count
            FROM fingerprint_results
            WHERE match_source IS NOT NULL
            GROUP BY match_source
        """).fetchall()

    return {
        "total_tracks": total_tracks,
        "processed": processed,
        "unprocessed": total_tracks - processed,
        "status_counts": status_counts,
        "matched": status_counts.get("complete", 0) + status_counts.get("tag_written", 0) + status_counts.get("auto_approved", 0),
        "flagged": status_counts.get("flagged", 0),
        "unmatched": status_counts.get("unmatched", 0),
        "failed": status_counts.get("failed", 0),
        "audd": audd_stats,
        "genre_distribution": genre_stats,
        "source_counts": {row["match_source"]: row["count"] for row in source_counts},
    }


@router.get("/progress")
def get_progress():
    """Batch progress (for polling while engine is running)."""
    from fingerprint_engine import get_status
    return get_status()


# -----------------------------------------------------------------------
# Review Queue
# -----------------------------------------------------------------------


@router.get("/review")
def get_review_queue(
    status: str = "flagged",
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    source: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Paginated review queue."""
    with get_db() as db:
        query = """
            SELECT fr.*, t.file_path, t.format, t.bitrate, t.duration,
                   t.artist AS current_artist, t.title AS current_title,
                   t.album AS current_album, t.album_artist AS current_album_artist,
                   t.track_number AS current_track_number
            FROM fingerprint_results fr
            JOIN tracks t ON t.id = fr.track_id
            WHERE fr.status = ?
        """
        params: list = [status]

        if min_confidence is not None:
            query += " AND fr.composite_confidence >= ?"
            params.append(min_confidence)
        if max_confidence is not None:
            query += " AND fr.composite_confidence <= ?"
            params.append(max_confidence)
        if source:
            query += " AND fr.match_source = ?"
            params.append(source)

        query += " ORDER BY fr.composite_confidence DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = db.execute(query, params).fetchall()

        # Get total count for pagination
        count_query = """
            SELECT COUNT(*) FROM fingerprint_results fr WHERE fr.status = ?
        """
        count_params: list = [status]
        if min_confidence is not None:
            count_query += " AND fr.composite_confidence >= ?"
            count_params.append(min_confidence)
        if max_confidence is not None:
            count_query += " AND fr.composite_confidence <= ?"
            count_params.append(max_confidence)
        if source:
            count_query += " AND fr.match_source = ?"
            count_params.append(source)

        total = db.execute(count_query, count_params).fetchone()[0]

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/review/{result_id}/approve")
def approve_result(result_id: int):
    """Approve a single flagged result — write tags."""
    with get_db() as db:
        row = db.execute("""
            SELECT fr.*, t.file_path
            FROM fingerprint_results fr
            JOIN tracks t ON t.id = fr.track_id
            WHERE fr.id = ?
        """, (result_id,)).fetchone()

    if not row:
        return {"ok": False, "error": "Result not found"}

    from fingerprint_engine import _auto_fix_track

    metadata = {
        "artist": row["matched_artist"],
        "title": row["matched_title"],
        "album": row["matched_album"],
        "album_artist": row["matched_album_artist"],
        "date": str(row["matched_year"]) if row["matched_year"] else None,
        "track_number": row["matched_track_number"],
        "total_tracks": None,
        "release_group_id": None,
        "release_id": row["acoustid_release_id"],
        "cover_art_url": row["matched_cover_art_url"],
    }

    _auto_fix_track(
        row["track_id"], row["file_path"], result_id,
        metadata, row["matched_genre"] or "Other",
        row["acoustid_recording_id"] or "",
    )

    return {"ok": True}


@router.post("/review/batch-approve")
def batch_approve(ids: list[int] | None = None, min_confidence: float | None = None):
    """Approve multiple flagged results."""
    approved = 0

    with get_db() as db:
        if ids:
            rows = db.execute(
                f"SELECT id FROM fingerprint_results WHERE id IN ({','.join('?' * len(ids))}) AND status='flagged'",
                ids,
            ).fetchall()
        elif min_confidence is not None:
            rows = db.execute(
                "SELECT id FROM fingerprint_results WHERE status='flagged' AND composite_confidence >= ?",
                (min_confidence,),
            ).fetchall()
        else:
            return {"ok": False, "error": "Provide ids or min_confidence"}

    for row in rows:
        result = approve_result(row["id"])
        if result.get("ok"):
            approved += 1

    return {"ok": True, "approved": approved}


@router.post("/review/{result_id}/edit")
def edit_result(result_id: int, metadata: dict):
    """Manual metadata override for a flagged result."""
    with get_db() as db:
        # Update the matched fields
        fields = []
        values = []
        for key in ["matched_artist", "matched_title", "matched_album",
                     "matched_album_artist", "matched_year", "matched_track_number",
                     "matched_disc_number", "matched_genre", "matched_isrc",
                     "matched_label", "matched_composer"]:
            if key in metadata:
                fields.append(f"{key}=?")
                values.append(metadata[key])

        if fields:
            fields.append("match_source='manual'")
            fields.append("updated_at=CURRENT_TIMESTAMP")
            values.append(result_id)
            db.execute(
                f"UPDATE fingerprint_results SET {', '.join(fields)} WHERE id=?",
                values,
            )

    return {"ok": True}


@router.post("/review/{result_id}/skip")
def skip_result(result_id: int):
    """Skip — mark as manually verified, exclude from future processing."""
    with get_db() as db:
        db.execute(
            "UPDATE fingerprint_results SET status='skipped', "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (result_id,),
        )
    return {"ok": True}


# -----------------------------------------------------------------------
# Unmatched Tracks
# -----------------------------------------------------------------------


@router.get("/unmatched")
def get_unmatched(limit: int = 200, offset: int = 0):
    """List tracks with no match from either pass."""
    with get_db() as db:
        rows = db.execute("""
            SELECT fr.*, t.file_path, t.artist AS current_artist,
                   t.title AS current_title, t.album AS current_album,
                   t.format, t.duration
            FROM fingerprint_results fr
            JOIN tracks t ON t.id = fr.track_id
            WHERE fr.status = 'unmatched'
            ORDER BY t.file_path
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

        total = db.execute(
            "SELECT COUNT(*) FROM fingerprint_results WHERE status='unmatched'"
        ).fetchone()[0]

    return {"items": [dict(r) for r in rows], "total": total}


# -----------------------------------------------------------------------
# History / Rollback
# -----------------------------------------------------------------------


@router.get("/history")
def get_history(limit: int = 200, offset: int = 0):
    """Change history — all applied tag modifications."""
    with get_db() as db:
        rows = db.execute("""
            SELECT ts.*, fr.matched_artist, fr.matched_title, fr.matched_album,
                   fr.matched_genre, fr.composite_confidence, fr.match_source,
                   fr.status AS fp_status,
                   t.file_path
            FROM tag_snapshots ts
            JOIN fingerprint_results fr ON fr.id = ts.fingerprint_result_id
            JOIN tracks t ON t.id = ts.track_id
            ORDER BY ts.snapshot_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

        total = db.execute("SELECT COUNT(*) FROM tag_snapshots").fetchone()[0]

    return {"items": [dict(r) for r in rows], "total": total}


@router.post("/rollback/{result_id}")
def rollback_single(result_id: int):
    """Rollback tags for a single track."""
    from tag_backup import rollback_tags

    with get_db() as db:
        snap = db.execute(
            "SELECT id FROM tag_snapshots WHERE fingerprint_result_id = ?",
            (result_id,),
        ).fetchone()

    if not snap:
        return {"ok": False, "error": "No snapshot found"}

    success = rollback_tags(snap["id"])
    return {"ok": success}


@router.post("/rollback/batch")
def rollback_multiple(ids: list[int]):
    """Rollback tags for multiple tracks."""
    from tag_backup import rollback_batch
    result = rollback_batch(ids)
    return {"ok": True, **result}


# -----------------------------------------------------------------------
# Engine Control
# -----------------------------------------------------------------------


@router.post("/run")
async def start_engine(dry_run: bool = False):
    """Trigger a full fingerprint audit."""
    import fingerprint_engine
    from fingerprint_engine import run_full_audit, get_status

    # Liveness check — auto-recovers if thread died but status stuck on running
    status = get_status()
    if status["running"]:
        return {"ok": False, "error": "Fingerprint engine already running"}

    t = threading.Thread(
        target=run_full_audit,
        args=(dry_run,),
        daemon=True,
        name="fingerprint-engine",
    )
    t.start()
    fingerprint_engine._engine_thread = t
    return {"ok": True, "dry_run": dry_run}


@router.post("/stop")
def stop_engine():
    """Stop the running fingerprint engine."""
    from fingerprint_engine import fp_status, stop

    if not fp_status["running"]:
        return {"ok": False, "error": "Engine is not running"}
    stop()
    return {"ok": True}


# -----------------------------------------------------------------------
# MusicBrainz Mirror Status
# -----------------------------------------------------------------------


@router.get("/mb-status")
def get_mb_status():
    """Check MusicBrainz local mirror status."""
    try:
        from mb_local import is_available, _get_pool
        available = is_available()
        record_count = 0
        if available:
            pool = _get_pool()
            if pool:
                conn = pool.getconn()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT count(*) FROM musicbrainz.recording")
                    record_count = cur.fetchone()[0]
                finally:
                    pool.putconn(conn)
        elif _get_pool() is not None:
            # DB connects but has no data
            return {
                "available": False,
                "type": "public_api",
                "reason": "Local mirror database is empty — data import required",
                "record_count": 0,
            }
        return {
            "available": available,
            "type": "local" if available else "public_api",
            "record_count": record_count,
        }
    except ImportError:
        return {"available": False, "type": "public_api", "record_count": 0}
