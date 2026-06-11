"""
API routes for the identity resolution sweep (U5).

Prefix: /api/identity
"""

import logging
import threading

from fastapi import APIRouter

from database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/identity", tags=["identity"])


# ---------------------------------------------------------------------------
# Sweep control
# ---------------------------------------------------------------------------


@router.post("/sweep/start")
def start_sweep(dry_run: bool = False):
    """Trigger a full identity resolution sweep."""
    import identity_sweep as sw

    status = sw.get_sweep_status()
    if status["running"]:
        return {"ok": False, "error": "Sweep already running"}

    t = threading.Thread(
        target=sw.run_sweep,
        args=(dry_run,),
        daemon=True,
        name="identity-sweep",
    )
    t.start()
    sw._sweep_thread = t
    return {"ok": True, "dry_run": dry_run}


@router.post("/sweep/stop")
def stop_sweep():
    """Stop the running sweep (graceful — finishes current batch)."""
    import identity_sweep as sw

    if not sw.sweep_status["running"]:
        return {"ok": False, "error": "Sweep is not running"}
    sw.stop_sweep()
    return {"ok": True}


@router.get("/sweep/status")
def sweep_status():
    """Current sweep progress."""
    import identity_sweep as sw
    return sw.get_sweep_status()


# ---------------------------------------------------------------------------
# Damage report
# ---------------------------------------------------------------------------


@router.get("/report")
def damage_report():
    """
    Summary counts by state and tier, plus divergent-track total.
    Only counts rows at the current RESOLVER_VERSION.
    """
    from identity_resolver import RESOLVER_VERSION

    with get_db() as db:
        by_state = db.execute("""
            SELECT state, COUNT(*) AS count
            FROM track_identity
            WHERE resolver_version = ?
            GROUP BY state
        """, (RESOLVER_VERSION,)).fetchall()

        by_tier = db.execute("""
            SELECT tier, COUNT(*) AS count
            FROM track_identity
            WHERE resolver_version = ? AND tier IS NOT NULL
            GROUP BY tier
        """, (RESOLVER_VERSION,)).fetchall()

        divergent_count = db.execute("""
            SELECT COUNT(*) FROM track_identity
            WHERE resolver_version = ? AND divergent = 1
        """, (RESOLVER_VERSION,)).fetchone()[0]

        total_active = db.execute(
            "SELECT COUNT(*) FROM tracks WHERE status = 'active'"
        ).fetchone()[0]

        total_resolved = db.execute(
            "SELECT COUNT(*) FROM track_identity WHERE resolver_version = ?",
            (RESOLVER_VERSION,),
        ).fetchone()[0]

    state_counts = {row["state"]: row["count"] for row in by_state}
    tier_counts = {str(row["tier"]): row["count"] for row in by_tier}

    return {
        "resolver_version": RESOLVER_VERSION,
        "total_active": total_active,
        "total_resolved": total_resolved,
        "unresolved": total_active - total_resolved,
        "by_state": state_counts,
        "by_tier": tier_counts,
        "divergent": divergent_count,
    }


# ---------------------------------------------------------------------------
# Per-state track listings (paginated)
# ---------------------------------------------------------------------------


@router.get("/tracks/confirmed")
def list_confirmed(limit: int = 100, offset: int = 0):
    """Paginated list of confirmed-identity tracks."""
    return _list_by_state("confirmed", limit, offset)


@router.get("/tracks/review")
def list_review(limit: int = 100, offset: int = 0):
    """Tracks needing human review."""
    return _list_by_state("review", limit, offset)


@router.get("/tracks/conflict")
def list_conflict(limit: int = 100, offset: int = 0):
    """Tracks with conflicting evidence."""
    return _list_by_state("conflict", limit, offset)


@router.get("/tracks/unknown")
def list_unknown(limit: int = 100, offset: int = 0):
    """Tracks with unknown identity."""
    return _list_by_state("unknown", limit, offset)


@router.get("/tracks/deferred")
def list_deferred(limit: int = 100, offset: int = 0):
    """Deferred tracks (budget exhausted / mirror unavailable)."""
    return _list_by_state("deferred", limit, offset)


@router.get("/tracks/error")
def list_error(limit: int = 100, offset: int = 0):
    """Tracks whose resolution failed mechanically (fpcalc / missing file)."""
    return _list_by_state("error", limit, offset)


@router.get("/tracks/divergent")
def list_divergent(limit: int = 100, offset: int = 0):
    """Tracks where resolved identity diverges from existing tags."""
    from identity_resolver import RESOLVER_VERSION

    with get_db() as db:
        rows = db.execute("""
            SELECT ti.*, t.file_path, t.format, t.bitrate,
                   t.artist AS tag_artist, t.title AS tag_title,
                   t.album AS tag_album
            FROM track_identity ti
            JOIN tracks t ON t.id = ti.track_id
            WHERE ti.resolver_version = ? AND ti.divergent = 1
            ORDER BY ti.track_id
            LIMIT ? OFFSET ?
        """, (RESOLVER_VERSION, limit, offset)).fetchall()

        total = db.execute(
            "SELECT COUNT(*) FROM track_identity WHERE resolver_version = ? AND divergent = 1",
            (RESOLVER_VERSION,),
        ).fetchone()[0]

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ---------------------------------------------------------------------------
# Single-track identity lookup
# ---------------------------------------------------------------------------


@router.get("/track/{track_id}")
def get_track_identity(track_id: int):
    """Return the current identity record for a single track."""
    with get_db() as db:
        row = db.execute(
            """
            SELECT ti.*, t.file_path, t.artist AS tag_artist, t.title AS tag_title,
                   t.album AS tag_album
            FROM track_identity ti
            JOIN tracks t ON t.id = ti.track_id
            WHERE ti.track_id = ?
            """,
            (track_id,),
        ).fetchone()

    if not row:
        return {"ok": False, "error": "No identity record found"}
    return {"ok": True, "identity": dict(row)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _list_by_state(state: str, limit: int, offset: int) -> dict:
    from identity_resolver import RESOLVER_VERSION

    with get_db() as db:
        rows = db.execute("""
            SELECT ti.*, t.file_path, t.format, t.bitrate,
                   t.artist AS tag_artist, t.title AS tag_title,
                   t.album AS tag_album
            FROM track_identity ti
            JOIN tracks t ON t.id = ti.track_id
            WHERE ti.resolver_version = ? AND ti.state = ?
            ORDER BY ti.track_id
            LIMIT ? OFFSET ?
        """, (RESOLVER_VERSION, state, limit, offset)).fetchall()

        total = db.execute(
            "SELECT COUNT(*) FROM track_identity WHERE resolver_version = ? AND state = ?",
            (RESOLVER_VERSION, state),
        ).fetchone()[0]

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
