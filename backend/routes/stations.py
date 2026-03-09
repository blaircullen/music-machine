"""
Stations routes — CRUD for Pandora-style stations + manual refresh trigger.
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
    seed_artists: list[str]
    bpm_min: Optional[int] = None
    bpm_max: Optional[int] = None
    decade_min: Optional[int] = None
    decade_max: Optional[int] = None
    plex_playlist_name: Optional[str] = None
    lastfm_min_listeners: int = 500_000


class StationUpdate(BaseModel):
    name: Optional[str] = None
    seed_artists: Optional[list[str]] = None
    bpm_min: Optional[int] = None
    bpm_max: Optional[int] = None
    decade_min: Optional[int] = None
    decade_max: Optional[int] = None
    plex_playlist_name: Optional[str] = None
    lastfm_min_listeners: Optional[int] = None


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["seed_artists"] = json.loads(d.get("seed_artists") or "[]")
    return d


@router.get("")
def list_stations():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM stations ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("")
def create_station(body: StationCreate):
    playlist_name = body.plex_playlist_name or body.name
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO stations "
            "(name, seed_artists, bpm_min, bpm_max, decade_min, decade_max, "
            "plex_playlist_name, lastfm_min_listeners) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body.name,
                json.dumps(body.seed_artists),
                body.bpm_min,
                body.bpm_max,
                body.decade_min,
                body.decade_max,
                playlist_name,
                body.lastfm_min_listeners,
            ),
        )
        station_id = cur.lastrowid
        row = db.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
    return _row_to_dict(row)


@router.put("/{station_id}")
def update_station(station_id: int, body: StationUpdate):
    with get_db() as db:
        existing = db.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Station not found")

        updates = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.seed_artists is not None:
            updates["seed_artists"] = json.dumps(body.seed_artists)
        if body.bpm_min is not None or "bpm_min" in body.model_fields_set:
            updates["bpm_min"] = body.bpm_min
        if body.bpm_max is not None or "bpm_max" in body.model_fields_set:
            updates["bpm_max"] = body.bpm_max
        if body.decade_min is not None or "decade_min" in body.model_fields_set:
            updates["decade_min"] = body.decade_min
        if body.decade_max is not None or "decade_max" in body.model_fields_set:
            updates["decade_max"] = body.decade_max
        if body.plex_playlist_name is not None:
            updates["plex_playlist_name"] = body.plex_playlist_name
        if body.lastfm_min_listeners is not None:
            updates["lastfm_min_listeners"] = body.lastfm_min_listeners

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
        existing = db.execute("SELECT id FROM stations WHERE id = ?", (station_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Station not found")
        db.execute("DELETE FROM stations WHERE id = ?", (station_id,))
    return {"ok": True}


@router.post("/{station_id}/refresh")
def refresh_station(station_id: int):
    """Trigger a background refresh for one station."""
    from stations_service import refresh_station as _refresh, get_refresh_status

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

    t = threading.Thread(target=_run, daemon=True, name=f"station-refresh-{station_id}")
    t.start()
    return {"ok": True}


@router.get("/{station_id}/status")
def station_refresh_status(station_id: int):
    from stations_service import get_refresh_status
    return get_refresh_status(station_id)
