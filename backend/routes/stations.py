"""
Stations routes — CRUD for sonic-similarity stations + refresh trigger.

Stations are seeded by 3–5 specific tracks (track IDs from the local library),
not artist names. The recommendation engine is sonic_service.py (cosine
similarity on Essentia feature vectors) — Last.fm is no longer used.
"""

import json
import logging
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_db

router = APIRouter(prefix="/api/stations", tags=["stations"])
logger = logging.getLogger(__name__)


class StationCreate(BaseModel):
    name: str
    seed_track_ids: list[int]
    plex_playlist_name: Optional[str] = None


class StationUpdate(BaseModel):
    name: Optional[str] = None
    seed_track_ids: Optional[list[int]] = None
    plex_playlist_name: Optional[str] = None


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["seed_track_ids"] = json.loads(d.get("seed_track_ids") or "[]")
    return d


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
def list_stations():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM stations ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("")
def create_station(body: StationCreate):
    if not body.seed_track_ids:
        raise HTTPException(status_code=400, detail="At least one seed track is required")

    playlist_name = body.plex_playlist_name or body.name
    with get_db() as db:
        # Verify seed tracks exist in library
        placeholders = ",".join("?" * len(body.seed_track_ids))
        found = db.execute(
            f"SELECT COUNT(*) as n FROM tracks WHERE id IN ({placeholders}) AND status = 'active'",
            body.seed_track_ids,
        ).fetchone()["n"]
        if found == 0:
            raise HTTPException(status_code=400, detail="None of the seed tracks found in library")

        cur = db.execute(
            "INSERT INTO stations (name, seed_track_ids, plex_playlist_name) VALUES (?, ?, ?)",
            (body.name, json.dumps(body.seed_track_ids), playlist_name),
        )
        station_id = cur.lastrowid
        row = db.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
    return _row_to_dict(row)


@router.get("/search/tracks")
def search_tracks(q: str, limit: int = 20):
    """
    Search the local library by artist or title for the station seed picker.
    Returns lightweight results: id, artist, title, album, duration.
    Must be defined before /{station_id} routes to avoid route conflicts.
    """
    if not q or len(q) < 2:
        return []

    pattern = f"%{q}%"
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, artist, title, album, duration
            FROM tracks
            WHERE status = 'active'
              AND (artist LIKE ? OR title LIKE ? OR album LIKE ?)
            ORDER BY artist, title
            LIMIT ?
            """,
            (pattern, pattern, pattern, limit),
        ).fetchall()

    return [dict(r) for r in rows]


@router.get("/{station_id}")
def get_station(station_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Station not found")
    return _row_to_dict(row)


@router.put("/{station_id}")
def update_station(station_id: int, body: StationUpdate):
    with get_db() as db:
        existing = db.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Station not found")

        updates: dict = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.seed_track_ids is not None:
            updates["seed_track_ids"] = json.dumps(body.seed_track_ids)
        if body.plex_playlist_name is not None:
            updates["plex_playlist_name"] = body.plex_playlist_name

        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            db.execute(
                f"UPDATE stations SET {set_clause} WHERE id = ?",
                [*updates.values(), station_id],
            )

        row = db.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
    return _row_to_dict(row)


@router.delete("/{station_id}")
def delete_station(station_id: int):
    with get_db() as db:
        if not db.execute("SELECT id FROM stations WHERE id = ?", (station_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Station not found")
        db.execute("DELETE FROM stations WHERE id = ?", (station_id,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

@router.post("/{station_id}/refresh")
def refresh_station(station_id: int):
    """Trigger a background sonic refresh for one station."""
    from sonic_service import refresh_station as _refresh, get_refresh_status

    status = get_refresh_status(station_id)
    if status.get("running"):
        return {"ok": False, "error": "Refresh already running for this station"}

    with get_db() as db:
        row = db.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Station not found")
        station = _row_to_dict(row)

    def _run():
        _refresh(station)

    t = threading.Thread(
        target=_run, daemon=True, name=f"station-refresh-{station_id}"
    )
    t.start()
    return {"ok": True}


@router.get("/{station_id}/status")
def station_refresh_status(station_id: int):
    from sonic_service import get_refresh_status
    return get_refresh_status(station_id)
