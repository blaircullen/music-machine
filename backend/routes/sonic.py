"""
Sonic routes — analysis stats, stream endpoint, player queue, and feedback.

GET  /api/sonic/stats                     — analysis coverage
GET  /api/sonic/queue/{station_id}        — current playlist with track metadata
GET  /api/stream/{track_id}              — range-aware FLAC stream
POST /api/sonic/feedback/{station_id}    — thumbs up / thumbs down
GET  /api/tracks/{track_id}/artwork      — proxy Plex album artwork
"""

import logging
import os
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from database import get_db

router = APIRouter(tags=["sonic"])
logger = logging.getLogger(__name__)

PLEX_URL = os.environ.get("PLEX_URL", "http://10.0.0.13:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "mxrEzLiMjZ1FftGMZaiq")


# ---------------------------------------------------------------------------
# Analysis stats
# ---------------------------------------------------------------------------

@router.get("/api/sonic/stats")
def analysis_stats():
    from sonic_service import get_analysis_stats
    return get_analysis_stats()


# ---------------------------------------------------------------------------
# Player queue — current playlist for a station
# ---------------------------------------------------------------------------

@router.get("/api/sonic/queue/{station_id}")
def station_queue(station_id: int):
    """
    Return the most recently generated playlist for this station,
    with full track metadata needed by the player.
    """
    with get_db() as db:
        station = db.execute(
            "SELECT id FROM stations WHERE id = ?", (station_id,)
        ).fetchone()
        if not station:
            raise HTTPException(status_code=404, detail="Station not found")

        # Get the latest refresh batch — all tracks share the same generated_at
        latest = db.execute(
            "SELECT MAX(generated_at) as ts FROM station_track_history "
            "WHERE station_id = ?",
            (station_id,),
        ).fetchone()

        if not latest or not latest["ts"]:
            return {"station_id": station_id, "tracks": []}

        rows = db.execute(
            """
            SELECT h.track_id, h.id as history_id,
                   t.artist, t.album_artist, t.album, t.title,
                   t.duration, t.track_number, t.file_path, t.format
            FROM station_track_history h
            JOIN tracks t ON t.id = h.track_id
            WHERE h.station_id = ? AND h.generated_at = ?
            ORDER BY h.id
            """,
            (station_id, latest["ts"]),
        ).fetchall()

    tracks = []
    for row in rows:
        tracks.append({
            "track_id": row["track_id"],
            "artist": row["artist"],
            "album_artist": row["album_artist"],
            "album": row["album"],
            "title": row["title"],
            "duration": row["duration"],
            "track_number": row["track_number"],
            "format": row["format"],
            "stream_url": f"/api/stream/{row['track_id']}",
            "artwork_url": f"/api/tracks/{row['track_id']}/artwork",
        })

    return {"station_id": station_id, "tracks": tracks, "generated_at": latest["ts"]}


# ---------------------------------------------------------------------------
# FLAC stream — range-aware
# ---------------------------------------------------------------------------

@router.get("/api/stream/{track_id}")
def stream_track(track_id: int):
    """
    Serve the FLAC file for a track with full Range request support.
    FastAPI/Starlette FileResponse handles Accept-Ranges and byte-range headers
    automatically, enabling seeking in the browser player.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT file_path, format FROM tracks WHERE id = ? AND status = 'active'",
            (track_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Track not found")

    file_path = Path(row["file_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found on disk")

    # Determine MIME type
    fmt = (row["format"] or "").lower()
    media_type = "audio/flac" if fmt == "flac" else "audio/mpeg"

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_path.name,
    )


# ---------------------------------------------------------------------------
# Feedback — thumbs up / thumbs down
# ---------------------------------------------------------------------------

class FeedbackBody(BaseModel):
    track_id: int
    signal: Literal["up", "down"]


@router.post("/api/sonic/feedback/{station_id}")
def station_feedback(station_id: int, body: FeedbackBody):
    with get_db() as db:
        if not db.execute(
            "SELECT id FROM stations WHERE id = ?", (station_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="Station not found")

    from sonic_service import apply_feedback
    try:
        apply_feedback(station_id, body.track_id, body.signal, source="player")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Feedback error station={station_id} track={body.track_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Artwork proxy — fetches album art from Plex
# ---------------------------------------------------------------------------

@router.get("/api/tracks/{track_id}/artwork")
async def track_artwork(track_id: int):
    """
    Proxy Plex album artwork for a track.
    Searches Plex by artist+title, then fetches the thumb URL.
    Falls back to a 404 if no artwork is found.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT artist, title FROM tracks WHERE id = ?", (track_id,)
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Track not found")

    from plex_playlist_sync import search_plex_track

    rating_key = search_plex_track(row["artist"] or "", row["title"] or "")
    if not rating_key:
        raise HTTPException(status_code=404, detail="Track not found in Plex")

    # Fetch thumb URL from Plex metadata
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                f"{PLEX_URL}/library/metadata/{rating_key}",
                params={"X-Plex-Token": PLEX_TOKEN},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("MediaContainer", {}).get("Metadata", [])
            thumb = items[0].get("thumb", "") if items else ""
        except Exception as e:
            logger.warning(f"Plex artwork lookup failed for track {track_id}: {e}")
            raise HTTPException(status_code=404, detail="Artwork not available")

    if not thumb:
        raise HTTPException(status_code=404, detail="No artwork in Plex")

    # Stream the image from Plex
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            img_resp = await client.get(
                f"{PLEX_URL}{thumb}",
                params={"X-Plex-Token": PLEX_TOKEN, "width": 300, "height": 300},
            )
            img_resp.raise_for_status()
        except Exception:
            raise HTTPException(status_code=404, detail="Artwork fetch failed")

    return StreamingResponse(
        content=iter([img_resp.content]),
        media_type=img_resp.headers.get("content-type", "image/jpeg"),
    )
