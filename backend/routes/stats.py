import logging

from fastapi import APIRouter

from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stats", tags=["stats"])

LOSSY_FORMATS = "('mp3', 'aac', 'm4a', 'ogg', 'wma', 'opus')"


@router.get("")
@router.get("/")
def get_stats():
    """
    Aggregate library statistics.
    Response shape matches API contract:
    {total_tracks, flac_count, lossy_count, dupes_found, upgrades_pending,
     lossy_upgrades_pending, hires_upgrades_pending,
     upgrades_completed, library_size_gb, formats: [{format, count}]}
    """
    with get_db() as db:
        total_tracks = db.execute(
            "SELECT COUNT(*) FROM tracks WHERE status = 'active'"
        ).fetchone()[0]

        flac_count = db.execute(
            "SELECT COUNT(*) FROM tracks WHERE status = 'active' AND format = 'flac'"
        ).fetchone()[0]

        lossy_count = db.execute(
            "SELECT COUNT(*) FROM tracks WHERE status = 'active' "
            f"AND format IN {LOSSY_FORMATS}"
        ).fetchone()[0]

        dupes_found = db.execute(
            "SELECT COUNT(*) FROM dupe_groups WHERE resolved = 0"
        ).fetchone()[0]

        upgrades_pending = db.execute(
            "SELECT COUNT(*) FROM upgrade_queue WHERE status IN ('found', 'approved')"
        ).fetchone()[0]

        upgrade_breakdown = db.execute(
            f"""SELECT
                 SUM(CASE WHEN t.format IN {LOSSY_FORMATS} THEN 1 ELSE 0 END) as lossy_pending,
                 SUM(CASE WHEN t.format NOT IN {LOSSY_FORMATS} THEN 1 ELSE 0 END) as hires_pending
               FROM upgrade_queue uq
               JOIN tracks t ON t.id = uq.track_id
               WHERE uq.status IN ('found', 'approved')"""
        ).fetchone()
        lossy_upgrades_pending = upgrade_breakdown["lossy_pending"] or 0
        hires_upgrades_pending = upgrade_breakdown["hires_pending"] or 0

        upgrades_completed = db.execute(
            "SELECT COUNT(*) FROM upgrade_queue WHERE status = 'completed'"
        ).fetchone()[0]

        library_size_bytes = db.execute(
            "SELECT COALESCE(SUM(file_size), 0) FROM tracks WHERE status = 'active'"
        ).fetchone()[0]

        formats = db.execute(
            """SELECT format, COUNT(*) as count
               FROM tracks
               WHERE status = 'active'
               GROUP BY format
               ORDER BY count DESC"""
        ).fetchall()

    return {
        "total_tracks": total_tracks,
        "flac_count": flac_count,
        "lossy_count": lossy_count,
        "dupes_found": dupes_found,
        "upgrades_pending": upgrades_pending,
        "lossy_upgrades_pending": lossy_upgrades_pending,
        "hires_upgrades_pending": hires_upgrades_pending,
        "upgrades_completed": upgrades_completed,
        "library_size_gb": round(library_size_bytes / 1024 / 1024 / 1024, 3),
        "formats": [{"format": r["format"], "count": r["count"]} for r in formats],
    }
